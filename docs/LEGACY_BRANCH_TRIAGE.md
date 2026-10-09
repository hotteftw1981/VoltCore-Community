# Legacy branch consolidation — status 2026-10-09

## Completed
- Community `v0.9.7.91` merged neutral CSV import and migration package APIs/UI/tests into `main` while retaining the lade.cloud importer.
- Community `v0.9.7.92` corrected public token-free release-check wording and documented manual installation for Docker/Portainer.
- Legacy updater agent `updater/updater.py` was deliberately **not** integrated; it assumes Docker/Compose control and needs a separate design and security review.
- Release workflows are triggered from current `main`. Obsolete P01–P12 branch triggers were removed.

## Preservation and housekeeping
- Keep `main` (live deployment) and `develop` until explicit branch policy is agreed.
- Other old branches (including already-merged feature branches) can be retired **only after a complete private backup of refs and history**. Deleting a branch does not remove old commits from GitHub's history.
- Retain the working historical tags/releases until the public history strategy is carried out; they point to older content.
- Do not make this existing repository public until historical objects, old tags/branches, secrets and third-party rights are cleared.

## Remaining publication blockers
1. Back up all reachable Git refs (branches/tags) privately.
2. Review historical contents for sensitive/Pro-only or nonredistributable material and resolve any hits; reconstruct the public history within the **same** repository only with deliberate migration planning.
3. Confirm rights and third-party redistribution notices; then change GitHub repository visibility manually in repository settings.
