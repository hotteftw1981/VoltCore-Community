# 🔌 Connecting a charge point to VoltCore Community

[🇩🇪 Deutsch](OCPP_CONNECTION.md) · [🇬🇧 English](OCPP_CONNECTION.en.md)

<p align="center">
  <img src="../app/static/branding/voltcore-community-horizontal.svg" alt="VoltCore Community Edition" width="680">
</p>

> [!IMPORTANT]
> VoltCore Community currently uses **OCPP 1.6 JSON over WebSocket**.  
> The charge point should offer the WebSocket subprotocol **`ocpp1.6`**.

## ⚡ Quick answer

If VoltCore Community runs at `192.168.1.50` and the charge point ID is `AMTRON-01`, use:

```text
ws://192.168.1.50:9000/AMTRON-01
```

With direct TLS/WSS on port 9000:

```text
wss://192.168.1.50:9000/AMTRON-01
```

Behind a reverse proxy on the standard HTTPS port:

```text
wss://ocpp.example.org/AMTRON-01
```

**The charge point ID is always the final path component of the URL.** 🔌

---

# 🧩 URL format

```text
ws://<SERVER>:<OCPP-PORT>/<CHARGE-POINT-ID>
```

or:

```text
wss://<SERVER>:<OCPP-PORT>/<CHARGE-POINT-ID>
```

Examples:

| Situation | Example |
| --- | --- |
| local IP, default OCPP port | `ws://192.168.1.50:9000/AMTRON-01` |
| local DNS name | `ws://voltcore.local:9000/AMTRON-01` |
| direct WSS | `wss://voltcore.local:9000/AMTRON-01` |
| reverse proxy on 443 | `wss://ocpp.example.org/AMTRON-01` |

## 🆔 Charge point ID

VoltCore reads the ID directly from the WebSocket path.

Recommended characters:

- letters
- digits
- hyphen `-`
- underscore `_`

Examples:

```text
AMTRON-01
AMEDIO_01
GARAGE-A-03
```

Maximum length: **128 characters**.

Do not use:

- `/`
- `\\`
- `?`
- `#`
- leading or trailing whitespace

---

# 🛰️ First connection

If the active security policy permits unknown stations:

1. 🔌 the station opens the WebSocket
2. 🆔 VoltCore reads the charge point ID
3. 📡 a new station is detected
4. 🟡 an unknown station is stored as **Pending**
5. 📬 the station sends `BootNotification`
6. 🧾 vendor/model metadata is recorded
7. ✅ the station can then be approved during onboarding

Previously approved stations are marked connected immediately.

---

# 📡 WebSocket subprotocol

VoltCore Community uses:

```text
ocpp1.6
```

The WebSocket handshake should therefore include:

```text
Sec-WebSocket-Protocol: ocpp1.6
```

Choose **OCPP 1.6 JSON / WebSocket**, not SOAP.

---

# 🔓 WS versus 🔒 WSS

Local unencrypted example:

```text
ws://192.168.1.50:9000/AMTRON-01
```

Encrypted example:

```text
wss://voltcore.local:9000/AMTRON-01
```

Direct OCPP TLS can be configured with:

```text
OCPP_TLS_CERTFILE
OCPP_TLS_KEYFILE
```

A reverse proxy may terminate TLS instead.

---

# 🌐 Reverse proxy

External example:

```text
wss://ocpp.example.org/AMTRON-01
```

forwarded internally to:

```text
ws://voltcore-community:9000/AMTRON-01
```

Requirements:

- WebSocket upgrades must be supported
- keep the full path including the charge point ID
- preserve the required upgrade headers
- `TRUST_PROXY_HEADERS=1` may be required when TLS is terminated at the proxy
- only trust proxy headers when the proxy itself is trusted

---

# 🔐 OCPP Basic Auth

When OCPP authentication is required:

- **username:** exactly the charge point ID
- **password:** the configured OCPP secret for that station

Example:

```text
Charge point ID: AMTRON-01
Username:        AMTRON-01
Password:        <configured secret>
```

Missing or invalid credentials cause the WebSocket handshake to be rejected.

---

# 🛡️ Security rules that may reject a connection

A connection may be rejected when:

- the charge point ID is invalid
- the `ocpp1.6` subprotocol is missing
- TLS/WSS is required
- unknown charge points are blocked
- Basic Auth is required but missing
- username or secret is wrong
- too many authentication failures occurred

---

# 🟢 Successful connection flow

Typical flow:

1. WebSocket connected
2. VoltCore records **Connected**
3. station sends **BootNotification**
4. VoltCore responds using OCPP 1.6
5. station appears in VoltCore
6. connector/status information starts updating

The Community UI is intended to show at least:

- online/offline state
- charge point ID
- vendor/model
- connection time
- WS/WSS transport
- latest OCPP message
- connector status
- diagnostics

---

# 🧪 Example

Server:

```text
192.168.111.20
```

Charge point ID:

```text
MENNEKES-01
```

OCPP URL:

```text
ws://192.168.111.20:9000/MENNEKES-01
```

Protocol:

```text
OCPP 1.6 JSON
```

Subprotocol:

```text
ocpp1.6
```

---

# 🧯 Troubleshooting

## No connection at all

Check:

- correct IP / hostname?
- VoltCore running?
- port 9000 published?
- firewall blocking port 9000?
- routing between station and server?
- OCPP 1.6 JSON enabled?

Docker logs:

```bash
docker compose ps
docker compose logs --tail=200 voltcore-community
```

## HTTP 400

Usually an invalid charge point path/ID.

Wrong:

```text
ws://192.168.1.50:9000/site/AMTRON-01
```

Correct:

```text
ws://192.168.1.50:9000/AMTRON-01
```

## HTTP 401

Check OCPP Basic Auth:

- username equals charge point ID
- secret is correct
- no accidental whitespace

## HTTP 403

Possible causes:

- TLS/WSS required
- unknown stations blocked
- required secret not configured

## HTTP 429

Too many failed authentication attempts. Correct the credentials and retry after the lockout window.

## WSS behind reverse proxy does not work

Check:

- WebSocket support enabled
- valid certificate
- full path forwarded
- target port is 9000
- forwarding headers are correct
- `TRUST_PROXY_HEADERS=1` only when appropriate

---

# ✅ Checklist

- [ ] VoltCore Community running
- [ ] Web UI reachable
- [ ] OCPP port 9000 reachable
- [ ] OCPP 1.6 JSON selected
- [ ] URL includes charge point ID
- [ ] unique, valid charge point ID
- [ ] `ocpp1.6` subprotocol offered
- [ ] WSS certificate/proxy correct when used
- [ ] Basic Auth username equals charge point ID when enabled
- [ ] firewall checked
- [ ] logs available for troubleshooting

---

## 💙 Remember

```text
ws://SERVER:9000/CHARGE-POINT-ID
```

Example:

```text
ws://192.168.1.50:9000/AMTRON-01
```
