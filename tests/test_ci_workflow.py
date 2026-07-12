import re
from pathlib import Path

import yaml


def test_ci_workflow_is_pinned_reproducible_and_secret_free() -> None:
    text = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    data = yaml.load(text, Loader=yaml.BaseLoader)
    assert data["permissions"] == {"contents": "read"}
    assert "secrets." not in text
    assert "actions/upload-artifact" not in text
    assert "GITHUB_STEP_SUMMARY" not in text
    assert "set -x" not in text

    for job in data["jobs"].values():
        assert job["timeout-minutes"]
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] == "false"

    quality = data["jobs"]["quality"]
    commands = "\n".join(step.get("run", "") for step in quality["steps"])
    assert "pip install --require-hashes -r requirements/ci.txt" in commands
    assert "pip install --no-build-isolation --no-deps ." in commands
    assert "medlearn bundle validate examples/gerd" in commands
    assert "medlearn bundle validate examples/copd" in commands
    assert "pip wheel --no-build-isolation --no-deps" in commands
    assert "medlearn --help" in commands and "medlearn doctor" in commands

    worker = data["jobs"]["worker-quality"]
    worker_commands = "\n".join(step.get("run", "") for step in worker["steps"])
    assert "npx --no-install wrangler deploy --dry-run" in worker_commands

    lock = Path("requirements/ci.txt").read_text(encoding="utf-8")
    requirement_lines = [
        line for line in lock.splitlines() if line and not line.startswith((" ", "#"))
    ]
    assert requirement_lines and all(
        "==" in line and line.endswith(chr(92)) for line in requirement_lines
    )
    assert "--hash=sha256:" in lock
