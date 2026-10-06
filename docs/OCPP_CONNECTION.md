# 🔌 Ladepunkt mit VoltCore Community verbinden

[🇩🇪 Deutsch](OCPP_CONNECTION.md) · [🇬🇧 English](OCPP_CONNECTION.en.md)

<p align="center">
  <img src="../app/static/branding/voltcore-community-horizontal.svg" alt="VoltCore Community Edition" width="680">
</p>

> [!IMPORTANT]
> VoltCore Community spricht aktuell **OCPP 1.6 JSON über WebSocket**.  
> Der Ladepunkt muss das Subprotokoll **`ocpp1.6`** anbieten.

## ⚡ Kurzfassung

Wenn VoltCore Community unter der IP `192.168.1.50` läuft und dein Ladepunkt die ID `AMTRON-01` verwendet, lautet die OCPP-URL bei unverschlüsseltem WebSocket:

```text
ws://192.168.1.50:9000/AMTRON-01
```

Bei direktem TLS/WSS auf Port 9000:

```text
wss://192.168.1.50:9000/AMTRON-01
```

Bei einem Reverse Proxy mit öffentlichem Hostnamen und Standard-HTTPS-Port kann sie zum Beispiel so aussehen:

```text
wss://ocpp.example.org/AMTRON-01
```

**Die Ladepunkt-ID gehört immer als letzter Pfadbestandteil an die URL.** 🔌

---

# 🧩 Aufbau der URL

Das Schema ist:

```text
ws://<SERVER>:<OCPP-PORT>/<CHARGE-POINT-ID>
```

oder verschlüsselt:

```text
wss://<SERVER>:<OCPP-PORT>/<CHARGE-POINT-ID>
```

Beispiele:

| Situation | Beispiel |
| --- | --- |
| lokale IP, Standard-OCPP-Port | `ws://192.168.1.50:9000/AMTRON-01` |
| lokaler DNS-Name | `ws://voltcore.local:9000/AMTRON-01` |
| direkte WSS-Verbindung | `wss://voltcore.local:9000/AMTRON-01` |
| Reverse Proxy auf 443 | `wss://ocpp.example.org/AMTRON-01` |

## 🆔 Charge-Point-ID

VoltCore Community liest die ID direkt aus dem URL-Pfad.

Bei:

```text
ws://192.168.1.50:9000/AMTRON-01
```

ist die ID:

```text
AMTRON-01
```

Empfohlen sind einfache IDs mit:

- Buchstaben
- Zahlen
- Bindestrich `-`
- Unterstrich `_`

Zum Beispiel:

```text
AMTRON-01
AMEDIO_01
PARKHAUS-A-03
```

Die ID darf maximal **128 Zeichen** lang sein.

Nicht verwenden:

- `/`
- `\\`
- `?`
- `#`
- führende oder nachgestellte Leerzeichen

> [!TIP]
> Verwende für die ID möglichst genau die Kennung, die auch am Ladepunkt selbst angezeigt oder konfiguriert wird. Das macht Diagnose und spätere Zuordnung deutlich einfacher. 😄

---

# 🛰️ Was passiert bei der ersten Verbindung?

Wenn die aktuelle Sicherheitsrichtlinie unbekannte Ladepunkte zulässt:

1. 🔌 der Ladepunkt öffnet die WebSocket-Verbindung
2. 🆔 VoltCore liest die Charge-Point-ID aus der URL
3. 📡 die Station wird als neue Verbindung erkannt
4. 🟡 ein unbekannter Ladepunkt wird zunächst als **Pending / ausstehend** geführt
5. 📬 die Station sendet ihre OCPP-`BootNotification`
6. 🧾 Hersteller, Modell und weitere OCPP-Daten werden übernommen
7. ✅ anschließend kann der Ladepunkt im Onboarding freigegeben werden

Bereits bekannte und freigegebene Ladepunkte werden direkt als verbunden markiert.

---

# 📡 OCPP-Subprotokoll

VoltCore Community verwendet:

```text
ocpp1.6
```

Beim WebSocket-Handshake sollte die Säule deshalb senden:

```text
Sec-WebSocket-Protocol: ocpp1.6
```

Viele Ladepunkte setzen das automatisch, sobald **OCPP 1.6 JSON** ausgewählt wird.

Falls eine Station zwischen mehreren Varianten wählen lässt:

> ✅ **OCPP 1.6 JSON / WebSocket** auswählen  
> ❌ nicht OCPP 1.6 SOAP

---

# 🔓 WS oder 🔒 WSS?

## WS

`ws://` ist unverschlüsselt.

Für ein abgeschottetes lokales Netz kann das für Tests ausreichend sein:

```text
ws://192.168.1.50:9000/AMTRON-01
```

## WSS

`wss://` ist WebSocket über TLS.

Für Netze, in denen die Verbindung abgesichert werden soll, oder für öffentliche/externe Verbindungen ist WSS die richtige Wahl.

VoltCore Community kann TLS direkt am OCPP-Port verwenden, wenn Zertifikat und Schlüssel konfiguriert sind.

Relevante Variablen:

```text
OCPP_TLS_CERTFILE
OCPP_TLS_KEYFILE
```

Alternativ kann ein Reverse Proxy TLS terminieren.

---

# 🌐 Reverse Proxy

Ein Reverse Proxy kann eine externe WSS-Adresse bereitstellen und intern an VoltCore weiterleiten.

Beispiel außen:

```text
wss://ocpp.example.org/AMTRON-01
```

intern weiter an:

```text
ws://voltcore-community:9000/AMTRON-01
```

Wichtig:

- WebSocket-Upgrades müssen erlaubt sein
- `Upgrade` und `Connection` dürfen nicht entfernt werden
- der komplette Pfad inklusive Charge-Point-ID muss weitergereicht werden
- bei TLS-Terminierung am Proxy kann `TRUST_PROXY_HEADERS=1` notwendig sein, damit VoltCore den Transport als sicher erkennt
- `TRUST_PROXY_HEADERS` nur aktivieren, wenn der Proxy wirklich vertrauenswürdig ist

> [!WARNING]
> Einen öffentlich erreichbaren OCPP-Port niemals einfach ungeschützt ins Internet stellen. Für externe Verbindungen mindestens TLS/WSS und eine passende Authentifizierungsstrategie verwenden. 🛡️

---

# 🔐 OCPP Basic Auth

VoltCore Community unterstützt OCPP-Verbindungen mit HTTP Basic Auth.

Wenn für einen Ladepunkt ein OCPP-Secret konfiguriert und Authentifizierung verlangt wird:

- **Benutzername:** exakt die Charge-Point-ID
- **Passwort:** das für diesen Ladepunkt hinterlegte OCPP-Secret

Beispiel:

```text
Charge-Point-ID: AMTRON-01
Benutzername:     AMTRON-01
Passwort:         <hinterlegtes Secret>
```

Ist Authentifizierung vorgeschrieben und die Zugangsdaten fehlen oder stimmen nicht, wird der WebSocket-Handshake abgewiesen.

Mehrere fehlerhafte Anmeldeversuche können außerdem vorübergehend gebremst bzw. gesperrt werden.

---

# 🔒 Sicherheitsregeln, die eine Verbindung blockieren können

Abhängig von der späteren Security-Konfiguration kann VoltCore eine Verbindung ablehnen, wenn:

- die Charge-Point-ID ungültig ist
- das Subprotokoll `ocpp1.6` fehlt
- WSS/TLS vorgeschrieben ist, die Station aber per WS kommt
- unbekannte Ladepunkte abgewiesen werden
- Basic Auth erforderlich ist, aber fehlt
- Benutzername oder Secret falsch sind
- zu viele fehlgeschlagene OCPP-Anmeldeversuche erfolgt sind

Damit ist ein Verbindungsfehler nicht automatisch ein Netzwerkproblem. 🧠

---

# 🟢 Woran erkenne ich eine erfolgreiche Verbindung?

Typischer Ablauf:

1. WebSocket-Verbindung wird aufgebaut
2. VoltCore protokolliert **Connected**
3. die Station sendet **BootNotification**
4. VoltCore antwortet auf OCPP 1.6
5. der Ladepunkt erscheint im System
6. Status und Connectoren werden nach und nach aktualisiert

Im späteren Community-UI sollen dafür mindestens sichtbar sein:

- Ladepunkt online/offline
- Charge-Point-ID
- Hersteller und Modell
- Zeitpunkt der Verbindung
- Transport **WS / WSS**
- letzte OCPP-Nachricht
- Connector-Status
- Diagnosehinweise

---

# 🧪 Konkretes Beispiel

Server:

```text
192.168.111.20
```

Ladepunkt:

```text
MENNEKES-01
```

VoltCore läuft mit dem Standard-OCPP-Port `9000`.

Dann wird in der Säule eingetragen:

```text
ws://192.168.111.20:9000/MENNEKES-01
```

OCPP-Version:

```text
OCPP 1.6 JSON
```

WebSocket-Subprotokoll:

```text
ocpp1.6
```

Danach sollte der Ladepunkt VoltCore erreichen und bei einer neuen Installation zunächst im Onboarding auftauchen.

---

# 🧯 Fehlerbehebung

## ❌ Verbindung kommt gar nicht an

Prüfen:

- stimmt die IP / der Hostname?
- läuft VoltCore?
- ist Port 9000 veröffentlicht?
- blockiert die Firewall Port 9000?
- befinden sich Ladepunkt und Server im selben Netz bzw. ist Routing vorhanden?
- ist in der Säule wirklich OCPP 1.6 JSON aktiviert?

Docker:

```bash
docker compose ps
docker compose logs --tail=200 voltcore-community
```

## ❌ HTTP 400 / ungültige Ladepunkt-ID

Prüfe den Pfad.

Falsch:

```text
ws://192.168.1.50:9000/site/AMTRON-01
```

Denn VoltCore erwartet die Charge-Point-ID als **einen einzelnen Pfadwert**.

Richtig:

```text
ws://192.168.1.50:9000/AMTRON-01
```

## ❌ OCPP-Subprotokoll fehlt

Wenn die Security-Einstellung das Subprotokoll erzwingt, muss die Station `ocpp1.6` anbieten.

In der Säule deshalb nach Optionen wie diesen suchen:

- OCPP-J
- JSON
- WebSocket
- OCPP 1.6 JSON

## ❌ HTTP 401

Das weist typischerweise auf OCPP Basic Auth hin.

Prüfen:

- Benutzername = Charge-Point-ID?
- korrektes Secret?
- keine führenden/nachgestellten Leerzeichen?

## ❌ HTTP 403

Mögliche Ursachen:

- TLS/WSS ist vorgeschrieben
- unbekannte Ladepunkte sind gesperrt
- für den Ladepunkt fehlt ein erforderliches Secret

## ❌ HTTP 429

Es gab zu viele fehlgeschlagene OCPP-Anmeldeversuche. Nach Ablauf des Sperrfensters erneut versuchen und vorher die Zugangsdaten korrigieren.

## ❌ WSS funktioniert über Reverse Proxy nicht

Prüfen:

- WebSocket-Support am Proxy aktiviert?
- Zertifikat gültig?
- Pfad wird vollständig weitergeleitet?
- Proxy leitet auf den richtigen internen Port 9000?
- `X-Forwarded-Proto` wird korrekt gesetzt?
- falls VoltCore darauf vertrauen soll: `TRUST_PROXY_HEADERS=1`

---

# ✅ Checkliste

Vor dem ersten echten Test:

- [ ] VoltCore Community läuft
- [ ] Weboberfläche erreichbar
- [ ] OCPP-Port 9000 erreichbar
- [ ] OCPP 1.6 JSON an der Säule gewählt
- [ ] URL enthält die Charge-Point-ID
- [ ] Charge-Point-ID ist eindeutig
- [ ] `ocpp1.6` wird als Subprotokoll verwendet
- [ ] bei WSS: Zertifikat / Proxy korrekt
- [ ] bei Basic Auth: Benutzername = Charge-Point-ID
- [ ] Firewall geprüft
- [ ] Logs geöffnet, falls die Verbindung nicht klappt

---

## 💙 Merksatz

> **Die wichtigste Zeile für den Start ist:**
>
> `ws://SERVER:9000/CHARGE-POINT-ID`

Beispiel:

```text
ws://192.168.1.50:9000/AMTRON-01
```

Damit ist die häufigste Frage beim ersten VoltCore-Start schon beantwortet. 😄⚡🔌
