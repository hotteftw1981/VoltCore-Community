import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CommunityIdentityTests(unittest.TestCase):
    def _read(self, relative):
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_update_source_is_community_github_repo(self):
        updates = self._read("app/updates.py")
        self.assertIn("hotteftw1981/VoltCore-Community", updates)
        self.assertIn('UPDATE_SOURCE = "github"', updates)
        self.assertIn('EDITION = "community"', updates)
        self.assertIn(".update_portainer_webhook", updates)
        self.assertIn(".update_portainer_api_key", updates)
        self.assertIn("trigger_portainer", updates)
        self.assertIn("trigger_docker_agent", updates)
        self.assertNotIn("ocpp-backend-update-center", updates)

    def test_docker_identity_is_community(self):
        compose = self._read("docker-compose.yml")
        portainer = self._read("docker-compose.portainer.yml")
        for content in (compose, portainer):
            self.assertIn("container_name: voltcore-community", content)
            self.assertNotIn("container_name: drk-ocpp-backend", content)
            self.assertIn("hotteftw1981/VoltCore-Community", content)
        self.assertIn("ghcr.io/hotteftw1981/voltcore-community:latest", portainer)
        self.assertNotIn("ghcr.io/hotteftw1981/voltcore:latest", portainer)

    def test_runtime_identity_has_no_legacy_drk_names(self):
        files = [
            "app/ocpp_server.py",
            "app/web_push.py",
            "app/mailer.py",
            "app/backup.py",
        ]
        combined = "\n".join(self._read(path) for path in files)
        self.assertNotIn("drk-ocpp-backend", combined.lower())
        self.assertNotIn('logging.getLogger("drk.', combined)
        self.assertNotIn("p.garbe@drk-schwelm.org", combined)

    def test_backup_names_are_community_specific(self):
        backup = self._read("app/backup.py")
        self.assertIn('BACKUP_PREFIX = "voltcore-community-backup-"', backup)
        self.assertNotIn("ocpp-backup-", backup)
        self.assertIn('(db.DATA_DIR/".update_portainer_webhook").resolve()', backup)
        self.assertIn('(db.DATA_DIR/".update_portainer_api_key").resolve()', backup)
        self.assertIn('".update_portainer_webhook"', backup)
        self.assertIn('".update_portainer_api_key"', backup)


if __name__ == "__main__":
    unittest.main()
