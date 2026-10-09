# Installation, localization and security verification (P10 + P11)

## Installation
1. Deploy the documented Docker Compose stack to a supported Docker host with persistent data volumes.
2. Configure a unique admin account, a strong session secret, and an appropriate reverse proxy with HTTPS for the web UI.
3. For publicly exposed OCPP endpoints prefer WSS and per-station OCPP credentials. Keep an intentional local WS deployment restricted to a trusted network.
4. Complete first-run setup where available. Configure pricing, mail delivery, and permissions for your installation.
5. Back up the database and configuration before applying updates. Test a restore on a separate instance.
6. Keep charging stations offline from changes until migrations and connector configurations are confirmed.

## Language support
- User interface locale may be selected with ?lang=de or ?lang=en; otherwise browser Accept-Language is considered.
- This controls the HTML document language and provides an explicit locale to templates; it does not mean every existing text is translated.
- A complete DE/EN text catalog and browser verification are required before claiming full translation coverage.

## Safety/release checks
- Confirm account roles and write restrictions, CSRF/session protections, security headers, and no-store cache handling in live deployment.
- Check real browser behavior and reverse proxy headers; automated source checks are not a substitute for penetration testing.
- Run backup/restore migration test on an isolated copy.
- Package and release only after chained P03-P11 acceptance is complete.

This note describes deployment precautions; verify product-specific environment variables and Compose instructions in the repository before use.
