# Changelog

All notable changes to VoltCore Community are documented here.

## 0.9.7.77 — One-click updates for Docker and Portainer

### Added
- Added real 1-click updates for Docker Compose through a bundled, authenticated updater sidecar. The application container itself never receives Docker socket access.
- Added 1-click Portainer updates for Community Edition and Business Edition through the authenticated Portainer REST API.
- Kept optional Portainer Business stack-webhook support as a convenience provider.
- Portainer API updates automatically discover the configured stack name, support an optional Environment ID when names are ambiguous, preserve stack environment variables and force an image re-pull/redeploy.
- Update providers install the exact released image tag `ghcr.io/hotteftw1981/voltcore-community:v<version>` whenever the deployment path supports explicit image selection.

### Safety
- Every 1-click update requires a successful local pre-update backup before deployment is triggered.
- Docker Compose updates isolate Docker socket access inside the updater sidecar and use a shared random bearer token that is never exposed through a host port.
- Portainer API keys and optional Business webhooks are stored as secrets under the persistent data directory.
- The update confirmation modal no longer closes when the backdrop is clicked.

### Release readiness
- The production ZIP now includes the updater service.
- CI validates both Compose definitions, builds the updater image, and rejects release archives missing the updater.
- Regression tests lock all three supported update providers: Docker Compose, Portainer API and optional Portainer webhook.

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

## 0.9.7.74 — Withdrawn Community release candidate

### Historical note
- This short-lived release candidate used a broader experimental scope that was withdrawn before the backend-only Community definition was finalized.
- It is retained in the changelog only as a migration boundary and must not be used as the current Community feature list.
- Neutral first-run defaults, Community-specific Docker/Portainer packaging, branding, PWA assets, update source and QA foundations originated in this candidate.
- Employer-specific defaults were already being removed during this transition.

### Release readiness
- Community release archive work began here with Docker/Portainer files, environment example, documentation and bilingual README files.
- Numeric release tags compatible with the in-app update checker were established here, for example `v0.9.7.74`.
- The authoritative Community scope is the current backend-only definition documented for 0.9.7.76 and later.
