# Changelog

All notable changes to VoltCore Community are documented here.

## 0.9.7.76 — Backend-only Community scope

### Removed
- Removed the personal charging portal and all PIN login/reset flows.
- Removed public charging-user registration and access-request workflows.
- Removed charging-user RFID self-service and replacement-request surfaces.
- Removed Community-inappropriate UI/routes for Achievements, Events, leaderboards, bonus/vouchers, load management, cost centers, lade.cloud imports and Fleet Integration.

### Changed
- Community is now explicitly defined as an administrative backend only.
- Charging users, RFID cards and vehicles are managed exclusively by authorized backend users.
- Kept core live charging state and diagnostics independent from removed advanced modules.

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
