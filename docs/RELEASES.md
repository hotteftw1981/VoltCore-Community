# Release-Prozess

`main` ist der stabile Release-Stand. Ein Release erhält einen Tag wie `v0.9.7.36`. Die README ist Teil jedes Release-Abschlusses und wird nicht mehr separat „irgendwann später“ nachgezogen.

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

Das Skript verwendet eine feste Allowlist. Tests, GitHub-Dateien und `dev/simulator/` landen nicht im Portainer-ZIP.

Produktionspakete werden ab V0.9.7.51 neutral als `VoltCore_V<APP_VERSION>_Portainer_FINAL.zip` benannt. Ab V0.9.7.67 ist `ghcr.io/hotteftw1981/voltcore` der primäre Containerpfad; der frühere Pfad bleibt während der Migration parallel als Legacy-Kompatibilität erhalten.

## Container-Release / Portainer

Die Veröffentlichung läuft bewusst in einer festen Kette:

```text
CI (main) → Container → Release
```

Erst wenn die CI für den betreffenden `main`-Commit erfolgreich abgeschlossen ist, baut `.github/workflows/container.yml` das Produktionsimage und veröffentlicht es in GitHub Container Registry. Erst nach erfolgreichem Container-Build startet anschließend der Release-Workflow mit Regressionstest, Produktions-ZIP, Tag und GitHub Release.

Veröffentlichte Tags:

```text
ghcr.io/hotteftw1981/voltcore:latest
ghcr.io/hotteftw1981/voltcore:<APP_VERSION>
ghcr.io/hotteftw1981/voltcore:v<APP_VERSION>

Legacy-Übergang:
ghcr.io/hotteftw1981/drk-ocpp-backend:latest
```

`docker-compose.portainer.yml` verwendet standardmäßig `latest` und `pull_policy: always`. Ein Rollback kann durch Setzen von `OCPP_IMAGE` auf einen konkreten Versionstag erfolgen.
