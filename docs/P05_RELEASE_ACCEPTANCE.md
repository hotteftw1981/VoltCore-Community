# P05 OCPP diagnostic acceptance

## Implemented
- Read-only inspection of connector records and active transactions. Never terminates or changes an ongoing charging session.
- Detects duplicate active sessions on a connector, stale transaction references, Faulted/error-code reports, Charging without a backend transaction, Available with an active backend transaction, and sessions with missing connector records.
- Each finding includes severity, stable code, German and English description, and German troubleshooting advice.
- Existing GET /api/charge-points/{cp_id}/diagnostics returns an extra integrity section alongside existing health and history.
- Same logic in Pro and Community.

## Before release
- Run tests/test_p05_connector_integrity.py and full P03/P04 regression suite in each repository; retain test logs and CI status.
- Validate two MENNEKES Amtron and dual-connector Amedio behavior during live charging, reboot, reconnect, and delayed status notifications.
- Exercise browser diagnostics in German and English with admin, normal user, and viewer.
- Verify historical OCPP logs and that diagnostic requests cannot stop or reset sessions.
- Merge dependencies in order P03 -> P04 -> P05 only after respective acceptance.
- Create versioned ZIP and GitHub Releases only after release gates pass.

## Not included as an automatic repair
- Do not reset a charger or stop/reassign a session based on a diagnostic heuristic.
- Do not infer charger is offline solely from an old timestamp; check current WebSocket state before displaying live connectivity.
