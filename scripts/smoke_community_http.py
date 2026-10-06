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
        self.expect("/login", 303, method="POST", form={"username": state["username"], "password": password})
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
        self.check("users survive restart", len(rows) == 2)
        self.check("edited monthly limit survives restart", any(x.get("id") == state.get("user_id") and x.get("monthly_kwh_limit") == 80 for x in rows))
        self.check("blocked cross-origin request did not create a user", not any(x.get("name") == "MUST NOT EXIST" for x in rows))
        self.expect("/api/rfid")
        self.expect("/api/reports")
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
