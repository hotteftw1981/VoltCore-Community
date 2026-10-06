#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOW_ROOT_FILES = [
    ".dockerignore",
    ".env.example",
    "CHANGELOG.md",
    "Dockerfile",
    "README.md",
    "README.en.md",
    "docker-compose.yml",
    "docker-compose.portainer.yml",
    "requirements.txt",
]

def version() -> str:
    text = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not match:
        raise SystemExit("APP_VERSION konnte in app/main.py nicht gelesen werden.")
    return match.group(1)

def copy_tree(target: Path) -> None:
    for name in ALLOW_ROOT_FILES:
        src = ROOT / name
        if not src.exists():
            raise SystemExit(f"Pflichtdatei fehlt: {name}")
        shutil.copy2(src, target / name)
    shutil.copytree(
        ROOT / "app",
        target / "app",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    shutil.copytree(ROOT / "docs", target / "docs")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", default="dist")
    args = parser.parse_args()
    dist = (ROOT / args.dist).resolve() if not Path(args.dist).is_absolute() else Path(args.dist)
    dist.mkdir(parents=True, exist_ok=True)
    ver = version()
    folder = f"voltcore-community-v{ver}"
    archive = dist / f"VoltCore_Community_V{ver.replace('.', '_')}.zip"
    with tempfile.TemporaryDirectory(prefix="ocpp-release-") as tmp:
        release_root = Path(tmp) / folder
        release_root.mkdir()
        copy_tree(release_root)
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for path in sorted(release_root.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(release_root.parent).as_posix())
        with zipfile.ZipFile(archive, "r") as zf:
            bad = zf.testzip()
            if bad:
                raise SystemExit(f"ZIP-Integritätsfehler: {bad}")
    print(archive)

if __name__ == "__main__":
    main()
