/*
 * Eventscribe harvester (browser capture adapter).
 * Run in the browser console (or via automation) on any page of an eventscribe site,
 * e.g. https://ctos2026.eventscribe.net/index.asp?launcher=1
 *
 * What it does
 *   1. Fetches the public listing pages (posters, presentations, agenda days, events, sponsors).
 *   2. Discovers every detail endpoint (PosterInfo, PresentationInfo, SessionInfo, ExhibitorInfo).
 *   3. Fetches each detail card politely: one request at a time, 1.5 s gap, backoff on HTTP 429.
 *   4. Keeps only the content card HTML (navigation and scripts removed) plus url, time and size.
 *   5. Exposes window.__H (progress) and window.exportBundle(part, size) for chunked export.
 *
 * Public pages only. No login, no favourites, no writes. credentials: 'omit' on every request.
 */
(async () => {
  const BASE = location.origin;
  const P = new DOMParser();
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const H = (window.__H = {
    source: { baseUrl: BASE, platform: 'eventscribe', userAgent: navigator.userAgent, started: new Date().toISOString() },
    listings: {}, details: {}, errors: [], n: 0, total: 0, done: false, rate: { delay: 1500, n429: 0 }
  });

  async function get(url) {
    for (let attempt = 0; attempt < 5; attempt++) {
      try {
        const r = await fetch(url, { credentials: 'omit' });
        if (r.status === 429) {
          H.rate.n429++;
          H.rate.delay = Math.min(H.rate.delay + 500, 5000);
          const retryAfter = parseInt(r.headers.get('Retry-After') || '0', 10) * 1000;
          await sleep(Math.max(retryAfter, 20000 * (attempt + 1)));
          continue;
        }
        const text = await r.text();
        // eventscribe returns transient 403s under load; the same URL succeeds on retry.
        if (r.status === 403 && attempt < 4) { H.errors.push([url, 403]); await sleep(4000 * (attempt + 1)); continue; }
        if (!r.ok) { H.errors.push([url, r.status]); return null; }
        return text;
      } catch (e) { H.errors.push([url, String(e)]); await sleep(3000); }
    }
    return null;
  }

  const LISTINGS = {
    home: '/index.asp?launcher=1',
    posters: '/PosterTitles.asp?PosterSortOrder=num&pfp=BrowsebyPosterNumber',
    postersByCategory: '/PosterTitles.asp?PosterSortOrder=num&pfp=BrowsebyPosterTitle',
    presentations: '/SearchByPresentation.asp?pfp=BrowsebyTitle',
    agenda: '/agenda.asp?pfp=BrowsebyDay',
    sponsorEvents: '/agenda.asp?pfp=BrowsebySP',
    networking: '/agenda.asp?pfp=BrowsebyNE',
    ancillary: '/agenda.asp?pfp=BrowsebyAE',
    spagn: '/agenda.asp?pfp=BrowsebyPA',
    sponsors: '/sponsors.asp?pfp=Sponsors'
  };

  const strip = root => {
    root.querySelectorAll('script,style,nav,#mobilenav,.navbar-wrapper,footer,#google_translate_element,.popup_header').forEach(e => e.remove());
    return root;
  };
  const norm = du => {
    const m = du.match(/ajaxcalls\/(PosterInfo|PresentationInfo|SessionInfo|ExhibitorInfo)\.asp\?.*?(PosterID|PresentationID|BoothID)=(\d+)/i);
    if (!m) return null;
    const efp = (du.match(/efp=([A-Za-z0-9]+)/) || [])[1];
    return [m[1] + ':' + m[3], '/ajaxcalls/' + m[1] + '.asp?' + (efp ? 'efp=' + efp + '&' : '') + m[2] + '=' + m[3]];
  };

  // Day pages are discovered from the agenda page, so the adapter works for any eventscribe meeting.
  const agendaHtml = await get(LISTINGS.agenda);
  if (agendaHtml) {
    const d = P.parseFromString(agendaHtml, 'text/html');
    d.querySelectorAll('a[href*="agenda.asp?pfp=days"]').forEach(a => {
      const href = a.getAttribute('href');
      const day = (href.match(/day=(\d+\/\d+\/\d+)/) || [])[1];
      if (day) { const [mm, dd, yy] = day.split('/'); LISTINGS['day_' + yy + '_' + mm + '_' + dd] = href.startsWith('/') ? href : '/' + href; }
    });
  }

  const queue = new Map();
  for (const [name, url] of Object.entries(LISTINGS)) {
    const html = await get(url);
    await sleep(H.rate.delay);
    if (!html) continue;
    const d = P.parseFromString(html, 'text/html');
    d.querySelectorAll('[data-url]').forEach(e => { const r = norm(e.getAttribute('data-url') || ''); if (r && !queue.has(r[0])) queue.set(r[0], r[1]); });
    const main = strip(d.querySelector('.content-wrap') || d.body);
    H.listings[name] = { url: BASE + url, retrievedAt: new Date().toISOString(), bytes: html.length, html: main.innerHTML.replace(/\s+/g, ' ') };
  }

  const items = [...queue.entries()];
  H.total = items.length;
  while (items.length) {
    const [key, url] = items.shift();
    const html = await get(url);
    H.n++;
    if (html) {
      const d = P.parseFromString(html, 'text/html');
      const card = strip(d.querySelector('.card') || d.querySelector('.popup_content') || d.body);
      H.details[key] = { url: BASE + url, retrievedAt: new Date().toISOString(), bytes: html.length, html: card.outerHTML.replace(/\s+/g, ' ') };
      if (/^(SessionInfo|PresentationInfo)/.test(key)) {
        card.querySelectorAll('[data-url]').forEach(e => {
          const r = norm(e.getAttribute('data-url') || '');
          if (r && !queue.has(r[0])) { queue.set(r[0], r[1]); items.push(r); H.total++; }
        });
      }
    }
    await sleep(H.rate.delay);
  }
  H.done = true;
  H.source.finished = new Date().toISOString();
})();

// Compressed export (preferred): gzip plus base64 the whole bundle, then read it in slices.
//   await prepareCompressed()            -> {jsonChars, gzBytes, chunks, sha256}
//   compressedChunk(i)                   -> 'CHUNK<i>|<base64 slice>'  (240,000 characters each)
// Decode with capture/decode_chunks.py, which verifies the SHA-256.
window.prepareCompressed = async function () {
  const H = window.__H;
  const json = JSON.stringify({ source: H.source, listings: H.listings, details: H.details, errors: H.errors, rate: H.rate });
  const gz = new Uint8Array(await new Response(new Blob([json]).stream().pipeThrough(new CompressionStream('gzip'))).arrayBuffer());
  let bin = '';
  for (let i = 0; i < gz.length; i += 0x8000) bin += String.fromCharCode.apply(null, gz.subarray(i, i + 0x8000));
  window.__B64 = btoa(bin);
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(json));
  window.__SHA = [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
  return { jsonChars: json.length, gzBytes: gz.length, chunks: Math.ceil(window.__B64.length / 240000), sha256: window.__SHA };
};
window.compressedChunk = i => 'CHUNK' + i + '|' + window.__B64.slice(i * 240000, (i + 1) * 240000);

// Chunked export: returns part `part` of the bundle as a JSON string of about `size` characters of detail HTML.
window.exportBundle = function (part = 0, size = 3_000_000) {
  const H = window.__H;
  const keys = Object.keys(H.details).sort();
  const chunks = [];
  let cur = [], len = 0;
  for (const k of keys) { const l = H.details[k].html.length; if (len + l > size && cur.length) { chunks.push(cur); cur = []; len = 0; } cur.push(k); len += l; }
  if (cur.length) chunks.push(cur);
  const details = {};
  (chunks[part] || []).forEach(k => (details[k] = H.details[k]));
  return JSON.stringify({ part, parts: chunks.length, source: H.source, listings: part === 0 ? H.listings : {}, details, errors: part === 0 ? H.errors : [], rate: H.rate });
};
