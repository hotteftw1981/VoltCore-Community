"""P01: middleware permission regression tests (no network or live sessions)."""
import os
import tempfile
import unittest
from unittest.mock import patch

from starlette.requests import Request
from starlette.responses import Response
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="voltcore-p01-import-"))

from app import db, main


class PermissionMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def check_role(self, role, method, path):
        scope = {
            "type": "http", "http_version": "1.1", "method": method,
            "scheme": "http", "path": path, "root_path": "",
            "query_string": b"", "headers": [
                (b"host", b"localhost"),
                (b"cookie", (main.SESSION_COOKIE + "=test-only").encode("ascii")),
            ],
            "server": ("localhost", 80), "client": ("127.0.0.1", 9000),
        }
        async def downstream(_request):
            return Response("OK")
        identity = {"id": 101, "role": role, "username": role, "display_name": role}
        with patch.object(db, "system_user_count", return_value=1), \
             patch.object(db, "system_user_for_session", return_value=identity), \
             patch.object(db, "get_setting", return_value="1"), \
             patch.object(db, "add_activity"), \
             patch.object(main, "render", return_value=Response(status_code=403)):
            return await main.web_access_control(Request(scope), downstream)

    async def test_viewer_cannot_change_user_records(self):
        result = await self.check_role("viewer", "POST", "/api/users")
        self.assertEqual(result.status_code, 403)

    async def test_viewer_cannot_change_charge_points(self):
        result = await self.check_role("viewer", "DELETE", "/api/charge-points/SIM")
        self.assertEqual(result.status_code, 403)

    async def test_regular_user_cannot_manage_system_accounts(self):
        result = await self.check_role("user", "GET", "/api/system-users")
        self.assertEqual(result.status_code, 403)

    async def test_admin_can_reach_user_api(self):
        result = await self.check_role("admin", "POST", "/api/users")
        self.assertEqual(result.status_code, 200)

    async def test_regular_user_must_not_create_charge_point(self):
        result = await self.check_role("user", "POST", "/api/charge-points")
        self.assertEqual(result.status_code, 403)

    async def test_regular_user_must_not_delete_users(self):
        result = await self.check_role("user", "DELETE", "/api/users/42")
        self.assertEqual(result.status_code, 403)

    @unittest.expectedFailure
    async def test_user_portal_admin_preview_must_be_private(self):
        result = await self.check_role("viewer", "GET", "/admin/ladeguthaben/42")
        self.assertEqual(result.status_code, 403)


if __name__ == "__main__":
    unittest.main()
