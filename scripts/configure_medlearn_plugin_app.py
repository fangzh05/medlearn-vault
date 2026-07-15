"""Bind the local MedLearn plugin to an App created in ChatGPT Developer Mode."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path

APP_ID = re.compile(r"(?:asdk_app_[A-Za-z0-9]+|plugin_asdk_app_[A-Za-z0-9]+)$")


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
        temp = Path(file.name)
    os.replace(temp, path)


def configure(root: Path, app_id: str) -> None:
    if app_id.startswith("asdk_app_v_"):
        raise ValueError("versioned asdk_app_v_ identifiers are not valid App IDs")
    if not APP_ID.fullmatch(app_id):
        raise ValueError("app_id must be an asdk_app or legacy plugin_asdk_app identifier")
    plugin = root / "plugins" / "medlearn"
    manifest_path = plugin / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["apps"] = "./.app.json"
    atomic_json(plugin / ".app.json", {"apps": [{"id": app_id}]})
    atomic_json(manifest_path, manifest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("app_id")
    args = parser.parse_args()
    configure(Path(__file__).resolve().parents[1], args.app_id)
