# P04 RFID Synchronisation – technische Abnahme

## Implementiert
- Station-/Backend-Versionen werden getrennt ausgewertet. Unbekannte Stationswerte bleiben unbekannt.
- Erfolgreiches SendLocalList wird erst nach GetLocalListVersion als versionsgleich bestätigt.
- Die Zahl gesendeter RFIDs gilt nicht als ausgelesener Stationsbestand.
- Automatische Wiederholungen nach Timeout, Fehler oder unbestätigter Übertragung sind für 120 Sekunden gedrosselt. Manueller Vollabgleich bleibt möglich.
- Die Übersicht zeigt Diagnosecodes für unbekannt, nicht unterstützt, Versionskonflikt, Prüfung erforderlich und Versionsgleichstand.
- Identische Regressionstests und Implementierungen in Community und Pro.

## Restrisiko und Voraussetzungen für Produktivfreigabe
- Die OCPP-1.6-Schnittstelle bestätigt einen Versionsstand, aber nicht zwingend jede einzelne gespeicherte RFID.
- Statusmeldungen und LocalList-Verhalten müssen an Amtron und Amedio vor Ort geprüft werden, inklusive Antwort -1, Offline, Reconnect und SendLocalList-VersionMismatch.
- Anzeige, Modals und DE/EN-Texte brauchen eine abschließende Browser-Abnahme.
- P03 ist eine Abhängigkeit; Pull-Requests werden nicht isoliert nach main gemergt.
- Vor einem Upgrade Datenbank sichern und Restore testen.

## Status
P04 kann nach grüner CI als technisch implementierter Entwicklungsstand betrachtet werden. Ohne Praxis-/Browser-Abnahme kein Produktiv-Release.
