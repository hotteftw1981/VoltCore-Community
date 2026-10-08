# VoltCore Community - Initial Scope

This document records the agreed initial feature scope for VoltCore Community.

VoltCore Community is intentionally **backend-only**. Charging users do not receive a personal login, PIN portal or self-service area. Administration happens in the authenticated VoltCore backend.

## Included

- OCPP 1.6J
- Charge point discovery / onboarding
- Connector status
- Live charge point status
- Start / stop of a charging session
- Basic remote commands
- RFID management
- User management
- Basic vehicle management
- Charging session history
- Basic user / vehicle / RFID assignment
- Dashboard
- Basic reports
- CSV export
- Backup / restore
- In-app update menu
- Dark mode
- Basic branding such as name and logo
- Security baseline
- Roles: Admin / User / Read-only
- System status
- Basic logs / audit
- Basic notifications
- PWA baseline

## First-run onboarding

VoltCore Community guides a new installation through a neutral first-run setup instead of inheriting organization-specific defaults.

Current first-login wizard:

1. organization / installation name;
2. optional SMTP / email setup;
3. optional invitation of the first additional system user;
4. optional basic tariff / price configuration;
5. explicit choice whether a neutral default monthly charging limit should be enabled.

Community defaults:

- charging users have no employer-specific role, department or weekly working hours;
- monthly charging limits are **disabled by default** and remain optional;
- there is no DRK-, employer- or workforce-specific budget logic;
- system-user roles remain Admin / User / Read-only and are separate from charging users.

## Update model

VoltCore Community checks GitHub Releases in this repository for newer stable releases.

The application should:
1. check on backend startup;
2. re-check periodically with a conservative interval;
3. provide a manual "check now" action;
4. show release notes before installation;
5. install only releases intended for VoltCore Community;
6. preserve database migrations and rollback safety.

## Scope notes

LiveView / Kiosk is explicitly **not part of VoltCore Community**. This applies to all former variants, including Standard, Pro and People.

The following modules are explicitly **not part of VoltCore Community**: personal charging portal / PIN access, RFID self-service, achievements / gamification / events / rankings, bonus and voucher logic, Smart Charging / load management, cost centers, the provider-specific Lade.cloud XLSX importer, Fleet Integration APIs and public registration / access requests.

Normal backend RFID management, charging-user management, vehicles, tariffs, billing groups, reports, the provider-neutral CSV migration workspace, versioned VoltCore migration export/import packages and optional manually configured monthly kWh limits remain part of Community.

This repository remains private while the first Community build is being prepared and verified.
