"""Destructive smoke test for an EMPTY, disposable loopback-only QA instance.

Never run against an installed system. The explicit environment opt-in, fresh
setup redirect, loopback restriction and random credentials guard test writes.
The restart phase reuses only a private temporary state file, not a production DB.
"""
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Smoke:
    def __init__(self, base):
        self.base = base.rstrip("/")
        parsed = urllib.parse.urlsplit(self.base)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or parsed.username or parsed.password or parsed.path not in ("", "/")):
            raise ValueError("QA accepts only an explicit http://127.0.0.1:PORT target")
        self.jar = http.cookiejar.CookieJar()
        self.client = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(self.jar))
        self.failures = []
        self.count = 0

    def request(self, path, method="GET", form=None, payload=None, origin=None):
        headers = {"Origin": origin or self.base}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            response = self.client.open(req, timeout=15)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            body = response.read()
            content_type = response.headers.get("Content-Type", "")
            value = json.loads(body) if "application/json" in content_type else body
            return response.code, response.headers, value

    def check(self, label, condition):
        self.count += 1
        print(("PASS " if condition else "FAIL ") + label, flush=True)
        if not condition:
            self.failures.append(label)
        return condition

    def expect(self, path, code=200, **kwargs):
        actual, headers, body = self.request(path, **kwargs)
        self.check(f"{kwargs.get('method', 'GET')} {path}: {actual} (expected {code})", actual == code)
        return actual, headers, body

    @staticmethod
    def html(body):
        return body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body or "")


    def reset_session(self):
        self.jar.clear()

    def login(self, username, password, code=303):
        self.reset_session()
        actual, headers, body = self.expect("/login", code, method="POST", form={
            "username": username, "password": password,
        })
        if code == 303:
            self.check(f"{username} receives authenticated session", any(
                cookie.name == "voltcore_community_session" for cookie in self.jar
            ))
        return actual, headers, body


    def health(self):
        for _ in range(30):
            try:
                code, headers, body = self.request("/health")
                if code == 200 and isinstance(body, dict) and body.get("status") == "ok":
                    self.check("real HTTP health endpoint", True)
                    self.check("health includes application version", bool(body.get("version")))
                    return
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(1)
        raise RuntimeError("Application never became healthy; inspect container startup log")

    def initial(self, state_path):
        self.health()
        code, headers, _ = self.request("/")
        if code != 303 or headers.get("Location") != "/setup":
            raise RuntimeError("Refusing writes: target is not an empty fresh installation")
        self.check("fresh installation redirects to setup", True)
        self.expect("/api/users", 503)
        self.expect("/setup")
        password = secrets.token_urlsafe(24) + "!aA9"
        state = {"username": "community-qa-admin", "password": password}
        code, headers, _ = self.expect("/setup", 303, method="POST", form={
            **state, "display_name": "Community QA Admin", "password_repeat": password,
        })
        if code != 303 or headers.get("Location") != "/first-run":
            raise RuntimeError("Initial administrator creation failed")
        self.check("session cookie is HTTP-only", "httponly" in headers.get("Set-Cookie", "").lower())
        self.expect("/first-run")
        self.expect("/api/users", 409)
        code, headers, _ = self.expect("/first-run", 303, method="POST", form={
            "organization_name": "Isolated Community QA", "display_name": "Community QA",
            "tariff_name": "QA Standard", "price_eur_kwh": "0,35",
            "default_monthly_limit_enabled": "1", "default_monthly_kwh": "100",
        })
        if code != 303 or headers.get("Location") != "/":
            raise RuntimeError("First-run completion failed")
        state_path.write_text(json.dumps(state), encoding="utf-8")
        state_path.chmod(0o600)

        for path in ("/", "/users", "/vehicles", "/charge-points", "/transactions",
                     "/reports", "/tariffs", "/settings", "/backups", "/updates",
                     "/security", "/system-users"):
            self.expect(path)
        for path in ("/api/users", "/api/vehicles", "/api/charge-points", "/api/rfid",
                     "/api/tariffs", "/api/reports", "/api/settings/branding",
                     "/api/settings/mail", "/api/rfid/local-list"):
            code, headers, data = self.expect(path)
            self.check(f"{path} returns JSON", isinstance(data, (dict, list)))

        _, _, data = self.expect("/api/users", method="POST", payload={"name": "QA Default"})
        default = data.get("user", {}) if isinstance(data, dict) else {}
        self.check("new user inherits 100 kWh default", default.get("monthly_kwh_limit") == 100)
        state["user_id"] = default.get("id")
        _, _, data = self.expect("/api/users", method="POST", payload={"name": "QA Unlimited", "monthly_kwh_limit": None})
        unlimited = data.get("user", {}) if isinstance(data, dict) else {}
        self.check("explicit unlimited overrides default", bool(unlimited.get("id")) and unlimited.get("monthly_kwh_limit") is None)
        if state["user_id"]:
            self.expect(f"/api/users/{state['user_id']}")
            self.expect(f"/api/users/{state['user_id']}", method="PUT", payload={"name": "QA Default Updated", "monthly_kwh_limit": 80})
        self.expect("/api/charge-points", method="POST", payload={"id": "QA-CP-001", "vendor": "QA", "model": "Virtual", "connector_count": 1, "max_power_kw": 22})
        self.expect("/api/vehicles", method="POST", payload={"name": "QA Vehicle", "plate": "QA-100"})
        if state["user_id"]:
            self.expect("/api/rfid", method="POST", payload={"uid": "QA-RFID-001", "user_id": state["user_id"]})

        # Block 3F: exercise real role boundaries through authenticated HTTP sessions.
        writer_password = secrets.token_urlsafe(24) + "!uU7"
        viewer_password = secrets.token_urlsafe(24) + "!vV8"
        state["writer"] = {"username": "community-qa-user", "password": writer_password}
        state["viewer"] = {"username": "community-qa-viewer", "password": viewer_password}
        _, _, writer_created = self.expect("/api/system-users", method="POST", payload={
            "username": state["writer"]["username"], "display_name": "Community QA User",
            "role": "user", "active": True, "password": writer_password,
        })
        _, _, viewer_created = self.expect("/api/system-users", method="POST", payload={
            "username": state["viewer"]["username"], "display_name": "Community QA Viewer",
            "role": "viewer", "active": True, "password": viewer_password,
        })
        state["writer"]["id"] = (writer_created.get("user") or {}).get("id") if isinstance(writer_created, dict) else None
        state["viewer"]["id"] = (viewer_created.get("user") or {}).get("id") if isinstance(viewer_created, dict) else None
        self.check("admin created writer account", bool(state["writer"]["id"]))
        self.check("admin created viewer account", bool(state["viewer"]["id"]))

        _, _, admin_home = self.expect("/")
        admin_html = self.html(admin_home)
        self.check("admin shell exposes admin role", 'data-role="admin"' in admin_html)
        self.check("admin navigation exposes settings", 'href="/settings"' in admin_html)
        _, _, admin_users_page = self.expect("/users")
        self.check("admin sees LocalList administration", "Offline-Autorisierung / LocalList" in self.html(admin_users_page))
        _, _, admin_cp_page = self.expect("/charge-points/QA-CP-001")
        self.check("admin sees remote-control tab", 'data-tab="remote"' in self.html(admin_cp_page))
        _, _, css_body = self.expect("/static/style.css")
        css_text = self.html(css_body)
        self.check("viewer write actions are hidden by role CSS", 'body[data-role="viewer"] .write-action{display:none!important}' in css_text)
        self.check("dark theme CSS exists", 'html[data-theme="dark"]' in css_text)
        self.check("mobile breakpoint CSS exists", '@media(max-width:700px)' in css_text)

        self.expect("/logout", 303, method="POST")
        self.expect("/api/users", 401)

        self.login(state["writer"]["username"], writer_password)
        _, _, writer_home = self.expect("/")
        writer_html = self.html(writer_home)
        self.check("writer shell exposes user role", 'data-role="user"' in writer_html)
        self.check("writer navigation hides settings", 'href="/settings"' not in writer_html)
        self.check("writer navigation hides system users", 'href="/system-users"' not in writer_html)
        self.check("writer has no read-only banner", "Nur-Lese-Zugang" not in writer_html)
        _, _, writer_users_page = self.expect("/users")
        self.check("writer does not see LocalList administration", "Offline-Autorisierung / LocalList" not in self.html(writer_users_page))
        _, _, writer_cp_page = self.expect("/charge-points/QA-CP-001")
        self.check("writer charge-point page hides remote-control tab", 'data-tab="remote"' not in self.html(writer_cp_page))
        for path in ("/users", "/vehicles", "/charge-points", "/transactions", "/reports", "/activity"):
            self.expect(path)
        for path in ("/settings", "/security", "/tariffs", "/backups", "/updates", "/system-users"):
            self.expect(path, 403)
        for path in ("/api/system-users", "/api/settings/branding", "/api/tariffs", "/api/backups"):
            self.expect(path, 403)
        self.expect("/api/system-users", 403, method="POST", payload={
            "username": "MUST-NOT-BE-CREATED", "display_name": "Forbidden",
            "role": "admin", "active": True, "password": "ForbiddenPassword!123",
        })
        self.expect("/api/remote-control/QA-CP-001/reset", 403, method="POST", payload={})
        _, _, writer_user = self.expect("/api/users", method="POST", payload={"name": "QA Writer Created"})
        writer_user_id = (writer_user.get("user") or {}).get("id") if isinstance(writer_user, dict) else None
        self.check("writer can create operational charging users", bool(writer_user_id))
        self.expect("/api/vehicles", method="POST", payload={"name": "QA Writer Vehicle", "plate": "QA-WRITER"})
        self.expect("/api/users")
        self.expect("/logout", 303, method="POST")

        self.login(state["viewer"]["username"], viewer_password)
        _, _, viewer_home = self.expect("/")
        viewer_html = self.html(viewer_home)
        self.check("viewer shell exposes viewer role", 'data-role="viewer"' in viewer_html)
        self.check("viewer sees read-only banner", "Nur-Lese-Zugang" in viewer_html)
        self.check("viewer navigation hides settings", 'href="/settings"' not in viewer_html)
        self.check("viewer navigation hides system users", 'href="/system-users"' not in viewer_html)
        _, _, viewer_users_page = self.expect("/users")
        self.check("viewer does not see LocalList administration", "Offline-Autorisierung / LocalList" not in self.html(viewer_users_page))
        _, _, viewer_cp_page = self.expect("/charge-points/QA-CP-001")
        viewer_cp_html = self.html(viewer_cp_page)
        self.check("viewer charge-point page hides remote-control tab", 'data-tab="remote"' not in viewer_cp_html)
        self.check("viewer edit affordance is tagged as write action", 'id="editMasterData" class="secondary button write-action"' in viewer_cp_html)
        for path in ("/users", "/vehicles", "/charge-points", "/transactions", "/reports", "/activity"):
            self.expect(path)
        for path in ("/settings", "/security", "/tariffs", "/backups", "/updates", "/system-users"):
            self.expect(path, 403)
        for path in ("/api/system-users", "/api/settings/branding", "/api/tariffs", "/api/backups"):
            self.expect(path, 403)
        self.expect("/api/users")
        self.expect("/api/vehicles")
        self.expect("/api/charge-points")
        self.expect("/api/reports")
        self.expect("/api/users", 403, method="POST", payload={"name": "MUST NOT EXIST VIEWER"})
        self.expect("/api/vehicles", 403, method="POST", payload={"name": "MUST NOT EXIST VIEWER", "plate": "NO-VIEW"})
        if state["user_id"]:
            self.expect(f"/api/users/{state['user_id']}", 403, method="PUT", payload={
                "name": "VIEWER MUST NOT EDIT", "monthly_kwh_limit": 1,
            })
        self.expect("/api/notifications/read-all", method="POST")
        self.expect("/api/account/password", method="POST", payload={
            "current_password": viewer_password, "new_password": viewer_password + "-changed",
        })
        state["viewer"]["password"] = viewer_password + "-changed"
        self.expect("/logout", 303, method="POST")
        self.login(state["viewer"]["username"], viewer_password, code=200)
        self.login(state["viewer"]["username"], state["viewer"]["password"])
        self.expect("/api/users")
        self.expect("/logout", 303, method="POST")

        self.login(state["username"], password)
        self.expect("/api/system-users")
        self.expect("/api/users", 403, method="POST", origin="https://foreign.example", payload={"name": "MUST NOT EXIST"})
        for path in ("/public/ladeguthaben", "/public/access-request", "/registration-onboarding",
                     "/registration-requests", "/engagement", "/cost-centers", "/imports",
                     "/load-management", "/liveview", "/api/settings/bonus-policy"):
            self.expect(path, 404)
        self.expect("/api/users/999999", 404)
        self.expect("/api/users", 400, method="POST", payload={"name": " "})
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.expect("/logout", 303, method="POST")
        self.expect("/api/users", 401)
        self.expect("/login")
        self.login(state["username"], password)
        self.expect("/api/users")

    def restart(self, state_path):
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.health()
        code, headers, _ = self.expect("/", 303)
        self.check("restart does not reopen initial setup", headers.get("Location", "").startswith("/login"))
        self.expect("/api/users", 401)
        self.expect("/login", 303, method="POST", form={"username": state["username"], "password": state["password"]})
        self.expect("/")
        _, _, data = self.expect("/api/users")
        rows = data.get("users", []) if isinstance(data, dict) else []
        self.check("operational users survive restart", len(rows) == 3)
        self.check("edited monthly limit survives restart", any(x.get("id") == state.get("user_id") and x.get("monthly_kwh_limit") == 80 for x in rows))
        self.check("blocked cross-origin request did not create a user", not any(x.get("name") == "MUST NOT EXIST" for x in rows))
        self.expect("/api/rfid")
        self.expect("/api/reports")
        _, _, system_data = self.expect("/api/system-users")
        system_rows = system_data.get("users", []) if isinstance(system_data, dict) else []
        self.check("writer role survives restart", any(
            x.get("username") == state["writer"]["username"] and x.get("role") == "user" for x in system_rows
        ))
        self.check("viewer role survives restart", any(
            x.get("username") == state["viewer"]["username"] and x.get("role") == "viewer" for x in system_rows
        ))
        self.expect("/logout", 303, method="POST")
        self.login(state["writer"]["username"], state["writer"]["password"])
        self.expect("/api/users", method="POST", payload={"name": "QA Writer After Restart"})
        self.expect("/logout", 303, method="POST")
        self.login(state["viewer"]["username"], state["viewer"]["password"])
        self.expect("/api/users")
        self.expect("/api/users", 403, method="POST", payload={"name": "VIEWER AFTER RESTART MUST NOT WRITE"})
        state_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:18010")
    parser.add_argument("--phase", choices=("initial", "restart"), required=True)
    parser.add_argument("--state", default="/tmp/community-http-qa-state.json")
    args = parser.parse_args()
    if os.environ.get("VOLTCORE_QA_ALLOW_EMPTY_SETUP") != "1":
        raise SystemExit("Set VOLTCORE_QA_ALLOW_EMPTY_SETUP=1 only for disposable QA")
    smoke = Smoke(args.base)
    try:
        getattr(smoke, args.phase)(Path(args.state))
    except Exception as exc:
        smoke.check(f"{args.phase} aborted: {type(exc).__name__}: {exc}", False)
    print(f"RESULT {args.phase}: {smoke.count} checks, {len(smoke.failures)} failed", flush=True)
    return 1 if smoke.failures else 0


if __name__ == "__main__":
    sys.exit(main())
