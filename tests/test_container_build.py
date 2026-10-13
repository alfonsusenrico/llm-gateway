"""Guards for the image build, which `./scripts/check` does not run."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_every_workspace_member_manifest_reaches_the_image_build() -> None:
    # `uv sync --locked` must parse every workspace member, even with `--no-dev`.
    members = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["uv"]["workspace"][
        "members"
    ]
    dockerfile = (ROOT / "Dockerfile").read_text()
    dockerignore = (ROOT / ".dockerignore").read_text().splitlines()

    for member in members:
        assert f"COPY {member}/pyproject.toml" in dockerfile
        assert f"!{member}/pyproject.toml" in dockerignore


def test_files_the_manifest_declares_reach_the_image_build() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    dockerignore = (ROOT / ".dockerignore").read_text().splitlines()

    for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE"):
        assert f"!{name}" in dockerignore
        assert name in dockerfile
