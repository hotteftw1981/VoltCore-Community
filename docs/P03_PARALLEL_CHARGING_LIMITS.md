# P03 — Parallel charging limits (Community + Pro)

Status: design contract and implementation checklist; **not yet enforced in production**.

## Goal
Allow administrators to set how many simultaneously active charging sessions may use one RFID and how many may belong to one charging user. The same safety policy and database behavior must be implemented in both Community and Pro.

## Rules
- Each RFID card and each charging user can have a configurable maximum of concurrent sessions. Default is **1 per RFID** and **1 per user** for new installations; existing installations require a non-destructive migration and explicit review of pre-existing concurrent use before rollout.
- An administrator may configure higher limits; no arbitrary Pro-versus-Community restriction.
- Enforce **both** limits when the card belongs to a user. When an RFID is not assigned to a user, enforce the card limit alone. A denied session must not consume a slot.
- Count only active, non-ended transactions, across all charging points and connectors. Ending a transaction releases its slot, independent of connector offline/status.
- Check the limits **atomically with session creation**, within a serialized SQLite write transaction. An authorize-only check cannot reserve a slot: re-check when StartTransaction is committed.
- Handle repeated StartTransaction/reconnections idempotently; never count an existing transaction twice.
- Denials need a clear machine-readable reason, a user-facing explanation and an audit entry. No silent fallback to a second session.
- When a card/user limit is reduced below currently active sessions, do not terminate existing sessions. Reject additional starts until the number drops below the new limit.
- RemoteStart and local OCPP StartTransaction must use the same effective policy. A pending RemoteStart alone should not consume a charging slot.
- Concurrent session ownership must remain attached to the original user/card even if that assignment is changed mid-session.
- Preserve history, identifiers and old settings on migration. Test fresh databases and upgrades.

## UI
- RFID detail: “Max. gleichzeitige Ladevorgänge” (integer >=1), concise info tooltip and current active count.
- User detail: equivalent limit and active count across all of their RFIDs.
- Clearly explain that the *lower effective remaining allowance* across the two limits governs a start.
- Use shared German/English i18n keys; respect read-only role. Editing requires admin rights.
- No personal PIN portal, self-service RFID portal or engagement features in Community.

## Tests / acceptance gates
1. One RFID used on two connectors with default limit 1: exactly one start accepted, including simultaneous starts in separate threads/processes.
2. Same RFID with limit 2 and user limit 2: two different connectors accepted; third rejected.
3. Two RFIDs owned by one user with user limit 1: just one active session accepted.
4. RFID limit 1, user limit 3: same RFID may still charge only once, other user cards can use remaining allowance.
5. Stop/abort/error/restart: slots released only on legitimately ended transaction, not prematurely on disconnection.
6. Changes to user/card assignment during a session cannot bypass limits; duplicate transaction messages cannot duplicate a session.
7. Both editions run an equivalent policy test suite. Live OCPP tests cover Authorize, StartTransaction, RemoteStart and multi-connector behavior.

## Delivery checklist
- [ ] DB migration + repository-level atomic start guard
- [ ] OCPP response/error wiring and audit
- [ ] Admin APIs and settings validation
- [ ] User/RFID UI and translations
- [ ] Regression suites in both editions + CI
- [ ] Backup / restore and migration checks
- [ ] Version, changelog, release notes, ZIP and GitHub releases

P03 branches were created from `fix/p02-scope-and-access-control` to preserve P01/P02 without prematurely promoting them to `main`.
