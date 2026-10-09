# VoltCore Community: Release-Prozess

## Gültiger Stand
- Stabile Version steht auf `main`; Entwicklungsarbeiten laufen über Pull Requests.
- Die Versionskennung steht in `app/main.py` als `APP_VERSION`.
- Nach erfolgreicher Community-CI auf `main` baut der Container-Workflow das Community-Image. Der Release-Workflow veröffentlicht den zur selben Version passenden Tag und das Community-ZIP.
- Offizielle Releases: https://github.com/hotteftw1981/VoltCore-Community/releases
- Container: `ghcr.io/hotteftw1981/voltcore-community:latest` und versionierte Community-Tags.

## Release-Dateien und Lizenz
Das ZIP wird mit `python scripts/build_release.py` aus einer Allowlist erstellt. `LICENSE` (AGPLv3) und `CONTRIBUTING.md` gehören hinein. Der öffentliche Einsatz setzt eine abgeschlossene Rechte- und Historienprüfung voraus.

## Docker Compose / Portainer
- Die dokumentierte Community-Installation erfolgt über `docker-compose.yml` oder `docker-compose.portainer.yml`.
- Vor jedem Upgrade ein Datenbackup erzeugen.
- Bei einem Portainer-Git-Stack `refs/heads/main` als Repository-Referenz verwenden und anschließend `Pull and redeploy` ausführen.
- Für Docker Compose entsprechend der Installationsanleitung Images neu erstellen/ziehen und den Stack neu starten.
- **Ein-Klick-Updates aus dem Backend sind nicht aktiviert**. Release-Erkennung ist für öffentliche GitHub-Repositories ohne Token möglich, private Repositories benötigen einen lesenden GitHub-Token.

## Trennung zu anderen Editionen
Diese Anleitung gilt ausschließlich für VoltCore Community. Containerpfade und Deployments anderer Editionen gehören nicht in diese Release-Dokumentation.
