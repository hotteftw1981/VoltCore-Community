# P12 Community release-candidate acceptance

## Automated checks
- The P12 CI workflow runs P02 Community scope checks and the listed P03-P11 regression modules, including connector integrity, charging limits, backups, reports and API contracts.
- Verified successful run for commit `246063a9188993f44ea33c992483b69f70349f32`: https://github.com/hotteftw1981/VoltCore-Community/actions/runs/37921077558
- A green unit-test workflow does **not** prove live hardware behavior, full browser compatibility, HTTPS/WSS correctness or production security.

## Completed manual checks (Community test instance, 2026-10-09)
- Separate Portainer source build using the P12 development branch; web UI opened and first-run setup completed.
- Global tariff created at 0.35 EUR/kWh and displayed correctly.
- Charging user and assigned RFID each saved a parallel charging limit of 2, verified after reopening forms.
- Manual backup created; restore command executed with an automatic pre-restore backup; user and RFID settings remained visible.
- Automatic backup scheduling settings saved with daily 03:00 and 30-day retention.
- Backup restoration to an *older*, distinguishable state and automatic execution at the scheduled time are **not yet demonstrated**.
- The source still reports app version 0.9.7.75; establish an intentional release-candidate version before publishing.

## Deliberately waived field testing
The operator does not have equipment/time available for additional Amtron/Amedio charging sessions for this Community release candidate. No new physical charging trials are required for this RC. Instead, document that OCPP compatibility derives from the established Pro implementation, code review and automated tests. **Do not claim live Community charging, reconnect, RFID local-list readback or vendor compatibility was manually verified.**

## Remaining release-candidate gates
- [ ] Review all dependent draft PRs and merge in dependency order; check that the final combined branch is exactly the tested source.
- [ ] Confirm no serious security, data-loss, unauthorized-access or billing defects are known; review public exposure and safe defaults.
- [ ] Verify resulting container boots and first-run path on the publishable commit, and check backup recovery/migration within available staging automation.
- [ ] Confirm release packaging, image provenance, and install/update docs match the actual publishing method.
- [ ] Set RC version/changelog and ensure GitHub releases, source archive and container image use matching identifiers.
- [ ] State clearly in RC release notes that fresh physical hardware tests were waived and deployment remains at operator risk.
- [ ] Keep the existing Community test stack and all Pro production data intact until an explicitly approved deployment.

## Non-blocking follow-up verification
- Full desktop/mobile DE/EN visual review and advanced RFID/OCPP physical readback.
- Scheduled backup execution after the first configured run.
- Restore to a deliberately changed prior state in an isolated installation.
- HTTPS/WSS reverse-proxy verification in each real-world deployment.
