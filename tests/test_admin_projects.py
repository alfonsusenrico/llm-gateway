from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from llm_gateway.admin import cli
from tests.conftest import TEST_CREDENTIAL_KEY, TEST_PEPPER, TEST_RESULT_KEY

pytestmark = pytest.mark.db
KEY = re.compile(r"lgw\.(dev|prod)\.[a-z2-7]{12}\.[A-Za-z0-9_-]{43}")


@pytest.fixture
def admin(
    migrated_schema_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv("DATABASE_URL", migrated_schema_url)
    monkeypatch.setenv("PROJECT_KEY_PEPPER", TEST_PEPPER)
    monkeypatch.setenv("RESULT_ENCRYPTION_KEY", TEST_RESULT_KEY)
    monkeypatch.setenv("PROVIDER_CREDENTIAL_ENCRYPTION_KEY", TEST_CREDENTIAL_KEY)

    def run(*argv: str) -> tuple[int, str]:
        status = cli.main(list(argv))
        return status, capsys.readouterr().out

    return run


def test_register_prints_both_keys_once(admin) -> None:
    status, out = admin("project", "register", "personal_agent", "--display-name", "Agent")

    assert status == 0
    assert sorted(match.group(1) for match in KEY.finditer(out)) == ["dev", "prod"]
    _, shown = admin("project", "show", "personal_agent")
    _, exported = admin("project", "export", "personal_agent")
    assert KEY.search(shown) is None
    assert KEY.search(exported) is None


def test_list_show_and_history(admin) -> None:
    admin("project", "register", "personal_agent", "--display-name", "Agent")
    admin("project", "set-request", "personal_agent", "--env", "production", "--concurrency", "6")

    _, listed = admin("project", "list")
    _, shown = admin("project", "show", "personal_agent", "--version", "1")
    _, history = admin("project", "history", "personal_agent")

    assert "personal_agent" in listed and "active" in listed
    assert (
        json.loads(shown.split("\n", 1)[1].split("\nKeys:")[0])["request"]["production"][
            "concurrency"
        ]
        == 4
    )
    assert [line.split()[0] for line in history.splitlines()] == ["v1", "v2"]
    assert "set-request production" in history


def test_validation_failure_exits_non_zero_with_the_reason(admin) -> None:
    admin("project", "register", "personal_agent", "--display-name", "Agent")

    status, out = admin(
        "project", "set-request", "personal_agent", "--env", "production", "--concurrency", "0"
    )

    assert status == 1
    assert out.startswith("error: request.production.concurrency")


def test_profiles_and_status(admin) -> None:
    admin("project", "register", "personal_agent", "--display-name", "Agent")

    set_status, _ = admin(
        "project", "set-profile", "personal_agent", "chat", "--model", "openai-gpt-6-luna", "--max-output-tokens", "1200",
    )  # fmt: skip
    disable_status, _ = admin("project", "disable", "personal_agent")
    _, exported = admin("project", "export", "personal_agent")
    missing_status, missing = admin("project", "remove-profile", "personal_agent", "other")

    contract = json.loads(exported)
    assert (set_status, disable_status) == (0, 0)
    assert contract["profiles"] == {
        "chat": {"model": "openai-gpt-6-luna", "parameters": {"max_output_tokens": 1200}}
    }
    assert contract["status"] == "disabled"
    assert (missing_status, missing.strip()) == (
        1,
        "error: Project personal_agent has no profile other.",
    )


def test_import_round_trip_and_unknown_project(admin, tmp_path: Path) -> None:
    admin("project", "register", "personal_agent", "--display-name", "Agent")
    _, exported = admin("project", "export", "personal_agent")
    contract_file = tmp_path / "contract.json"
    contract_file.write_text(exported, encoding="utf-8")

    status, out = admin("project", "import", str(contract_file), "--note", "restore")
    ghost = json.loads(exported) | {"project_id": "ghost_project"}
    contract_file.write_text(json.dumps(ghost), encoding="utf-8")
    ghost_status, ghost_out = admin("project", "import", str(contract_file))

    assert status == 0
    assert out.splitlines() == [
        "Imported a new version of personal_agent; keys are unchanged.",
        "Grants unchanged.",
    ]
    assert ghost_status == 1
    assert "Register it first" in ghost_out


def test_rotate_and_revoke(admin) -> None:
    admin("project", "register", "personal_agent", "--display-name", "Agent")

    status, rotated = admin(
        "project", "rotate-key", "personal_agent", "--env", "production", "--grace-minutes", "30"
    )
    key_match = KEY.search(rotated)
    assert key_match is not None
    key_id = key_match.group(0).split(".")[2]
    revoke_status, _ = admin("project", "revoke-key", key_id)
    again_status, again = admin("project", "revoke-key", key_id)

    assert status == 0
    assert "keep working for 30 minutes" in rotated
    assert revoke_status == 0
    assert (again_status, again.strip()) == (1, f"error: No active key {key_id}.")


def test_register_recomputes_and_grants_show_lists_both_environments(admin) -> None:
    _, registered = admin("project", "register", "personal_agent", "--display-name", "Agent")
    status, shown = admin("grants", "show")

    assert "Grants recomputed: 2 grants, 0 reduced." in registered
    assert status == 0
    assert "personal_agent/production" in shown
    assert "personal_agent/development" in shown
    assert "concurrency 6/8" in shown
    assert "monthly budget 1.25/5.00 USD" in shown  # CLI defaults: 1.00 + 0.25


def test_set_weight_validates_and_recomputes(admin) -> None:
    admin("project", "register", "alpha", "--display-name", "Alpha")
    admin("project", "register", "beta", "--display-name", "Beta")

    status, out = admin("project", "set-weight", "alpha", "3")
    bad_status, bad = admin("project", "set-weight", "alpha", "0")
    ghost_status, ghost = admin("project", "set-weight", "ghost", "2")

    assert status == 0
    assert out.startswith("Weight of alpha is now 3.")
    assert (bad_status, bad.strip()) == (1, "error: Weight must be between 1 and 100.")
    assert (ghost_status, ghost.strip()) == (1, "error: Project ghost is not registered.")


def test_profiles_are_checked_against_the_catalogue(admin) -> None:
    admin("project", "register", "personal_agent", "--display-name", "Agent")

    temperature_status, temperature = admin(
        "project", "set-profile", "personal_agent", "chat", "--model", "openai-gpt-6-luna", "--reasoning-effort", "low", "--temperature", "0.7",
    )  # fmt: skip
    unknown_status, unknown = admin(
        "project", "set-profile", "personal_agent", "chat", "--model", "gpt-9",
    )  # fmt: skip

    assert temperature_status == 1
    assert "reasoning_effort: const" in temperature
    assert unknown_status == 1
    assert "alias is not registered" in unknown


def test_provider_credential_cli_is_write_only_and_excluded_from_exports(
    admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import sys

    admin("project", "register", "alpha", "--display-name", "Alpha")
    _, original = admin("project", "export", "alpha")
    secret = "synthetic-cli-provider-secret"
    monkeypatch.setattr(sys, "stdin", io.StringIO(secret + "\n"))
    status, registered = admin(
        "project", "credential", "set", "alpha", "--provider", "openai", "--stdin"
    )
    assert status == 0
    _, listed = admin("project", "credential", "list", "alpha")
    assert json.loads(listed)[0]["configured"] is True
    _, exported = admin("project", "export", "alpha")
    assert exported == original
    status, removed = admin("project", "credential", "remove", "alpha", "--provider", "openai")
    assert status == 0
    _, listed_again = admin("project", "credential", "list", "alpha")
    assert json.loads(listed_again)[0]["configured"] is False
    assert secret not in registered + listed + exported + removed + listed_again
