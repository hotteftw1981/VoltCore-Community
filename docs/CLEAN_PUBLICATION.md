# Clean public Community repository procedure

This is a **preparation plan**, not approval to make the existing private repository public.

## Why not change visibility on the existing private repository?
The private repository includes historical development branches and commits. Removing branches alone does not guarantee that sensitive content is absent from old commits, releases, caches or references. Do **not** switch its visibility to public.

## Safe publication route
1. Finish ownership, third-party-licenses, privacy, history and public configuration reviews in `docs/PUBLICATION_RIGHTS_CHECKLIST.md`.
2. Select a reviewed **single Community-only source snapshot**. Verify required files, branding, no DRK references, no credentials, no production data and no proprietary Pro/Cloud modules. Verify `LICENSE`, `CONTRIBUTING.md`, bilingual README and Compose.
3. Prepare a **new empty repository**, or a fresh orphan history in a separate destination, from the reviewed snapshot. Do not import any private branch, commit history, tag or release from the development repository.
4. Review all content in the new destination. Create its initial commit, a new version tag and an appropriately labeled **release candidate**, with checked ZIP/archive and image if applicable.
5. Update the app's `UPDATE_GITHUB_REPOSITORY`, installation docs and GHCR paths only if the final public repository name differs from `hotteftw1981/VoltCore-Community`.
6. Verify anonymous downloads and updates after visibility is changed, and only then announce the public project.
7. Keep the existing private development repository, Portainer test stack and Pro/Cloud repositories intact until a separately approved migration.

## Important limitation
A source-only ZIP exported from a reviewed working tree does not include old Git commit history. The existing private GitHub Release ZIP `v0.9.7.90` does **not** yet contain the proposed `LICENSE` and `CONTRIBUTING.md` introduced on this preparation branch: do not republish it unchanged as the first AGPL distribution.

## Branch housekeeping
Retain development branches until their contents/history have been checked and archival needs resolved. Do not delete or force-push branches as a substitute for creating clean history.
