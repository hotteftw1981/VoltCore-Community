# 🐳🟦 VoltCore Community installieren

[🇩🇪 Deutsch](INSTALLATION.md) · [🇬🇧 English](INSTALLATION.en.md)

<p align="center">
  <img src="../app/static/branding/voltcore-community-horizontal.svg" alt="VoltCore Community Edition" width="680">
</p>

> [!IMPORTANT]
> 🧪 **VoltCore Community befindet sich aktuell im Release-Candidate-Stadium.**
>
> Die unten beschriebenen Installationswege werden automatisiert per Community CI gegen frische Installationen geprüft.

## ⚡ Zwei offizielle Installationswege

VoltCore Community soll bewusst einfach selbst gehostet werden können. Dafür unterstützen wir zwei Wege:

| Variante | Geeignet für | Empfehlung |
| --- | --- | --- |
| 🐳 **Docker Compose** | Docker-Hosts, Server, NAS mit CLI | ⭐ Standardweg |
| 🟦 **Portainer** | Nutzer mit Portainer / Container-GUI | ⭐ Komfortweg |

Beide Varianten verwenden dieselben persistenten Daten und dieselben Community-Updates über GitHub Releases. 🔄🐙

---

# 🐳 Variante 1: Docker Compose

## ✅ Voraussetzungen

Du brauchst:

- Docker Engine
- Docker Compose Plugin (`docker compose`)
- freien Port für die Weboberfläche, standardmäßig **8010**
- freien Port für OCPP, standardmäßig **9000**
- Internetzugriff für Updates über GitHub Releases

Prüfen kannst du Docker mit:

```bash
docker --version
docker compose version
```

## 📥 Repository holen

Während der Entwicklungsphase wird aus dem Repository gebaut:

```bash
git clone https://github.com/hotteftw1981/VoltCore-Community.git
cd VoltCore-Community
```

Stabile Releases werden zusätzlich als fertiges Community-Image über GHCR bereitgestellt.

## ⚙️ Umgebungsdatei anlegen

Kopiere die Beispielkonfiguration:

```bash
cp .env.example .env
```

Für einen ersten lokalen Test reichen die Standardwerte normalerweise aus.

## 🚀 Starten

```bash
docker compose up -d
```

Status prüfen:

```bash
docker compose ps
```

Logs ansehen:

```bash
docker compose logs -f voltcore-community
```

## 🌐 Oberfläche öffnen

Standardmäßig:

```text
http://<SERVER-IP>:8010
```

Beispiel:

```text
http://192.168.1.50:8010
```

## 🔌 Ladepunkte verbinden

Der OCPP-1.6J-Endpunkt läuft standardmäßig auf Port **9000**.

Die URL hat dieses Schema:

```text
ws://<SERVER>:9000/<CHARGE-POINT-ID>
```

Beispiel:

```text
ws://192.168.1.50:9000/AMTRON-01
```

Für WSS gilt entsprechend:

```text
wss://<SERVER>:9000/<CHARGE-POINT-ID>
```

👉 **[Ausführliche Anleitung: Ladepunkt mit VoltCore Community verbinden](OCPP_CONNECTION.md)**

Dort sind auch WS/WSS, Reverse Proxy, OCPP Basic Auth, Onboarding, Fehlercodes und konkrete Troubleshooting-Beispiele beschrieben.

## 💾 Persistente Daten

VoltCore Community nutzt ein Docker-Volume:

```text
voltcore_community_data
```

Dort liegen die persistenten Anwendungsdaten.

Wichtig: Ein Container-Neustart oder Image-Update löscht dieses Volume **nicht**. ✅

## ⏹️ Stoppen

```bash
docker compose down
```

Das Datenvolume bleibt erhalten.

Nur wenn du ausdrücklich auch die Volumes löschen willst:

```bash
docker compose down -v
```

> [!CAUTION]
> ⚠️ `-v` löscht persistente Daten. Vorher Backup erstellen!

---

# 🟦 Variante 2: Portainer

Portainer ist **nicht erforderlich**, wird aber als offizieller komfortabler Installationsweg unterstützt. 😄

## ✅ Voraussetzungen

- laufender Docker-Host
- Portainer
- Zugriff auf **Stacks**
- freie Ports **8010** und **9000**
- Internetzugriff zum Laden des Community-Images und für GitHub-Updates

## 🧱 Stack anlegen

In Portainer:

1. **Stacks** öffnen
2. **Add stack** wählen
3. Name z. B. `voltcore-community`
4. als Quelle die Community-Stack-Datei verwenden:
   `docker-compose.portainer.yml`
5. Stack deployen

Für Git-basierte Stacks kann direkt dieses Community-Repository verwendet werden.

## 📦 Community-Image

Die Portainer-Variante ist auf das Community-Image vorbereitet:

```text
ghcr.io/hotteftw1981/voltcore-community:latest
```

Für stabile Veröffentlichungen werden zusätzlich versionierte Tags verwendet, z. B.:

```text
ghcr.io/hotteftw1981/voltcore-community:1.0.0
```

> Das Community-Image wird durch die Release-/Container-Pipeline aus dem geprüften Stand erzeugt.

## 🌐 Oberfläche

Nach erfolgreichem Deployment:

```text
http://<SERVER-IP>:8010
```

## 🔄 Stack neu deployen

Bei einem späteren Image-Update kann Portainer das neue Community-Image ziehen und den Stack neu deployen.

**Wichtig:** Portainer ist dabei nur der Installations-/Deployment-Weg.  
Die Updatequelle innerhalb von VoltCore Community bleibt immer:

**🐙 GitHub Releases → VoltCore Community Update-Center**

---

# 🔄 1-Klick-Updates

VoltCore Community prüft stabile Releases im Repository:

```text
hotteftw1981/VoltCore-Community
```

Der Ablauf ist vollständig integriert:

1. 🔍 Community erkennt einen neuen GitHub Release
2. 📝 das Update-Center zeigt Version und Release Notes
3. 💾 vor jeder Installation wird **zwingend** ein lokales Pre-Update-Backup erstellt
4. 🔐 VoltCore startet den passenden Update-Provider
5. 📦 der Provider zieht das freigegebene Community-Image
6. ♻️ der Anwendungscontainer wird neu erstellt
7. ✅ nach dem Neustart bestätigt VoltCore die neue Version

## 🐳 Docker Compose

Der Standard-Stack enthält einen kleinen Dienst `voltcore-community-updater`. Nur dieser Sidecar erhält Zugriff auf `/var/run/docker.sock`; der VoltCore-Anwendungscontainer selbst hat **keinen** Docker-Socket-Zugriff.

Der Updater:
- ist nicht über einen Host-Port erreichbar
- erzeugt beim ersten Start automatisch ein zufälliges gemeinsames Bearer-Token
- akzeptiert nur syntaktisch gültige Versionsziele
- zieht das exakte Image `ghcr.io/hotteftw1981/voltcore-community:v<version>`
- recreatet ausschließlich den Dienst `voltcore-community`

Für Docker Compose ist nach der normalen Installation keine zusätzliche Update-Konfiguration nötig.

## 🟦 Portainer CE / BE

Portainer-Installationen können 1-Klick-Updates über die **Portainer REST API** ausführen. Dafür einmalig im Update-Center eintragen:

- Portainer URL, z. B. `https://portainer:9443`
- Stackname, standardmäßig `voltcore-community`
- optional die Environment-ID, falls derselbe Stackname in mehreren Environments vorkommt
- einen Portainer API-Key
- TLS-Prüfung je nach Zertifikat

VoltCore sucht den Stack über die API, übernimmt dessen vorhandene Environment-Variablen und setzt `VOLTCORE_COMMUNITY_IMAGE` auf das freigegebene Versionsimage. Bei Git-basierten Stacks wird der Git-Redeploy-Endpunkt verwendet; bei Datei-/Web-Editor-Stacks wird der bestehende Stack-Inhalt wiederverwendet und mit Image-Re-Pull neu deployed.

## 🟦 Portainer Business – optionaler Webhook

Wer Portainer Business mit Stack-Webhooks verwendet, kann stattdessen einen Stack-Webhook hinterlegen. Der Webhook hat Vorrang vor dem API-Weg und bekommt das exakte Community-Image als Environment-Variable übergeben.

Die **Updatequelle bleibt in allen Fällen identisch: GitHub Releases**.

---

# 🔐 Sicherheit

Für einen lokalen Test ist direkter Zugriff über Port 8010 ausreichend.

Für öffentliche Installationen empfehlen wir später ausdrücklich:

- 🔒 HTTPS über Reverse Proxy
- 🍪 sichere Cookies
- 🛡️ Firewall-Regeln
- 🔐 starke Admin-Zugangsdaten
- 🔄 regelmäßige Updates
- 💾 regelmäßige Backups
- 🚫 OCPP- und Webports nicht unnötig offen ins Internet stellen

Eine ausführliche Security-Anleitung folgt separat.

---

# 🌙 Wichtige Ports

| Port | Zweck | Standard |
| --- | --- | --- |
| `8010` | VoltCore Weboberfläche | Host → Container 8000 |
| `9000` | OCPP 1.6J | Host → Container 9000 |

Die Hostports können später über die Compose-Konfiguration angepasst werden.

---

# 💾 Backup vor Änderungen

Vor Updates, größeren Konfigurationsänderungen oder Migrationen:

**Backup erstellen. Immer.** 😄💾

VoltCore Community enthält ein eigenes Backup-/Restore-System. Details folgen in einer separaten Backup-Anleitung.

---

# 🩺 Schnellcheck bei Problemen

## Container läuft nicht

```bash
docker compose ps
docker compose logs --tail=200 voltcore-community
```

## Weboberfläche nicht erreichbar

Prüfen:

- läuft der Container?
- ist Port 8010 frei?
- blockiert eine Firewall den Port?
- wurde ein anderer Hostport konfiguriert?

## OCPP-Ladepunkt verbindet sich nicht

Prüfen:

- Port 9000 erreichbar?
- richtige Server-IP / Hostname?
- richtige Charge-Point-ID?
- WebSocket-Verbindung erlaubt?
- Reverse Proxy korrekt konfiguriert?

---

# 🧹 Deinstallation

Container und Netzwerk entfernen:

```bash
docker compose down
```

Daten behalten: **ja** ✅

Komplett inklusive Daten löschen:

```bash
docker compose down -v
```

> [!CAUTION]
> 💥 Damit werden die Community-Datenvolumes gelöscht.

---

# 💙 Empfehlung

Für die meisten Nutzer:

> 🐳 **Docker Compose**, wenn du mit Docker/Terminal vertraut bist.

Wenn du ohnehin Portainer nutzt:

> 🟦 **Portainer Stack** – gleiche Community, bequem per GUI.

Beide Wege gehören offiziell zu VoltCore Community. ⚡🔌💙
