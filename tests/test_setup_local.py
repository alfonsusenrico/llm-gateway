"""Setup provisions a separate key without changing existing settings or printing them."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).parents[1]


def _run(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "scripts" / "setup-local", scripts / "setup-local")
    shutil.copyfile(ROOT / ".env.example", tmp_path / ".env.example")
    return subprocess.run(  # noqa: S603 — fixed executable and task-created script
        [sys.executable, str(scripts / "setup-local")], capture_output=True, text=True, check=False
    )


def _settings(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    )


def test_fresh_setup_generates_distinct_keys_with_private_permissions(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.returncode == 0
    values = _settings(tmp_path / ".env")
    key = values["PROVIDER_CREDENTIAL_ENCRYPTION_KEY"]
    Fernet(key.encode())
    assert key != values["RESULT_ENCRYPTION_KEY"]
    assert key not in result.stdout + result.stderr
    assert os.stat(tmp_path / ".env").st_mode & 0o777 == 0o600
    before = (tmp_path / ".env").read_bytes()
    assert _run(tmp_path).returncode == 0
    assert (tmp_path / ".env").read_bytes() == before


def test_existing_settings_and_legacy_key_are_preserved_without_import(tmp_path: Path) -> None:
    existing = "# preserved settings\nOPENAI_API_KEY=synthetic-legacy-secret\nPROJECT_KEY_PEPPER=synthetic-pepper\n"
    (tmp_path / ".env").write_text(existing)
    result = _run(tmp_path)
    assert result.returncode == 0
    assert (tmp_path / ".env").read_text().startswith(existing)
    assert "synthetic-legacy-secret" not in result.stdout + result.stderr
    assert "project_provider_credentials" not in (tmp_path / ".env").read_text()


def test_symbolic_link_target_is_never_replaced(tmp_path: Path) -> None:
    original = tmp_path / "original"
    original.write_text("synthetic-original")
    (tmp_path / ".env").symlink_to(original)
    assert _run(tmp_path).returncode == 1
    assert original.read_text() == "synthetic-original"
