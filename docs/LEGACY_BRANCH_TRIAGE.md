# Legacy Community branches: consolidation decision (2026-10-09)

## Confirmed
- The current release line is `main` / v0.9.7.90.
- The old `feature/community-migration-export-0.9.7.79` branch contains files not present on `main`: `app/csv_import.py`, `app/migration_package.py`, an `updater/` agent, more integration/regression tests and a runtime QA workflow.
- Neither `app/main.py` nor `app/updates.py` on current `main` imports these modules. Copying the three files alone **would not implement** the import/export/updater features.
- The updater agent proposes control of Docker/Compose and therefore requires a dedicated security/design review; do not blindly restore it into the public Community product.
- Do not delete the branches holding these sources until a replacement implementation, a documented rejection or an archival snapshot exists.

## Next implementation batch
1. Port and integrate **neutral CSV import/export** as a separate functional change. Keep provider mapping, validation, dry-run, permissions, duplicate handling and tests together.
2. Port **migration package** generation/import as a separate change with integrity, retention and restore testing, without exposing private Pro-only data.
3. Review update-agent approach against deployment-neutral Docker/Portainer constraints before considering code integration. Prefer a nonprivileged documented install flow over Docker host-control by default.
4. Reuse relevant legacy unit and runtime tests when integrating corresponding behavior.
5. After functionality is verified, archive or delete superseded legacy feature/fix/test branches. Keep `main`, `develop`, releases/tags.

## Publication
This list does not certify Git history safe for public visibility. Do not confuse deleting branches with purging reachable historical sensitive data.
