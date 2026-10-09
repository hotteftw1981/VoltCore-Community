# Changelog

## 0.9.7.95 — Final dead-code cleanup pass

- Remove five unreferenced historical Pro/portal helper functions (182 lines).
- Preserve Community registration defaults and existing persistent schemas.
- Community CI and concurrent charging regression suite passed before merging.

## 0.9.7.94 — Community-only API reduction

- Remove 25 unused legacy Pro achievement, event and bonus/voucher admin functions.
- Retain schema and shared constants required for existing installations.
- Full Community CI and concurrent charging regression suite passed before merging.

## 0.9.7.93 — Community scope cleanup

- Removed unused legacy Pro achievement seed catalog and private PIN portal functions.
- No Community charging API or persistent database schema removed.
- Community CI and P03 charging regressions validated changes.

## 0.9.7.92 — Update-Center correction

- Public GitHub Releases can be checked without a GitHub token; private repositories still need a read token.
- Updated confusing token/Portainer UI; installation now shows an explicit backup and manual Docker/Portainer deployment procedure, without pretending one-click installation is available.
- No Docker socket or privileged background updater added.

## 0.9.7.91 — CSV import and migration consolidation

- Added provider-neutral CSV import with column mapping, preview, duplicate detection and explicit execution.
- Added VoltCore migration ZIP export, compatibility preview and guarded import into empty installations.
- Preserved the existing lade.cloud XLSX import alongside new workflows.
- Excluded temporary import uploads from backups.
- Kept Docker-host updater agent out of the application for security and deployment independence.
- Includes dedicated CSV/migration and backup regression tests.


## 0.9.7.90 — Community release candidate

- Community-only scope enforced; no personal PIN portal, XP or gamification UI.
- Concurrent charging limits per charging user and RFID.
- RFID local-list diagnostics, OCPP connector and charging session data quality improvements.
- Reporting and backup integrity checks, release regression coverage and access-control improvements.
- Improved tariff row readability, parallel limit form layout and RFID notes editor.
- Standalone Portainer source build supported via `pull_policy: build`.
- Known limitation: additional physical Amtron/Amedio field tests deliberately waived for this release candidate.


All notable changes to VoltCore Community are documented here.

## 0.9.7.75 — Community scope correction

### Removed
- Removed LiveView from VoltCore Community completely, including Standard, Pro and People variants.
- Removed LiveView routes, public APIs, settings UI, template and edition-specific styles.
- Removed obsolete LiveView settings from existing 0.9.7.74 Community databases during startup.

### Release readiness
- Added regression coverage so LiveView cannot accidentally return to the Community edition.
- Community documentation and advertised feature scope now match the intended edition boundary.

## 0.9.7.74 — Community release candidate

### Community edition
- Neutral first-run wizard for organization, SMTP, tariff, optional charging credit and first-user invitation.
- Removed employer-specific weekly-hours and automatic employee-budget rules.
- Neutral fresh-install defaults without DRK-specific branding, load-management seeds or historical billing rewrites.
- Public charging portal restored with PIN login/reset, RFID self-service, bonus, vouchers, achievements, rankings and session history.
- Public registration restored with email verification, configurable registration settings, admin approval and portal onboarding.
- Restored Smart Charging, Engagement, Cost Centers, Lade.cloud import and read-only Fleet Integration APIs.
- Restored per-user charging portal administration.
- Community-specific Docker Compose, Portainer stack, branding, PWA assets and update source.
- Added Community CI and QA smoke tests including fresh-install, container health, first-run, route/template integrity and unit tests.

### Release readiness
- Community release archive includes Docker/Portainer files, environment example, documentation and bilingual README files.
- GitHub Releases use numeric tags compatible with the in-app update checker, for example `v0.9.7.74`.
