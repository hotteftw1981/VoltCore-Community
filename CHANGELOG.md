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
- Community billing now uses metered kWh multiplied by the transaction's stored tariff. Unlimited charging is not implicitly free; monthly limits do not subtract free credit.
- Missing or invalid energy/tariff values remain unknown instead of being reported as free charging. An explicitly configured zero tariff still produces zero cost.
- Billing rounding uses decimal half-up cents. No bulk recalculation of historical transactions is introduced.
- Restored 33 shared backend helpers for audit logging, notifications, diagnostics, web push and session/security summaries that were accidentally removed alongside Fleet integration. No excluded edition modules were restored.
- Fixed an obsolete variable reference in the first-run audit entry that caused HTTP 500 after saving the wizard.

### Release readiness
- Existing Community databases clean up obsolete portal, registration, gamification, bonus, load-management, cost-center and integration state on startup.
- The short-lived `community_free_credit_enabled` setting is migrated to the neutral default-limit setting name.
- Regression coverage locks the backend-only edition boundary and runtime cleanup.
- Documentation and smoke tests now describe the actual Community runtime instead of the temporary 0.9.7.74 restoration state.
- Block 3D: 20 isolated SQLite billing regression tests passed locally and on GitHub Actions; Python compilation of app and tests passed on GitHub. This is not a full application-startup or end-to-end certification.
- Block 3E adds a read-only Docker runtime QA workflow: fresh administrator setup, first-run configuration, authenticated pages/APIs, explicit negative authorization tests, removed-route checks and persisted data after restart.
- Positive runtime-contract tests now detect missing shared database APIs and stale first-run variable references, complementing the existing feature-removal checks.
- Runtime QA uses disposable loopback-only instances and random temporary credentials. Only external update checks are disabled through the existing persisted setting; production update behavior is unchanged. Browser interaction, physical chargers, external mail/push delivery and upgrade/restore scenarios require separate validation.
- Block 3F exercises real authenticated Admin/User/Viewer sessions. The writer can mutate operational charging data but is blocked from administration, while Viewer remains read-only except for its own account/notification state; role assignments and boundaries are rechecked after an actual container restart.
- Block 3G is complete: Viewer write affordances and direct edit entry points are hidden and client-guarded, charge-point policy controls are truly read-only, admin navigation remains role-gated, mobile Viewer layouts no longer leave empty action rows, and Dark Mode/mobile shell contracts remain protected. Community terminology uses neutral monthly-limit wording instead of charging-budget language.

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
