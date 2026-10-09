# Publication and commercial-edition rights checklist

**Status: preparation only.** The Community repository must remain private until this checklist is expressly approved. The `LICENSE` file on this preparation branch is a **proposed** AGPL-3.0-only release license; it is not a certification of copyright ownership.

## Intended structure
- **VoltCore Community:** source available under GNU AGPL v3 only (`AGPL-3.0-only`) after publication.
- **VoltCore Pro:** separate private/proprietary product; no Pro source or private assets included here.
- **VoltCore Cloud:** independently licensed hosted/commercial offering. If it incorporates AGPL-only code without sufficient separate rights, AGPL obligations may apply, including network source-access provisions for modified covered versions.
- **Branding:** the AGPL licenses copyrightable program content; it does not grant trademark ownership. Trademark rights for the VoltCore name/logo must be checked and protected separately. Do not claim registered trademark protection without registration/clearance.

## Mandatory approval before making this repository public
- [ ] Identify the actual copyright holder(s), including any employment/client/association rights, and confirm that they may license every original part under AGPL.
- [ ] Inventory all third-party source/assets, fonts, icons, embedded JS/CSS and dependencies. Preserve their license terms, notices and attribution; resolve incompatible or uncertain licenses.
- [ ] Inspect **all reachable Git history**, branches and tags, not just current main, for tokens, passwords, personal data, private Pro-only implementation and licensed third-party material. Rotate any leaked secrets: simply deleting from the latest commit does not remove history.
- [ ] Confirm the public-repository history policy. Consider publishing a clean Community-only repository/history if prior commits contain Pro-exclusive IP or secrets.
- [ ] Confirm control of every shared original code component for **independent commercial licensing** of Pro/Cloud. Publishing one owned version under AGPL does not by itself prevent the owner separately licensing the same owned code, but outside contributions generally require additional rights for proprietary reuse.
- [ ] Decide on contributor terms (e.g. DCO plus explicit contributor agreement/assignment or separate commercial licensing permission as legally appropriate) **before accepting third-party PRs intended for Pro/Cloud reuse**.
- [ ] Confirm appropriate copyright notices naming Patrick Garbe only where ownership is legally established; investigate any other potentially relevant rights holders separately.
- [ ] Check public hosting defaults, configuration, TLS/WSS instructions, data handling and security disclosures; the RC was not live field-tested with chargers.
- [ ] Obtain legal review of licensing, trademark and third-party rights before changing repository visibility.

## Publication boundaries
Do not upload Pro/Cloud source, private keys, production databases, credentials, non-redistributable images, or sensitive customer data. Keep Pro and Cloud development repositories private and technically separate. This checklist is not legal advice.
