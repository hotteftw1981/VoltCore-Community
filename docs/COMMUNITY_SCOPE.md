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

1. installation / organization name and basic branding;
2. optional SMTP / email setup;
3. optional invitation of another backend user;
4. optional basic tariff / price configuration;
5. explicit opt-in for a monthly free charging allowance.

Community defaults:

- weekly working hours are **not** part of the Community user profile;
- the monthly free charging allowance is **disabled by default**;
- optional per-user monthly limits are usage/access limits and do **not** imply free energy;
- without an explicitly enabled free allowance, tariff costs apply to all charged energy;
- no DRK- or employer-specific assumptions.

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

VoltCore Community is a **backend-only** charging-management system. Charging users do not receive a separate public or personal frontend.

Explicitly not part of VoltCore Community:

- LiveView / Kiosk, including Standard, Pro and People;
- personal charging portal or PIN login;
- public user registration / access requests;
- RFID self-service for charging users;
- Achievements, XP, Events and leaderboards;
- bonus kWh, vouchers and user-to-user credit transfers;
- Smart Charging / load management;
- cost-center and billing-group modules;
- lade.cloud import;
- Fleet Integration API.

Charging users, RFID cards and vehicles are managed by authorized backend users.

This repository remains private while the first Community build is being prepared and verified.
