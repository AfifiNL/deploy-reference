from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "cited-answers"


def need(tool: str) -> str:
    path = shutil.which(tool)
    if path is None:
        pytest.fail(f"{tool} not found: run `make tools` (or use `make check`)")
    return path


def render(*args: str) -> list[dict[str, Any]]:
    out = subprocess.run(
        [need("helm"), "template", "ca", str(CHART), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def by_kind(docs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [d for d in docs if d["kind"] == kind]


def api_deployment(docs: list[dict[str, Any]]) -> dict[str, Any]:
    (dep,) = [d for d in by_kind(docs, "Deployment") if d["metadata"]["name"] == "ca-cited-answers"]
    return dep
