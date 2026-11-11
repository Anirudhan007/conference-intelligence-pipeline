# Security and Responsible Capture

## Reporting Security or Data-Governance Issues

Report security vulnerabilities or suspected exposure of embargoed/confidential material privately to the repository owner. **Never** include raw conference captures, embargoed abstract text, personal speaker contact data, API tokens, or credentials in issues, pull requests, or commit messages.

## Responsible Web Capture

Direct HTTP capture (`capture/capture.py`) is engineered to be conservative and auditable:

- **Public pages only:** No login bypass, no session cookies, no form submissions, and no access behind delegate paywalls.
- **Explicit attribution:** Live capture requires a valid `--contact-email` (or local `CONTACT_EMAIL` environment variable) in the `User-Agent` header and refuses to run with malformed or empty values.
- **Fail-closed `robots.txt` enforcement:** Capture verifies `/robots.txt` before fetching content and aborts immediately if the robots policy cannot be established or disallows access.
- **Polite pacing and backoff:** Single-request concurrency with configurable inter-request delay (default `1.5s`) and exponential backoff honouring `Retry-After` on HTTP `429`.

Do not run capture against any website whose terms of service, `robots.txt` policy, or embargo rules prohibit automated collection.

## Protecting Confidential and Embargoed Data

Raw bundles, real HTML fixtures, draft clinical readouts, and pre-release exports may contain embargoed scientific abstracts and speaker affiliations. Never commit them to this repository; store raw/generated artefacts outside Git in access-controlled private storage, and follow [docs/data-governance.md](docs/data-governance.md).
