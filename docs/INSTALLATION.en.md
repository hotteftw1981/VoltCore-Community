# 🐳🟦 Installing VoltCore Community

[🇩🇪 Deutsch](INSTALLATION.md) · [🇬🇧 English](INSTALLATION.en.md)

<p align="center">
  <img src="../app/static/branding/voltcore-community-horizontal.svg" alt="VoltCore Community Edition" width="680">
</p>

> [!IMPORTANT]
> 🧪 **VoltCore Community is currently at release-candidate stage.**
>
> The deployment paths below are automatically validated by Community CI against fresh installations.

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

Stable releases also provide a prebuilt Community container image via GHCR.

## ⚙️ Create the environment file

```bash
cp .env.example .env
```

The defaults are normally sufficient for an initial local test.

## 🚀 Start

```bash
docker compose up -d
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

Git-based stacks can point directly to this Community repository.

## 📦 Community image

The Portainer configuration is prepared for:

```text
ghcr.io/hotteftw1981/voltcore-community:latest
```

Stable releases will also use versioned tags such as:

```text
ghcr.io/hotteftw1981/voltcore-community:1.0.0
```

> The Community image is produced from the tested source by the release/container pipeline.

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

# 🔄 One-click updates

VoltCore Community checks stable releases from:

```text
hotteftw1981/VoltCore-Community
```

The update flow is fully integrated:

1. 🔍 Community detects a new GitHub Release
2. 📝 Update Center shows version and release notes
3. 💾 a local pre-update backup is **mandatory**
4. 🔐 VoltCore starts the matching update provider
5. 📦 the provider pulls the released Community image
6. ♻️ the application container is recreated
7. ✅ after restart VoltCore confirms the new version

## 🐳 Docker Compose

The default stack includes a small `voltcore-community-updater` service. Only this sidecar receives access to `/var/run/docker.sock`; the VoltCore application container itself has **no** Docker socket access.

The updater:
- is not exposed on a host port
- generates a random shared bearer token on first start
- accepts only syntactically valid version targets
- pulls the exact image `ghcr.io/hotteftw1981/voltcore-community:v<version>`
- recreates only the `voltcore-community` service

No additional update configuration is required after a normal Docker Compose installation.

## 🟦 Portainer CE / BE

Portainer installations can perform one-click updates through the **Portainer REST API**. Configure once in Update Center:

- Portainer URL, for example `https://portainer:9443`
- stack name, default `voltcore-community`
- optional Environment ID when the same stack name exists in multiple environments
- a Portainer API key
- TLS verification according to your certificate setup

VoltCore locates the stack through the API, preserves its existing environment variables and sets `VOLTCORE_COMMUNITY_IMAGE` to the released version image. Git-backed stacks use Portainer's Git redeploy endpoint; file/web-editor stacks reuse the deployed stack content and force an image re-pull.

## 🟦 Portainer Business – optional webhook

Portainer Business users may alternatively store a stack webhook. The webhook takes precedence over the API provider and receives the exact Community image through an environment variable.

The **update source is identical in all cases: GitHub Releases**.

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
