# VoltCore Community

[English](README.md) | [Deutsch](README.de.md)

Private development repository for the future **VoltCore Community** edition.

> Status: work in progress. Not yet intended for public use.

VoltCore Community will provide a solid self-hosted OCPP 1.6J management base with charging stations, connectors, RFID, users, vehicles, sessions, dashboard, basic LiveView, reports, backups, updates, security, audit/system status, notifications, PWA support, dark mode and basic branding.

The Community edition is being derived from the private VoltCore development baseline. Non-Community features are removed from the Community source rather than merely hidden.

## Languages

VoltCore Community is designed as a multilingual application from the start.

Initial languages:

- English
- German

The UI uses translation keys and locale files instead of hard-coded interface text wherever practical. Browser language detection is supported, English is the fallback language, and a manual language choice can override automatic detection.

## Development

- `main`: future stable Community releases
- `develop`: current Community integration branch
- Update source: GitHub Releases of this repository
- Repository visibility: private until the first public-ready Community release

See `docs/COMMUNITY_SCOPE.md` for the fixed initial scope.
