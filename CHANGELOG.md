# Changelog

All notable changes to VoltCore Community are documented here.

## 0.9.7.76 — Backend-only Community scope

### Removed
- Removed the public personal charging portal, PIN login/reset and all portal administration.
- Removed RFID self-service / enrollment while keeping normal RFID administration in the backend.
- Removed achievements, XP/levels, events, rankings, bonus kWh, vouchers and transfers.
- Removed Smart Charging / load management, cost centers, Lade.cloud import and Fleet Integration APIs.
- Removed public registration / access requests and their onboarding workflow.
- Removed employer- and organization-specific charging-user fields such as weekly working hours, departments and charging-user roles.
- Removed the legacy free-text vehicle driver field; user-to-vehicle assignment remains available through the neutral assignment model.

### Kept
- Neutral backend charging-user management, RFID management, vehicles and user/vehicle/RFID assignment.
- Optional manually configured monthly kWh limits, disabled by default on fresh installations.
- Tariffs, billing groups, reports, CSV/PDF export, backup/restore, branding, PWA, security and system-user roles.
- OCPP 1.6J monitoring, diagnostics, remote commands and charge-point onboarding.

### Fixed
- The optional first-run default monthly kWh limit is now actually applied to newly created charging users while explicit unlimited users remain possible.
- Offline RFID LocalList authorization is re-evaluated once on month change so block-mode monthly limits do not remain stale across billing months.
- Removed orphaned registration-signature code, unused runtime imports and unreferenced database helper functions left behind by the Community scope reduction.

### Release readiness
- Existing Community databases clean up obsolete portal, registration, gamification, bonus, load-management, cost-center and integration state on startup.
- The short-lived `community_free_credit_enabled` setting is migrated to the neutral default-limit setting name.
- Regression coverage locks the backend-only edition boundary and runtime cleanup.
- Documentation and smoke tests now describe the actual Community runtime instead of the temporary 0.9.7.74 restoration state.

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
