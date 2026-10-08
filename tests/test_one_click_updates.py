from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CommunityOneClickUpdateTests(unittest.TestCase):
    def test_backend_exposes_both_update_providers(self):
        updates = (ROOT / "app" / "updates.py").read_text(encoding="utf-8")
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")

        self.assertIn('PORTAINER_WEBHOOK_FILE = db.DATA_DIR / ".update_portainer_webhook"', updates)
        self.assertIn('UPDATE_AGENT_URL = os.getenv("UPDATE_AGENT_URL"', updates)
        self.assertIn('return "portainer-webhook"', updates)
        self.assertIn('return "portainer-api"', updates)
        self.assertIn('return "docker-compose"', updates)
        self.assertIn('query["VOLTCORE_COMMUNITY_IMAGE"]', updates)
        self.assertIn('PORTAINER_API_KEY_FILE', updates)
        self.assertIn('"/api/stacks"', updates)
        self.assertIn('/git/redeploy?endpointId=', updates)
        self.assertIn('"RepullImageAndRedeploy": True', updates)
        self.assertIn('UPDATE_AGENT_URL + "/update"', updates)
        self.assertIn('"Authorization": f"Bearer {token}"', updates)

        self.assertIn("portainer_webhook: str | None = None", main)
        self.assertIn("portainer_url: str | None = None", main)
        self.assertIn("portainer_api_key: str | None = None", main)
        self.assertIn("portainer_tls_verify: bool = True", main)
        self.assertIn('@app.post("/api/updates/install",status_code=202)', main)
        self.assertIn("backup.create_backup", main)
        self.assertIn("updates.mark_pending", main)
        self.assertIn("background_tasks.add_task(updates.trigger_and_record,target)", main)

    def test_docker_updater_is_isolated_from_application_container(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        updater = (ROOT / "updater" / "updater.py").read_text(encoding="utf-8")
        updater_dockerfile = (ROOT / "updater" / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("voltcore-community-updater:", compose)
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock", compose)
        self.assertEqual(compose.count("/var/run/docker.sock:/var/run/docker.sock"), 1)
        self.assertIn("voltcore_community_updater:/run/voltcore-updater:ro", compose)
        self.assertIn("UPDATE_AGENT_URL: http://voltcore-community-updater:9011", compose)
        self.assertIn("condition: service_healthy", compose)

        self.assertIn('Authorization', updater)
        self.assertIn("hmac.compare_digest", updater)
        self.assertIn('VERSION_RE = re.compile', updater)
        self.assertIn('"docker", "pull", exact_image', updater)
        self.assertIn('"docker", "tag", exact_image, LATEST_ALIAS', updater)
        self.assertIn('"--force-recreate", COMPOSE_SERVICE', updater)
        self.assertIn("docker-cli-compose", updater_dockerfile)

    def test_portainer_uses_exact_version_image_without_docker_socket(self):
        compose = (ROOT / "docker-compose.portainer.yml").read_text(encoding="utf-8")
        updates = (ROOT / "app" / "updates.py").read_text(encoding="utf-8")

        self.assertIn(
            "VOLTCORE_COMMUNITY_IMAGE:-ghcr.io/hotteftw1981/voltcore-community:latest",
            compose,
        )
        self.assertIn("UPDATE_IMAGE_REPOSITORY", compose)
        self.assertNotIn("/var/run/docker.sock", compose)
        self.assertIn('f"{IMAGE_REPOSITORY}:v{target_version}"', updates)

    def test_update_ui_does_not_close_modal_on_backdrop_click(self):
        html = (ROOT / "app" / "templates" / "updates.html").read_text(encoding="utf-8")
        self.assertIn("Aktiver 1-Klick-Provider", html)
        self.assertIn("Docker Compose", html)
        self.assertIn("Portainer", html)
        self.assertIn("Portainer CE / BE über API", html)
        self.assertIn("portainerApiKey", html)
        self.assertNotIn("updateConfirmModal').onclick", html)

    def test_update_provider_secrets_are_excluded_from_backups(self):
        backup = (ROOT / "app" / "backup.py").read_text(encoding="utf-8")
        self.assertIn('(db.DATA_DIR/".update_github_token").resolve()', backup)
        self.assertIn('(db.DATA_DIR/".update_portainer_webhook").resolve()', backup)
        self.assertIn('(db.DATA_DIR/".update_portainer_api_key").resolve()', backup)
        self.assertIn('".update_github_token"', backup)
        self.assertIn('".update_portainer_webhook"', backup)
        self.assertIn('".update_portainer_api_key"', backup)

    def test_release_archive_includes_updater(self):
        builder = (ROOT / "scripts" / "build_release.py").read_text(encoding="utf-8")
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn('shutil.copytree(ROOT / "updater"', builder)
        self.assertIn("Release archive missing updater/", ci)
        self.assertIn("docker build --pull -t voltcore-community-updater-ci", ci)


if __name__ == "__main__":
    unittest.main()
