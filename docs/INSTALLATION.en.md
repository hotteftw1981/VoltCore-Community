# 🐳🟦 Installing VoltCore Community

[🇩🇪 Deutsch](INSTALLATION.md) · [🇬🇧 English](INSTALLATION.en.md)

<p align="center">
  <img src="../app/static/branding/voltcore-community-horizontal.svg" alt="VoltCore Community Edition" width="680">
</p>

> [!IMPORTANT]
> 🚧 **VoltCore Community is still under active development.**
>
> This guide already documents the intended official deployment paths. It will be finalized and release-tested with the first runnable Community release.

## ⚡ Two official installation methods

VoltCore Community is designed to stay easy to self-host:

| Method | Best for | Recommendation |
| --- | --- | --- |
| 🐳 **Docker Compose** | Docker hosts, servers, NAS systems with CLI access | ⭐ Default |
| 🟦 **Portainer** | Users running Portainer / a container GUI | ⭐ Convenience |

Both methods use persistent data and the same Community update source through GitHub Releases. 🔄🐙

---

# 🐳 Option 1: Docker Compose

## ✅ Requirements

You need:

- Docker Engine
- Docker Compose plugin (`docker compose`)
- a free web port, default **8010**
- a free OCPP port, default **9000**
- internet access for GitHub Release update checks

Verify Docker:

```bash
docker --version
docker compose version
```

## 📥 Clone the repository

During development the application is built from source:

```bash
git clone https://github.com/hotteftw1981/VoltCore-Community.git
cd VoltCore-Community
```

Stable releases will later also provide a prebuilt Community container image via GHCR.

## ⚙️ Create the environment file

```bash
cp .env.example .env
```

The defaults are normally sufficient for an initial local test.

## 🚀 Start

```bash
docker compose up -d --build
```

Check status:

```bash
docker compose ps
```

View logs:

```bash
docker compose logs -f voltcore-community
```

## 🌐 Open the UI

By default:

```text
http://<SERVER-IP>:8010
```

## 🔌 Connect charge points

The OCPP 1.6J endpoint listens on port **9000** by default.

URL format:

```text
ws://<SERVER>:9000/<CHARGE-POINT-ID>
```

Example:

```text
ws://192.168.1.50:9000/AMTRON-01
```

For WSS:

```text
wss://<SERVER>:9000/<CHARGE-POINT-ID>
```

👉 **[Full guide: Connect a charge point to VoltCore Community](OCPP_CONNECTION.en.md)**

The guide also covers WS/WSS, reverse proxying, OCPP Basic Auth, onboarding, HTTP errors and practical troubleshooting.

## 💾 Persistent data

VoltCore Community uses the Docker volume:

```text
voltcore_community_data
```

Container restarts and image updates do **not** remove this volume. ✅

## ⏹️ Stop

```bash
docker compose down
```

To explicitly remove volumes too:

```bash
docker compose down -v
```

> [!CAUTION]
> ⚠️ `-v` removes persistent data. Create a backup first.

---

# 🟦 Option 2: Portainer

Portainer is **not required**, but is officially supported as a convenient deployment method. 😄

## ✅ Requirements

- Docker host
- Portainer
- access to **Stacks**
- free ports **8010** and **9000**
- internet access for the Community image and GitHub updates

## 🧱 Create a stack

In Portainer:

1. open **Stacks**
2. choose **Add stack**
3. use a name such as `voltcore-community`
4. use `docker-compose.portainer.yml`
5. deploy the stack

Git-based stacks can later point directly to the public Community repository.

## 📦 Community image

The Portainer configuration is prepared for:

```text
ghcr.io/hotteftw1981/voltcore-community:latest
```

Stable releases will also use versioned tags such as:

```text
ghcr.io/hotteftw1981/voltcore-community:1.0.0
```

> 🚧 The public container image will be enabled with the first runnable Community release.

## 🌐 UI

After deployment:

```text
http://<SERVER-IP>:8010
```

## 🔄 Redeploying

Portainer may later pull a newer Community image and redeploy the stack.

Portainer remains only the deployment method. The canonical update source inside VoltCore Community remains:

**🐙 GitHub Releases → VoltCore Community Update Center**

---

# 🔄 Updates

VoltCore Community checks stable releases from:

```text
hotteftw1981/VoltCore-Community
```

Planned flow:

1. 🔍 Community detects a new GitHub Release
2. 📝 Update Center shows version and release notes
3. 💾 backup can be created before installation
4. 🧩 the matching deployment provider performs the install
5. ✅ after restart VoltCore recognizes the new version

Docker Compose and Portainer may use different installer implementations while sharing the **same update source**.

---

# 🔐 Security

For local testing, direct access through port 8010 is sufficient.

For public deployments we recommend:

- 🔒 HTTPS through a reverse proxy
- 🍪 secure cookies
- 🛡️ firewall rules
- 🔐 strong administrator credentials
- 🔄 regular updates
- 💾 regular backups
- 🚫 do not expose web/OCPP ports unnecessarily

A dedicated security guide will follow.

---

# 🌙 Ports

| Port | Purpose | Default |
| --- | --- | --- |
| `8010` | VoltCore web UI | Host → Container 8000 |
| `9000` | OCPP 1.6J | Host → Container 9000 |

Host ports may be adjusted in the Compose configuration.

---

# 💾 Back up before changes

Before updates, larger configuration changes or migrations:

**Create a backup. Always.** 😄💾

VoltCore Community includes its own backup/restore system.

---

# 🩺 Quick troubleshooting

## Container does not start

```bash
docker compose ps
docker compose logs --tail=200 voltcore-community
```

## Web UI not reachable

Check:

- container running?
- port 8010 available?
- firewall blocking the port?
- custom host port configured?

## Charge point cannot connect

Check:

- port 9000 reachable?
- correct server IP / hostname?
- correct charge point ID?
- WebSocket connection permitted?
- reverse proxy configured correctly?

---

# 🧹 Uninstall

Remove container and network:

```bash
docker compose down
```

Keep data: **yes** ✅

Remove everything including data:

```bash
docker compose down -v
```

> [!CAUTION]
> 💥 This removes the Community data volumes.

---

# 💙 Recommendation

For most users:

> 🐳 **Docker Compose** if you are comfortable with Docker and a terminal.

If you already use Portainer:

> 🟦 **Portainer Stack** for the same Community edition with a GUI-driven workflow.

Both are official VoltCore Community deployment paths. ⚡🔌💙
