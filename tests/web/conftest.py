from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from topoforge.models import BuildConfig, SamplingMode
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff
from topoforge.web.models import JobCreateRequest, WebAppConfig
from topoforge.workflow import WorkflowLaunchConfig


@pytest.fixture
def web_static_dir(tmp_path: Path) -> Path:
    root = tmp_path / "static"
    assets = root / "assets"
    assets.mkdir(parents=True)
    (assets / "app.js").write_text("document.body.dataset.ready = 'true';\n", encoding="utf-8")
    (root / "index.html").write_text(
        '<!doctype html><html lang="en"><body>TopoForge Web</body>'
        '<script type="module" src="/assets/app.js"></script></html>\n',
        encoding="utf-8",
    )
    (root / "asset-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "topoforge-web-assets-v1",
                "assets": ["index.html", "assets/app.js"],
                "languages": ["zh-CN", "en"],
                "frameworks": ["React", "MapLibre", "Three.js"],
                "sha256": {
                    name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                    for name in ("index.html", "assets/app.js")
                },
                "sizes": {
                    name: (root / name).stat().st_size for name in ("index.html", "assets/app.js")
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def web_config(tmp_path: Path) -> WebAppConfig:
    input_root = tmp_path / "inputs"
    input_root.mkdir()
    return WebAppConfig(
        state_dir=tmp_path / "state",
        workspace_root=tmp_path / "workspaces",
        input_roots=(input_root,),
        poll_interval_seconds=0.05,
    )


def make_job_request(
    config: WebAppConfig,
    *,
    name: str = "job",
    missing_source: bool = False,
) -> JobCreateRequest:
    input_root = config.input_roots[0]
    source = input_root / ("missing.tif" if missing_source else f"{name}.tif")
    if not missing_source:
        create_synthetic_geotiff(
            source,
            SyntheticTerrain.SADDLE,
            rows=12,
            columns=16,
            pixel_size_m=20.0,
        )
    workspace = config.workspace_root / name
    launch = WorkflowLaunchConfig(
        workspace_dir=workspace,
        build=BuildConfig(
            dem_path=source,
            output_dir=workspace,
            model_width_mm=40.0,
            max_height_mm=20.0,
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
            max_grid_cells=10_000,
            max_estimated_triangles=50_000,
            resource_budget_mode="strict",
        ),
        maximum_tile_width_mm=180.0,
        maximum_tile_depth_mm=180.0,
        slicing_enabled=False,
    )
    return JobCreateRequest(launch=launch)
