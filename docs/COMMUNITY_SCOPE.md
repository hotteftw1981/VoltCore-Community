# VoltCore Community - Initial Scope

This document records the agreed initial feature scope for VoltCore Community.

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

1. organization / installation name and basic branding;
2. administrator basics and preferred language;
3. optional SMTP / email setup;
4. invite first users by email;
5. basic tariff / price configuration;
6. basic charging defaults;
7. explicit choice whether free charging credit / charging budgets should be enabled.

Community defaults:

- weekly working hours are **not** part of the Community user profile;
- free charging credit / monthly charging budgets are **disabled by default**;
- no organization- or employer-specific assumptions;
- optional features must be deliberately enabled during first-run setup or later in settings.

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

The Community edition retains neutral modules such as Smart Charging, Cost Centers, imports, and read-only Fleet Integration. Engagement (achievements, XP, events, leaderboards, rewards, bonuses, vouchers), the personal PIN charging portal, and RFID self-service are NOT Community features. The backend is the only account-based management area. Historical tables may remain for safe migrations, but Community must never re-enable the removed features.

This repository remains private while the first Community build is being prepared and verified.

## P02 security and scope regression contract

- Creating and approving charging users must not create a portal PIN or activate gamification.
- Admin rights are required for write requests to charging users, RFID, vehicles and charge points.
- Viewer accounts cannot write operational data; regular backend accounts can read permitted operational data but cannot mutate those management collections.
- The personal PIN portal, its API routes and the engagement/bonus management routes must not exist.
- Legacy database rows are preserved during migration rather than destructively dropped; portal and XP flags are disabled on Community database initialization.
- Before a release, the P02 automated checks and Community CI must pass. No Pro-only modules should be revived by merges.
