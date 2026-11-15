# SPDX-License-Identifier: MIT

"""The version in the code has release notes, and the release index lists them."""

from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASES = ROOT / "docs" / "releases"


def project_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def test_installed_package_reports_the_project_version() -> None:
    assert importlib.metadata.version("agent-application") == project_version()


def test_release_notes_exist_for_the_project_version() -> None:
    version = project_version()
    notes = RELEASES / f"v{version}.md"

    assert notes.is_file(), f"{notes} is missing"
    heading = next(
        line for line in notes.read_text().splitlines() if line.startswith("# ")
    )
    assert f"v{version}" in heading


def test_release_index_lists_the_project_version() -> None:
    version = project_version()

    assert f"(v{version}.md)" in (RELEASES / "README.md").read_text()
