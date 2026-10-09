# Entwicklung und GitHub-Workflow

## Branch-Modell

Das Projekt verwendet bewusst nur zwei dauerhafte Branches:

- `main` – stabiler, veröffentlichbarer Stand
- `develop` – laufende Entwicklung und Integration

Ein zusätzlicher permanenter `release`-Branch ist nicht nötig. Ein Release ist bei GitHub ein **Tag + Release** auf einem stabilen Commit in `main`.

Für größere Einzelthemen können temporäre Branches verwendet werden:

```text
feature/rfid-enrollment
fix/sidebar-overflow
chore/production-cleanup
```

Diese werden nach `develop` gemergt und anschließend gelöscht.

## Was gehört wohin?

- Produktionscode: `app/`
- Aktuelle Tests: `tests/`
- Entwicklerwerkzeuge: `dev/`
- Dokumentation: `docs/`
- Release-Helfer: `scripts/`
- Automatisierung: `.github/workflows/`

Historische QA-Dateien und alte versionierte Testkopien werden nicht im aktuellen Baum aufgehoben. Git selbst ist die Historie.
