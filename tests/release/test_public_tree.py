from __future__ import annotations

from pathlib import Path

import pytest
from scripts.verify_public_tree import audit_public_tree, tracked_paths


@pytest.mark.parametrize(
    "forbidden_path",
    [
        "AGENTS.md",
        ".agent/STATE.md",
        ".agents/session.json",
        ".codex/settings.json",
        "cache/provider.bin",
        "downloads/source.tif",
        "outputs/" + "local-web/state/job.json",
        "artifacts/verification/topoforge-main-web-job.json",
    ],
)
def test_public_tree_rejects_local_only_paths(tmp_path: Path, forbidden_path: str) -> None:
    report = audit_public_tree(tmp_path, [forbidden_path])
    assert report["required_checks_passed"] is False
    assert report["violations"][0]["path"] == forbidden_path


def test_public_tree_rejects_local_runtime_references(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    marker = "outputs/" + "local-web/state"
    path.write_text(f'{{"path":"{marker}"}}\n', encoding="utf-8")
    report = audit_public_tree(tmp_path, ["report.json"])
    assert report["required_checks_passed"] is False
    assert "local runtime marker" in report["violations"][0]["reason"]


def test_repository_public_tree_is_clean() -> None:
    root = Path(__file__).parents[2]
    report = audit_public_tree(root, tracked_paths(root))
    assert report["required_checks_passed"] is True
    assert report["violations"] == []
