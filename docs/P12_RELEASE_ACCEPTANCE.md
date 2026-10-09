# P12 release acceptance matrix

Code-level gate: full P03-P11 regression suite on the P12 branch in both editions, including P05 connector integrity. Passing unit tests alone does not establish live readiness.

Required outstanding checks: real Amtron/Amedio station charging/reconnect tests; live RFID local-list capability and readback; full browser testing, both locales, roles and mobile; backup-and-restore rehearsal on a separate installation; fresh Compose/Portainer setup; session/report validation; security and HTTPS/WSS verification.

Do not merge chained draft branches or publish versioned ZIPs/releases until dependent packages and field acceptance are confirmed. Preserve backups, rollback points and historic charging data.
