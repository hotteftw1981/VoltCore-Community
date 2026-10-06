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
- Basic LiveView
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

## Update model

VoltCore Community checks GitHub Releases in this repository for newer stable releases.

The application should:
1. check on backend startup;
2. re-check periodically with a conservative interval;
3. provide a manual "check now" action;
4. show release notes before installation;
5. install only releases intended for VoltCore Community;
6. preserve database migrations and rollback safety.

## Explicitly not implied by this scope

Features not listed above are not automatically part of Community. The initial Community build is intentionally limited to the agreed scope.

This repository remains private while the first Community build is being prepared and verified.
