# P01 - isolierte Regressionstest-Basis

Status: Testpaket auf Draft-PR, keine Produktionseingriffe. Basis: stabiler `main`-Stand bei Erstellung des Test-Branches; kein automatischer Merge.

## Geltungsbereich

- Tests verwenden eigene, temporaere SQLite-Datenbanken und simulierte Connectoren.
- Keine Verbindung zu realen Ladestationen, kein Zugriff auf produktive Docker-Volumes.
- Produktionscode bleibt in P01 unveraendert.
- Jede Edition wird eigenstaendig getestet.

## Testgruppen

| Datei | Umfang |
|---|---|
| `tests/test_ocpp_regression_baseline.py` | RFID, Mehrfach-Connector, OCPP Start/Stop, verspätete Messwerte und doppelte Stop-Nachricht |
| `tests/test_p01_ocpp_edges.py` | Reconnect-Rennen, fehlerhafte Connector-Zuordnung, parallele RFID-Freigabe |
| `tests/test_p01_role_matrix.py` | Rechtepruefung fuer Viewer, Backend-Benutzer und Administratoren |\n| `tests/test_p01_parallel_transactions.py` | Gleichzeitige Sessions und Messwert-Updates; verbleibender Connector bei Stop |
| `tests/test_p01_backup_restore.py` | Backup-Snapshot, ZIP-Pruefung, Secrets-Ausschluss, Restore, Kopierfehler mit simuliertem Rollback-Test |

## Interpretation der Ergebnisse

- `ok`: aktuelles Verhalten erfuellt die gepruefte Erwartung.
- `expected failure`: bekannte unbehobene Schwachstelle oder noch nicht implementierte Sicherheitsregel. **Kein bestandener Funktionstest.**
- `unexpected success`: alte Defektannahme ist zu pruefen. Wenn eine Erwartung bereits erfuellt ist, wird sie als regulärer Regressionstest gefuehrt.
- Jede Korrektur muss die entsprechende `expectedFailure`-Markierung entfernen und den Test regulär bestehen lassen.
- Testbedingte Import-, Pfad- oder Rechtefehler duerfen nicht als Produktfehler gezählt werden.

## Ausfuehrung

```bash
python -m unittest discover -s tests -p 'test_ocpp_regression_baseline.py' -v
python -m unittest discover -s tests -p 'test_p01_*.py' -v
```

Die Community-CI entdeckt die neuen Tests automatisch ueber die vorhandene unittest-Discovery.

## Offene Risiko- und Produktpakete

- **P02:** Berechtigungsmatrix und Community-Scope; Zugriffsschutz fuer personenbezogene Daten.
- **P03:** OCPP-Reconnect-Zuordnung, fehlerhafte Station-/Connector-Korrelation, transaktionsfeste Messwerte, idempotente Stop-Bearbeitung.
- **P04:** konfigurierbare gleichzeitige Ladevorgänge pro Benutzer/RFID mit atomarer Freigabe; Offline-Einschraenkungen beachten.
- **P05:** gestufter Restore mit automatischem Rollback, getrennte Daten- und Datei-Konsistenz, Backup-Retention und geschuetzte Importdaten.

**Freigaberegel:** Kein Merge, solange eine neu hinzugefuegte Regression unerwartet fehlschlaegt. Stabile Editions-Branches und Live-Ladevorgaenge bleiben unberuehrt.
