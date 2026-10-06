<p align="center">
  <img src="app/static/branding/voltcore-community-horizontal.svg" alt="VoltCore Community Edition" width="760">
</p>

<p align="center">
  <strong>⚡ Kostenlos. 🔌 Selbst gehostet. 🧠 OCPP. 💙 Community.</strong>
</p>

<p align="center">
  <a href="README.md">🇩🇪 Deutsch</a> · <a href="README.en.md">🇬🇧 English</a>
</p>

---

> [!WARNING]
> ## ⚠️⚡ COMMUNITY-ESKALATION
> **VoltCore Community ist KOSTENLOS. Wirklich kostenlos. 🆓💸**
>
> Kein Testzeitraum. ⏳❌ Kein „nur drei Benutzer“. 👥❌ Kein „ein Ladepunkt kostenlos, danach bitte zahlen“. 🔌💳❌ Kein absichtlich kaputtgeschnittener Demo-Modus. 🪚😵
>
> Die Community Edition soll ein **wirklich brauchbares, selbst gehostetes OCPP-System** sein, das man auch ernsthaft einsetzen kann. 🖥️⚡🔋

## 🆓 Kostenlos. 🖥️ Selbst gehostet. ✅ Wirklich nutzbar.

VoltCore Community ist die **kostenlose Community Edition von VoltCore** für selbst gehostetes OCPP-Lademanagement. ⚡🔌

Die Idee ist simpel: Menschen, Vereine, Organisationen, kleine Fuhrparks, Testumgebungen und neugierige Nerds 🤓 sollen ihre eigene Ladeinfrastruktur mit einer soliden VoltCore-Basis betreiben können, **ohne für die Community Edition bezahlen zu müssen**.

### 💸 Preis: **0. Kostenlos. Free. Gratis.**

Community soll nicht absichtlich nervig oder unbrauchbar gemacht werden. Die Abgrenzung zu späteren VoltCore-Editionen erfolgt über weiterführende Produktfunktionen, Analytics, Integrationen und Enterprise-Funktionen — **nicht** dadurch, dass die Grundlagen künstlich beschnitten werden. 🧠✨

> 🚧 **Status:** in Entwicklung. Noch nicht für den öffentlichen Produktiveinsatz vorgesehen.

## 🚀 Installation

VoltCore Community unterstützt zwei offizielle Installationswege:

- 🐳 **Docker Compose** — unser Standardweg
- 🟦 **Portainer** — komfortabel als Stack über die GUI

👉 **[Zur ausführlichen Installationsanleitung](docs/INSTALLATION.md)**

Dort findest du Schritt für Schritt Voraussetzungen, Start, Ports, Datenhaltung, Updates, Backup-Hinweise und Troubleshooting. 📚🔧

## ⚡ Community-Umfang

> **Backend-only:** VoltCore Community besitzt ausschließlich die Verwaltungsoberfläche. Es gibt kein persönliches Ladeportal, keinen PIN-Login, keine öffentliche Benutzerregistrierung und keinen RFID-Self-Service für Ladebenutzer.

Die Community Edition enthält unter anderem:

- 🔌 OCPP 1.6J
- 🛰️ Ladepunkte erkennen und onboarden
- 🔋 Connector-Status
- 📡 Live-Status der Ladepunkte
- ▶️⏹️ Start / Stop von Ladevorgängen
- 🎛️ grundlegende Remote-Befehle
- 🪪 RFID-Verwaltung
- 👤 Benutzerverwaltung
- 🚗 einfache Fahrzeugverwaltung
- 🧾 Ladevorgänge / Historie
- 🔗 einfache Zuordnung Benutzer / Fahrzeug / RFID
- 📊 Dashboard
- 📈 einfache Reports
- 📄 CSV-Export
- 💾 Backup / Restore
- 🔄 Update-Menü direkt in VoltCore
- 🌙 Dark Mode
- 🎨 Basis-Branding wie Organisationsname und Logo
- 🛡️ Security-Basis
- 🔐 Rollen: Administrator / Benutzer / Nur Lesen
- 🩺 Systemstatus
- 🧭 Logs / Audit-Basis
- 🔔 einfache Benachrichtigungen
- 📱 PWA-Basis

Der festgelegte erste Funktionsumfang ist zusätzlich in `docs/COMMUNITY_SCOPE.md` dokumentiert. 📋✅

## 🔄 Updates über GitHub

VoltCore Community behält das bekannte **Update-Menü direkt in der Anwendung**. 🧰⚡

Stabile Community-Releases werden als **GitHub Releases** in diesem Repository veröffentlicht. Laufende Installationen können GitHub auf neuere stabile Versionen prüfen und Administratoren direkt in VoltCore auf verfügbare Updates hinweisen. 🚀

Geplant sind:

- 🚦 automatische Update-Prüfung nach dem Backend-Start
- 🕒 regelmäßige Prüfungen im Hintergrund
- 🔍 manueller Button **Jetzt nach Updates suchen**
- 📝 Release Notes vor der Installation
- 🧪 Prüfung, ob das Release wirklich für VoltCore Community bestimmt ist
- 🗃️ Berücksichtigung von Datenbank-Migrationen
- 🛟 Backup- und Rollback-Sicherheit

Öffentliche Community-Releases sollen sich prüfen lassen, **ohne dass jede Installation zwingend einen eigenen GitHub-Zugriffstoken speichern muss**. 🔓🐙

> 🧩 **Wichtig:** GitHub Releases sind die feste Updatequelle der Community Edition. Der eigentliche Installationsweg bleibt bewusst deployment-neutral. Docker, Portainer oder andere Varianten dürfen später eigene Installer bereitstellen — **VoltCore Community selbst ist nicht an Portainer gebunden.**

## 🌍 Sprachen

VoltCore Community wird von Anfang an **mehrsprachig** aufgebaut. 🌐✨

Zum Start:

- 🇩🇪 Deutsch
- 🇬🇧 Englisch

Die Browsersprache kann automatisch erkannt werden. Eine manuelle Sprachauswahl hat Vorrang. Englisch dient als Fallback-Sprache. 🔁

Die Übersetzungen liegen in `app/locales/`. Neue UI-Texte sollen nach Möglichkeit über Übersetzungsschlüssel eingebunden und nicht fest im Quellcode verdrahtet werden. 🧩

## 🎨 Branding & Logo-Set

Das offizielle Community-Branding ist direkt im Projekt enthalten. 💙⚡

<p align="center">
  <img src="app/static/branding/voltcore-community-icon.svg" alt="VoltCore Community Icon" width="150">
</p>

Enthalten sind:

- 🖼️ horizontales Hauptlogo
- 🌙 Variante für dunkle Oberflächen
- ↕️ gestapelte Logo-Version
- 🔌 transparentes VC-Icon
- 📱 App-Icon
- ⭐ Favicon

Die Dateien liegen unter `app/static/branding/`. Details und Farbwerte stehen in `docs/BRANDING.md`.

## 🧑‍💻 Entwicklungsmodell

VoltCore Community wird aus der privaten VoltCore-Entwicklungsbasis abgeleitet. 🧠⚙️

Funktionen, die nicht zu Community gehören, werden aus dem Community-Quellcode entfernt und nicht lediglich ausgeblendet. So landet im später öffentlichen Community-Repository nicht versehentlich Code, der nicht zur Community Edition gehört. 🧹🔒

Branches:

- 🟢 `main` — zukünftige stabile Community-Releases
- 🟡 `develop` — Integrationsbranch der Community Edition
- 🔵 kurzlebige Feature-Branches — einzelne Änderungen vor der Integration

Das Repository bleibt privat, während der erste Community-Build aufgebaut und geprüft wird. 🔐🚧

## 🧾 Was „kostenlos“ hier bedeutet

VoltCore Community ist als **kostenlose VoltCore-Edition** geplant. 🆓💙

Die endgültige öffentliche Softwarelizenz ist noch nicht festgelegt. Diese Entscheidung treffen wir, bevor das Repository öffentlich wird. Bis dahin ist dieses private Entwicklungs-Repository ausdrücklich noch keine endgültige Lizenzierungsaussage. ⚖️

Das ist bewusst getrennt:

- 🆓 **Community Edition: kostenlos**
- 📜 **endgültige öffentliche Lizenz: noch festzulegen**
- 🔐 **Repository: bleibt privat, bis Community veröffentlichungsreif ist**

## 📚 Dokumentation

Aktuell vorhanden:

- 📋 Community-Umfang: `docs/COMMUNITY_SCOPE.md`
- 🌍 Mehrsprachigkeit / i18n: `docs/I18N.md`
- 🎨 Branding & Logo-Set: `docs/BRANDING.md`
- 🐳🟦 Installation: `docs/INSTALLATION.md`
- 🔌 Ladepunkt verbinden / OCPP-URL: `docs/OCPP_CONNECTION.md`

Ausführliche Installations-, Konfigurations-, Update-, Backup-, OCPP-, Security- und Troubleshooting-Dokumentation folgt mit dem weiteren Community-Ausbau. 🛠️📖

## 💙 Grundidee

VoltCore Community soll:

- ✅ brauchbar statt künstlich eingeschränkt sein
- 🧠 verständlich statt geheimnisvoll sein
- 🔄 updatefähig statt wegwerfbar sein
- 🖥️ selbst hostbar statt cloudabhängig sein
- 🛡️ standardmäßig sicher sein
- ✨ angenehm zu bedienen sein
- 🤓 und natürlich nerdig genug bleiben, damit es noch nach VoltCore aussieht ⚡

---

<p align="center">
  <strong>⚡ VoltCore Community — OCPP-Lademanagement. 🆓 Kostenlos. 🖥️ Selbst gehostet. 💙 Für die Community.</strong>
</p>

<p align="center">
  Und ja: Diese README darf später noch deutlich weiter eskalieren. 😂🔥📚⚡
</p>
