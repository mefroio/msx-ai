#!/usr/bin/env python3
"""Generate the Z80 banner from the Python distribution version."""

from __future__ import annotations

import ast
from pathlib import Path
import re


class VersionIncludeError(ValueError):
    """The canonical project version cannot produce an assembly include."""


def read_project_version(repository: Path) -> str:
    source = repository / "server" / "_version.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    values = [
        statement.value.value
        for statement in tree.body
        if isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and statement.targets[0].id == "__version__"
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    ]
    if len(values) != 1 or re.fullmatch(
            r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
            values[0]) is None:
        raise VersionIncludeError(
            "project version must be one literal stable SemVer value")
    return values[0]


def render_version_include(version: str) -> bytes:
    return (
        "; Generated from server/_version.py; do not edit.\n"
        f'db "MSX-AI MCP Agent {version}",13,10\n'
    ).encode("ascii")


def materialize_version_include(repository: Path) -> Path:
    destination = repository / "work" / "agent" / "build" / \
        "MSXAI_VERSION.INC"
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = render_version_include(read_project_version(repository))
    if not destination.exists() or destination.read_bytes() != payload:
        destination.write_bytes(payload)
    return destination

