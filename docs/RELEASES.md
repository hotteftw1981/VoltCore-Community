# Release-Prozess

`main` ist der stabile Release-Stand. Ein Release erhält einen Tag wie `v0.9.7.78`. Die README ist Teil jedes Release-Abschlusses und wird nicht mehr separat „irgendwann später“ nachgezogen.

## Vor einem Release

- aktueller Stand in `develop`
- CI auf `develop` grün
- Pull Request `develop` → `main`
- CI im Pull Request grün
- Merge nach `main`
- Versionsnummer in `app/main.py`, README und Changelog prüfen
- README-Kopfzeile und „Aktueller stabiler Stand“ müssen exakt zu `APP_VERSION` passen; der Release-Workflow bricht bei Abweichung automatisch ab

## Produktionspaket lokal bauen

```bash
python scripts/build_release.py
```

Das Skript verwendet eine feste Allowlist. Tests, GitHub-Dateien und `dev/simulator/` landen nicht im Produktions-ZIP. Der für Docker-Compose-1-Klick-Updates benötigte `updater/`-Dienst ist Bestandteil des Pakets.

Community-Produktionspakete werden als `VoltCore_Community_V<APP_VERSION>.zip` gebaut. Der offizielle Community-Containerpfad ist `ghcr.io/hotteftw1981/voltcore-community`.

## Container-Release / Portainer

Die Veröffentlichung läuft bewusst in einer festen Kette:

```text
vollständige CI (main) → Container → Release
```

Die Community-CI ist das harte Release-Gate: Unit-Tests, kompletter First-Run, Admin/User/Viewer-Rechte, Negativtests, echter Container-Neustart mit Persistenzprüfung sowie Bau und Prüfung des Release-ZIPs müssen erfolgreich sein. Erst danach baut `.github/workflows/container.yml` das Produktionsimage und veröffentlicht es in GitHub Container Registry. Nur wenn auch dieser Container-Workflow erfolgreich war, startet `.github/workflows/release.yml` mit erneutem Regressionstest, Produktions-ZIP, Tag und GitHub Release.

Veröffentlichte Tags:

```text
ghcr.io/hotteftw1981/voltcore-community:latest
ghcr.io/hotteftw1981/voltcore-community:<APP_VERSION>
ghcr.io/hotteftw1981/voltcore-community:v<APP_VERSION>
```

`docker-compose.portainer.yml` verwendet standardmäßig `latest` und `pull_policy: always`. Ein Rollback kann durch Setzen von `VOLTCORE_COMMUNITY_IMAGE` auf einen konkreten Versionstag erfolgen.

Ein veröffentlichter Versionsstand ist unveränderlich: Existiert z. B. bereits `v0.9.7.78`, bricht der `main`-Container-Workflow vor dem Push ab und auch der Release-Workflow verweigert ein zweites Release mit derselben Version. Für jede weitere Veröffentlichung muss `APP_VERSION` erhöht werden. Dadurch können ZIP, GitHub Release und die versionierten Container-Tags nicht unbemerkt auseinanderlaufen.
