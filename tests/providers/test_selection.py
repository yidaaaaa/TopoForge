from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from topoforge.exceptions import ProviderFetchError
from topoforge.models import (
    AreaOfInterestInput,
    DatasetMetadata,
    DatasetType,
    TerrainMode,
)
from topoforge.providers import (
    CoverageInfo,
    ProviderDescriptor,
    ProviderSelectionError,
    ProviderSelectionPolicy,
    evaluate_providers,
    fetch_with_provider_selection,
)
from topoforge.raster import normalize_area_of_interest


@dataclass(frozen=True)
class FakeAcquisition:
    acquisition_manifest_path: Path


class FakeProvider:
    def __init__(
        self,
        provider_id: str,
        *,
        dataset_type: DatasetType,
        resolution_m: float,
        complete: bool = True,
        covered: bool = True,
        requires_api_key: bool = False,
        failure_probability: str = "low",
        estimated_download_bytes: int | None = 1000,
        license_id: str = "TEST-LICENSE",
        vertical_datum: str = "TEST-DATUM",
        fetch_error: Exception | None = None,
        leave_artifact_on_failure: bool = False,
    ) -> None:
        self.provider_id = provider_id
        self.dataset_type = dataset_type
        self.resolution_m = resolution_m
        self.complete = complete
        self.covered = covered
        self.requires_api_key = requires_api_key
        self.failure_probability = failure_probability
        self.estimated_download_bytes = estimated_download_bytes
        self.license_id = license_id
        self.vertical_datum = vertical_datum
        self.fetch_error = fetch_error
        self.leave_artifact_on_failure = leave_artifact_on_failure
        self.fetch_count = 0

    def probe(self, _aoi: Any) -> CoverageInfo:
        return CoverageInfo(
            covered=self.covered,
            complete=self.complete,
            dataset_type=self.dataset_type,
            horizontal_resolution_m=self.resolution_m,
            requires_api_key=self.requires_api_key,
            estimated_download_bytes=self.estimated_download_bytes,
            failure_probability=self.failure_probability,
            reason=[f"{self.provider_id} fixture probe"],
        )

    def metadata(self) -> DatasetMetadata:
        return DatasetMetadata(
            provider=self.provider_id,
            dataset_name=f"{self.provider_id} dataset",
            dataset_version="fixture-v1",
            dataset_type=self.dataset_type,
            horizontal_resolution_m=self.resolution_m,
            horizontal_crs="EPSG:4326",
            vertical_datum=self.vertical_datum,
            license=self.license_id,
            attribution="fixture",
        )

    def fetch(self, _aoi: Any, destination: Path) -> FakeAcquisition:
        self.fetch_count += 1
        if self.fetch_error is not None:
            if self.leave_artifact_on_failure:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"retained evidence")
            raise self.fetch_error
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.provider_id.encode())
        manifest = destination.with_suffix(destination.suffix + ".source_acquisition.json")
        manifest.write_text(
            json.dumps({"provider_id": self.provider_id}) + "\n",
            encoding="utf-8",
        )
        return FakeAcquisition(acquisition_manifest_path=manifest)


def descriptor(
    provider_id: str,
    dataset_type: DatasetType,
    *,
    implemented: bool = True,
    requires_api_key: bool = False,
) -> ProviderDescriptor:
    return ProviderDescriptor(
        provider_id=provider_id,
        name=f"{provider_id} fixture",
        implemented=implemented,
        requires_api_key=requires_api_key,
        dataset_types=[dataset_type],
        notes="offline selection fixture",
    )


@pytest.fixture
def aoi() -> Any:
    return normalize_area_of_interest(AreaOfInterestInput(bbox_wgs84=(101.2, 29.2, 101.3, 29.3)))


def test_semantic_match_outranks_nominal_resolution(aoi: Any) -> None:
    exact = FakeProvider("exact-dtm", dataset_type=DatasetType.DTM, resolution_m=90)
    fast_dsm = FakeProvider("fast-dsm", dataset_type=DatasetType.DSM, resolution_m=10)

    evaluations, ranked = evaluate_providers(
        aoi=aoi,
        providers={"exact-dtm": exact, "fast-dsm": fast_dsm},
        descriptors=[
            descriptor("fast-dsm", DatasetType.DSM),
            descriptor("exact-dtm", DatasetType.DTM),
        ],
        policy=ProviderSelectionPolicy(
            requested_terrain_mode=TerrainMode.DTM,
            allow_semantic_fallback=True,
        ),
    )

    assert ranked == ["exact-dtm", "fast-dsm"]
    assert {item.provider_id: item.semantic_status for item in evaluations} == {
        "fast-dsm": "fallback",
        "exact-dtm": "exact",
    }


def test_dtm_to_dsm_is_rejected_until_semantic_fallback_is_explicit(aoi: Any) -> None:
    provider = FakeProvider("dsm", dataset_type=DatasetType.DSM, resolution_m=30)
    descriptions = [descriptor("dsm", DatasetType.DSM)]

    evaluations, ranked = evaluate_providers(
        aoi=aoi,
        providers={"dsm": provider},
        descriptors=descriptions,
        policy=ProviderSelectionPolicy(requested_terrain_mode=TerrainMode.DTM),
    )
    assert ranked == []
    assert evaluations[0].status == "rejected"
    assert "semantic fallback is disabled" in evaluations[0].reasons[0]

    evaluations, ranked = evaluate_providers(
        aoi=aoi,
        providers={"dsm": provider},
        descriptors=descriptions,
        policy=ProviderSelectionPolicy(
            requested_terrain_mode=TerrainMode.DTM,
            allow_semantic_fallback=True,
        ),
    )
    assert ranked == ["dsm"]
    assert evaluations[0].semantic_status == "fallback"
    assert "explicit semantic fallback" in evaluations[0].reasons[0]


def test_incomplete_coverage_is_a_hard_rejection(aoi: Any) -> None:
    provider = FakeProvider(
        "partial",
        dataset_type=DatasetType.DTM,
        resolution_m=5,
        complete=False,
    )

    evaluations, ranked = evaluate_providers(
        aoi=aoi,
        providers={"partial": provider},
        descriptors=[descriptor("partial", DatasetType.DTM)],
        policy=ProviderSelectionPolicy(requested_terrain_mode=TerrainMode.DTM),
    )

    assert ranked == []
    assert evaluations[0].reasons[0] == "complete AOI coverage is required"


def test_missing_credential_and_unimplemented_candidates_are_recorded(aoi: Any) -> None:
    authenticated = FakeProvider(
        "auth",
        dataset_type=DatasetType.DTM,
        resolution_m=5,
        requires_api_key=True,
    )

    evaluations, ranked = evaluate_providers(
        aoi=aoi,
        providers={"auth": authenticated},
        descriptors=[
            descriptor("auth", DatasetType.DTM, requires_api_key=True),
            descriptor("planned", DatasetType.DTM, implemented=False),
        ],
        policy=ProviderSelectionPolicy(
            requested_terrain_mode=TerrainMode.DTM,
            allow_authenticated_providers=True,
        ),
    )

    assert ranked == []
    assert [(item.provider_id, item.status) for item in evaluations] == [
        ("auth", "rejected"),
        ("planned", "rejected"),
    ]
    assert evaluations[0].reasons == ["required provider credential is not available"]
    assert evaluations[1].reasons == ["provider is registered but not implemented"]


def test_license_vertical_datum_and_download_budget_are_hard_filters(aoi: Any) -> None:
    provider = FakeProvider(
        "filtered",
        dataset_type=DatasetType.DTM,
        resolution_m=10,
        license_id="TEST-LICENSE",
        vertical_datum="VDATUM-A",
        estimated_download_bytes=5000,
    )
    descriptions = [descriptor("filtered", DatasetType.DTM)]

    evaluations, ranked = evaluate_providers(
        aoi=aoi,
        providers={"filtered": provider},
        descriptors=descriptions,
        policy=ProviderSelectionPolicy(
            requested_terrain_mode=TerrainMode.DTM,
            allowed_license_ids=["OTHER-LICENSE"],
        ),
    )
    assert ranked == []
    assert "allowed_license_ids" in evaluations[0].reasons[0]

    evaluations, ranked = evaluate_providers(
        aoi=aoi,
        providers={"filtered": provider},
        descriptors=descriptions,
        policy=ProviderSelectionPolicy(
            requested_terrain_mode=TerrainMode.DTM,
            required_vertical_datum="VDATUM-B",
        ),
    )
    assert ranked == []
    assert "does not match required" in evaluations[0].reasons[0]

    evaluations, ranked = evaluate_providers(
        aoi=aoi,
        providers={"filtered": provider},
        descriptors=descriptions,
        policy=ProviderSelectionPolicy(
            requested_terrain_mode=TerrainMode.DTM,
            maximum_download_bytes=4999,
        ),
    )
    assert ranked == []
    assert "estimated download bytes" in evaluations[0].reasons[0]


def test_deterministic_tie_order_and_repeated_trace(aoi: Any, tmp_path: Path) -> None:
    descriptions = [
        descriptor("provider-b", DatasetType.DSM),
        descriptor("provider-a", DatasetType.DSM),
    ]
    policy = ProviderSelectionPolicy(requested_terrain_mode=TerrainMode.DSM)

    traces: list[dict[str, Any]] = []
    for index in range(2):
        providers = {
            "provider-a": FakeProvider(
                "provider-a",
                dataset_type=DatasetType.DSM,
                resolution_m=30,
            ),
            "provider-b": FakeProvider(
                "provider-b",
                dataset_type=DatasetType.DSM,
                resolution_m=30,
            ),
        }
        result = fetch_with_provider_selection(
            aoi=aoi,
            destination=tmp_path / f"run-{index}" / "aoi.tif",
            providers=providers,
            descriptors=descriptions,
            policy=policy,
        )
        traces.append(result.trace.model_dump(mode="json"))

    assert traces[0]["ranked_provider_ids"] == ["provider-b", "provider-a"]
    normalized = [
        {
            **trace,
            "fetch_attempts": [
                {**attempt, "retained_destination_artifacts": []}
                for attempt in trace["fetch_attempts"]
            ],
        }
        for trace in traces
    ]
    assert normalized[0] == normalized[1]


def test_fetch_failure_falls_back_and_writes_complete_manifest_trace(
    aoi: Any, tmp_path: Path
) -> None:
    first = FakeProvider(
        "first",
        dataset_type=DatasetType.DSM,
        resolution_m=10,
        fetch_error=ProviderFetchError("fixture outage"),
    )
    second = FakeProvider("second", dataset_type=DatasetType.DSM, resolution_m=30)
    destination = tmp_path / "aoi.tif"

    result = fetch_with_provider_selection(
        aoi=aoi,
        destination=destination,
        providers={"first": first, "second": second},
        descriptors=[
            descriptor("first", DatasetType.DSM),
            descriptor("second", DatasetType.DSM),
        ],
        policy=ProviderSelectionPolicy(requested_terrain_mode=TerrainMode.DSM),
    )

    assert result.trace.selected_provider == "second"
    assert [item.status for item in result.trace.fetch_attempts] == [
        "fetch-failed",
        "selected",
    ]
    manifest = json.loads(
        destination.with_suffix(".tif.source_acquisition.json").read_text(encoding="utf-8")
    )
    assert manifest["provider_selection"] == result.trace.model_dump(mode="json")
    assert manifest["provider_selection"]["fetch_attempts"][0]["error_type"] == (
        "ProviderFetchError"
    )


def test_all_fetch_failures_are_aggregated(aoi: Any, tmp_path: Path) -> None:
    providers = {
        "one": FakeProvider(
            "one",
            dataset_type=DatasetType.DSM,
            resolution_m=10,
            fetch_error=ProviderFetchError("one failed"),
        ),
        "two": FakeProvider(
            "two",
            dataset_type=DatasetType.DSM,
            resolution_m=20,
            fetch_error=OSError("two failed"),
        ),
    }

    with pytest.raises(ProviderSelectionError, match="all provider fetches failed") as caught:
        fetch_with_provider_selection(
            aoi=aoi,
            destination=tmp_path / "aoi.tif",
            providers=providers,
            descriptors=[
                descriptor("one", DatasetType.DSM),
                descriptor("two", DatasetType.DSM),
            ],
            policy=ProviderSelectionPolicy(requested_terrain_mode=TerrainMode.DSM),
        )

    assert caught.value.trace.outcome == "all-fetches-failed"
    assert [item.provider_id for item in caught.value.trace.fetch_attempts] == ["one", "two"]
    assert "one failed" in str(caught.value)
    assert "two failed" in str(caught.value)


def test_explicit_provider_mode_ignores_higher_ranked_auto_candidate(
    aoi: Any, tmp_path: Path
) -> None:
    first = FakeProvider("first", dataset_type=DatasetType.DSM, resolution_m=10)
    chosen = FakeProvider("chosen", dataset_type=DatasetType.DSM, resolution_m=90)

    result = fetch_with_provider_selection(
        aoi=aoi,
        destination=tmp_path / "explicit.tif",
        providers={"first": first, "chosen": chosen},
        descriptors=[
            descriptor("first", DatasetType.DSM),
            descriptor("chosen", DatasetType.DSM),
        ],
        policy=ProviderSelectionPolicy(
            requested_provider_id="chosen",
            requested_terrain_mode=TerrainMode.DSM,
        ),
    )

    assert result.trace.ranked_provider_ids == ["chosen"]
    assert result.trace.selected_provider == "chosen"
    assert first.fetch_count == 0
    assert chosen.fetch_count == 1


def test_retained_failed_destination_stops_fallback(aoi: Any, tmp_path: Path) -> None:
    first = FakeProvider(
        "first",
        dataset_type=DatasetType.DSM,
        resolution_m=10,
        fetch_error=ProviderFetchError("partial publication"),
        leave_artifact_on_failure=True,
    )
    second = FakeProvider("second", dataset_type=DatasetType.DSM, resolution_m=20)
    destination = tmp_path / "aoi.tif"

    with pytest.raises(ProviderSelectionError, match="left destination evidence") as caught:
        fetch_with_provider_selection(
            aoi=aoi,
            destination=destination,
            providers={"first": first, "second": second},
            descriptors=[
                descriptor("first", DatasetType.DSM),
                descriptor("second", DatasetType.DSM),
            ],
            policy=ProviderSelectionPolicy(requested_terrain_mode=TerrainMode.DSM),
        )

    assert destination.read_bytes() == b"retained evidence"
    assert second.fetch_count == 0
    assert caught.value.trace.outcome == "failed-with-retained-evidence"
