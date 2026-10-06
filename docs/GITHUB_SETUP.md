# GitHub einmal sauber einrichten

Diese Anleitung ist absichtlich auf GitHub Desktop + GitHub-Webseite ausgelegt.

## Branch-Modell

- `main` = stabil / produktiv
- `develop` = laufende Entwicklung

Ein permanenter `release`-Branch ist nicht nötig. Releases werden als Tag + GitHub Release aus `main` erstellt.

## Normaler Ablauf

1. In `develop` arbeiten.
2. Committen und pushen.
3. Pull Request `develop` → `main`.
4. CI muss grün sein.
5. Mergen.
6. Unter GitHub → Actions → Release den passenden Tag starten.

Git selbst ist die Historie. Alte QA-Dateien und versionsspezifische Testkopien müssen nicht im aktuellen Repository-Baum liegen.
