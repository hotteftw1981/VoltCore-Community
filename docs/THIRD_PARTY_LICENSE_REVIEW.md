# Third-party dependency review (pre-publication)

This is an **inventory**, not an assertion that every transitive dependency or asset has been cleared for redistribution.

## Python direct dependencies (`requirements.txt`)
- FastAPI 0.142.2
- Uvicorn 0.38.0 (standard extras)
- ocpp 2.1.0
- websockets 12.0
- Jinja2 3.x
- ReportLab 4.4.9
- Pillow 10.x–12.x
- python-multipart 0.0.20
- tzdata 2025.2–2027.x
- openpyxl 3.1.5
- Paramiko 3.5–4.x
- qrcode 8.x
- cryptography 44.x–46.x
- pywebpush 2.x

## Before public release
- [ ] Record the current upstream license, copyright notices and source URL for each direct dependency and its actual installed/transitive versions.
- [ ] Validate ReportLab, Pillow, crypto, push and any other libraries against planned AGPL distribution; do not infer license from package name.
- [ ] Review bundled browser JS/CSS, logos/SVGs and any fonts/images for ownership and redistribution rights.
- [ ] Generate an SBOM and license report from an isolated release environment; review findings instead of assuming a scan is legal clearance.
- [ ] Confirm attribution / NOTICE requirements, if any, are fulfilled in the distributed archive.
- [ ] Re-check before every publication when versions/assets change.

## Current observation
The `requirements.txt` file contains package names and version constraints, not a license manifest. The original Community logo assets are present in source; their authorship and rights are not independently verified by this document.

No DRK/other-organization references should be introduced into public-facing material.
