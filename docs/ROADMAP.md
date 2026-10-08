# VoltCore Community Roadmap

This roadmap collects post-release ideas that are intentionally **not** part of the 0.9.7.77 release candidate. The current release remains focused on a stable backend-only Community edition.

## UI / information architecture

- Rework navigation into clearer operational groups such as **Operation**, **People & Access**, **Analytics** and **System**.
- Use consistent filter chips and compact list views across charge points, sessions, users and vehicles.
- Offer list/card toggles where both dense administration and visual monitoring are useful.
- Standardize detail pages around predictable tabs such as **Overview**, **Sessions**, **Technology**, **Events** and **Settings**.
- Keep VoltCore's stronger live-status and diagnostics focus while reducing visual noise.
- Preserve first-class Dark Mode, mobile layouts and clear status badges.

## Sites and hierarchy

- Replace the simple location concept with a real hierarchical site model.
- Support structures such as **Organization → Site → Building → Level → Zone → Charge point**.
- Allow arbitrary nesting where it remains understandable.
- Make site hierarchy available to filters, reports, permissions and later load-management features.

## Users, RFID and permissions

- Add user/RFID groups.
- Allow charge permissions to inherit from groups and site nodes.
- Keep direct per-user/per-charge-point overrides for special cases.
- Preserve the distinction between charging users and backend/system users.

## Onboarding

- Add an optional **Getting started** card after a fresh installation:
  1. connect a charge point,
  2. create a charging user,
  3. add an RFID,
  4. perform a first test charge.
- Keep the onboarding dismissible and avoid turning normal administration into a permanent wizard.

## API

- Expand toward a complete versioned REST API for core resources.
- Cover charge points, connectors, sessions, users, RFID, vehicles, tariffs, reports and system status.
- Design API authentication and permissions independently from browser sessions.
- Keep future integrations such as Home Assistant, Grafana, DRK ONE and custom apps in mind.

## Tariffs and billing — later

- Extend tariffs beyond energy-only pricing.
- Consider energy, charging time, parking time and flat components.
- Support time windows, weekdays and connector/site-specific tariff assignments.
- Consider configurable grace periods and parking fees after charging ends.

## Long-term architecture

- Revisit Smart Charging only after the Community baseline is stable.
- Keep OCPI/roaming as a long-term CPO-oriented option, not a near-term Community requirement.
- Preserve manufacturer-neutral OCPP operation and strong diagnostics as VoltCore differentiators.

## Release discipline

New roadmap features must not be added to a release candidate that is already in final validation. Complete the current release first, then start roadmap work in isolated feature branches.
