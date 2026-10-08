import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="voltcore-community-update-provider-"))

from app import updates


class CommunityUpdateProviderLogicTests(unittest.TestCase):
    def test_portainer_file_stack_update_preserves_env_and_sets_exact_image(self):
        calls = []

        def fake_request(method, path, payload=None):
            calls.append((method, path, payload))
            if method == "GET" and path == "/api/stacks":
                return [{
                    "Id": 7,
                    "EndpointId": 2,
                    "Name": "voltcore-community",
                    "Env": [{"name": "KEEP_ME", "value": "yes"}],
                }]
            if method == "GET" and path == "/api/stacks/7":
                return {
                    "Id": 7,
                    "EndpointId": 2,
                    "Name": "voltcore-community",
                    "Env": [{"name": "KEEP_ME", "value": "yes"}],
                }
            if method == "GET" and path == "/api/stacks/7/file":
                return {"StackFileContent": "services:\n  voltcore-community:\n    image: test"}
            if method == "PUT":
                return {"ok": True}
            raise AssertionError((method, path, payload))

        cfg = {
            "portainer_stack_name": "voltcore-community",
            "portainer_endpoint_id": "",
        }
        with patch.object(updates, "settings", return_value=cfg), \
             patch.object(updates, "_portainer_api_request", side_effect=fake_request):
            result = updates.trigger_portainer_api("0.9.7.77")

        self.assertTrue(result["ok"])
        self.assertEqual("portainer-api", result["provider"])
        method, path, payload = calls[-1]
        self.assertEqual("PUT", method)
        self.assertEqual("/api/stacks/7?endpointId=2", path)
        self.assertTrue(payload["RepullImageAndRedeploy"])
        self.assertEqual(
            "services:\n  voltcore-community:\n    image: test",
            payload["StackFileContent"],
        )
        env = {item["name"]: item["value"] for item in payload["Env"]}
        self.assertEqual("yes", env["KEEP_ME"])
        self.assertEqual(
            "ghcr.io/hotteftw1981/voltcore-community:v0.9.7.77",
            env["VOLTCORE_COMMUNITY_IMAGE"],
        )

    def test_portainer_git_stack_uses_git_redeploy_and_exact_image(self):
        calls = []

        def fake_request(method, path, payload=None):
            calls.append((method, path, payload))
            if method == "GET" and path == "/api/stacks":
                return [{
                    "Id": 8,
                    "EndpointId": 3,
                    "Name": "voltcore-community",
                    "Env": [{"name": "KEEP_ME", "value": "still-here"}],
                }]
            if method == "GET" and path == "/api/stacks/8":
                return {
                    "Id": 8,
                    "EndpointId": 3,
                    "Name": "voltcore-community",
                    "Env": [{"name": "KEEP_ME", "value": "still-here"}],
                    "GitConfig": {"ReferenceName": "refs/heads/main"},
                }
            if method == "PUT":
                return {"ok": True}
            raise AssertionError((method, path, payload))

        cfg = {
            "portainer_stack_name": "voltcore-community",
            "portainer_endpoint_id": "3",
        }
        with patch.object(updates, "settings", return_value=cfg), \
             patch.object(updates, "_portainer_api_request", side_effect=fake_request):
            result = updates.trigger_portainer_api("1.2.3")

        self.assertTrue(result["ok"])
        method, path, payload = calls[-1]
        self.assertEqual("PUT", method)
        self.assertEqual("/api/stacks/8/git/redeploy?endpointId=3", path)
        self.assertTrue(payload["RepullImageAndRedeploy"])
        env = {item["name"]: item["value"] for item in payload["Env"]}
        self.assertEqual("still-here", env["KEEP_ME"])
        self.assertEqual(
            "ghcr.io/hotteftw1981/voltcore-community:v1.2.3",
            env["VOLTCORE_COMMUNITY_IMAGE"],
        )
        self.assertFalse(any(path == "/api/stacks/8/file" for _, path, _ in calls))

    def test_ambiguous_portainer_stack_requires_environment_id(self):
        def fake_request(method, path, payload=None):
            if method == "GET" and path == "/api/stacks":
                return [
                    {"Id": 1, "EndpointId": 1, "Name": "voltcore-community"},
                    {"Id": 2, "EndpointId": 2, "Name": "voltcore-community"},
                ]
            raise AssertionError((method, path, payload))

        cfg = {
            "portainer_stack_name": "voltcore-community",
            "portainer_endpoint_id": "",
        }
        with patch.object(updates, "settings", return_value=cfg), \
             patch.object(updates, "_portainer_api_request", side_effect=fake_request):
            with self.assertRaisesRegex(RuntimeError, "Environment-ID"):
                updates._portainer_stack()


if __name__ == "__main__":
    unittest.main()
