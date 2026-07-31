"""Deterministic, explainable provider evaluation and fetch fallback."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from topoforge.exceptions import ProviderFetchError
from topoforge.models import AreaOfInterest, DatasetMetadata, DatasetType, TerrainMode
from topoforge.providers.protocol import ElevationProvider, ProviderDescriptor


class ProviderSelectionPolicy(BaseModel):
    """User-visible hard filters and deterministic ranking preferences."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requested_provider_id: str = "auto"
    requested_terrain_mode: TerrainMode = TerrainMode.BEST_AVAILABLE
    require_complete_coverage: bool = True
    allow_authenticated_providers: bool = False
    available_credentials: list[str] = Field(default_factory=list)
    allow_semantic_fallback: bool = False
    maximum_horizontal_resolution_m: float | None = Field(default=None, gt=0)
    maximum_download_bytes: int | None = Field(default=None, gt=0)
    required_vertical_datum: str | None = None
    preferred_provider_ids: list[str] = Field(default_factory=list)
    allowed_license_ids: list[str] | None = None

    @model_validator(mode="after")
    def validate_identifiers(self) -> ProviderSelectionPolicy:
        """Reject ambiguous duplicate preference and credential declarations."""
        if not self.requested_provider_id.strip():
            raise ValueError("requested_provider_id must be 'auto' or a non-empty provider id")
        for name, values in (
            ("available_credentials", self.available_credentials),
            ("preferred_provider_ids", self.preferred_provider_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must not contain duplicate provider ids")
        return self


class ProviderEvaluation(BaseModel):
    """One registry candidate's probe result, hard-filter decision, and rank."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str
    provider_name: str
    registry_order: int = Field(ge=0)
    status: str
    reasons: list[str]
    dataset: dict[str, Any] | None = None
    coverage: dict[str, Any] | None = None
    semantic_status: str | None = None
    rank: tuple[int, int, float, int, int, int, int, str] | None = None


class ProviderFetchAttempt(BaseModel):
    """One literal provider fetch attempt after deterministic ranking."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str
    rank_order: int = Field(ge=1)
    status: str
    error_type: str | None = None
    error_message: str | None = None
    retained_destination_artifacts: list[str] = Field(default_factory=list)


class ProviderSelectionTrace(BaseModel):
    """Complete, deterministic selection and fallback history."""

    model_config = ConfigDict(extra="forbid")

    policy: ProviderSelectionPolicy
    evaluations: list[ProviderEvaluation]
    ranked_provider_ids: list[str]
    fetch_attempts: list[ProviderFetchAttempt]
    selected_provider: str | None = None
    selected_dataset: str | None = None
    outcome: str
    selection_reasons: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ProviderFetchSelection:
    """Successful provider result paired with its recorded trace."""

    acquisition: object
    trace: ProviderSelectionTrace


class ProviderSelectionError(ProviderFetchError):
    """Aggregated selection/fetch failure with a machine-readable trace."""

    def __init__(self, message: str, trace: ProviderSelectionTrace) -> None:
        super().__init__(message)
        self.trace = trace


def _semantic_rank(
    requested: TerrainMode,
    actual: DatasetType,
    *,
    allow_fallback: bool,
) -> tuple[int | None, str, str]:
    if requested is TerrainMode.BEST_AVAILABLE:
        ranking = {
            DatasetType.DTM: 0,
            DatasetType.DSM: 1,
            DatasetType.MIXED: 2,
            DatasetType.BATHYMETRY: 3,
            DatasetType.UNKNOWN: 4,
        }
        return (
            ranking[actual],
            "best-available",
            f"{actual.value} is eligible for best-available terrain mode",
        )
    requested_type = DatasetType(requested.value)
    if actual is requested_type:
        return 0, "exact", f"dataset semantics exactly match requested {requested.value}"
    fallback_order: dict[TerrainMode, tuple[DatasetType, ...]] = {
        TerrainMode.DTM: (DatasetType.DSM, DatasetType.MIXED, DatasetType.UNKNOWN),
        TerrainMode.DSM: (DatasetType.DTM, DatasetType.MIXED, DatasetType.UNKNOWN),
        TerrainMode.BATHYMETRY: (DatasetType.MIXED, DatasetType.UNKNOWN),
        TerrainMode.BEST_AVAILABLE: (),
    }
    candidates = fallback_order[requested]
    if allow_fallback and actual in candidates:
        return (
            candidates.index(actual) + 1,
            "fallback",
            f"explicit semantic fallback permits {requested.value} to {actual.value} downgrade",
        )
    return (
        None,
        "incompatible",
        f"dataset semantics {actual.value} do not match requested {requested.value}; "
        "semantic fallback is disabled",
    )


def _risk_rank(value: str) -> int:
    normalized = value.strip().lower()
    if normalized in {"low", "unlikely"}:
        return 0
    if normalized in {"medium", "moderate"}:
        return 1
    if normalized in {"high", "likely"}:
        return 2
    return 3


def _license_rejection(metadata: DatasetMetadata, policy: ProviderSelectionPolicy) -> str | None:
    license_id = metadata.license.strip()
    if license_id.lower() in {"", "unknown", "unverified", "none"}:
        return "dataset license is missing or unverified"
    if policy.allowed_license_ids is not None and license_id not in policy.allowed_license_ids:
        return f"dataset license {license_id!r} is not in allowed_license_ids"
    return None


def _evaluation(
    descriptor: ProviderDescriptor,
    order: int,
    *,
    status: str,
    reasons: list[str],
    metadata: DatasetMetadata | None = None,
    coverage: Any | None = None,
    semantic_status: str | None = None,
    rank: tuple[int, int, float, int, int, int, int, str] | None = None,
) -> ProviderEvaluation:
    return ProviderEvaluation(
        provider_id=descriptor.provider_id,
        provider_name=descriptor.name,
        registry_order=order,
        status=status,
        reasons=reasons,
        dataset=metadata.model_dump(mode="json") if metadata is not None else None,
        coverage=(
            coverage.model_dump(mode="json")
            if coverage is not None and hasattr(coverage, "model_dump")
            else None
        ),
        semantic_status=semantic_status,
        rank=rank,
    )


def evaluate_providers(
    *,
    aoi: AreaOfInterest,
    providers: dict[str, ElevationProvider],
    descriptors: list[ProviderDescriptor],
    policy: ProviderSelectionPolicy,
) -> tuple[list[ProviderEvaluation], list[str]]:
    """Evaluate and rank registry candidates without fetching destination artifacts."""
    descriptor_ids = [item.provider_id for item in descriptors]
    if len(descriptor_ids) != len(set(descriptor_ids)):
        raise ValueError("provider descriptors must have unique provider ids")
    if (
        policy.requested_provider_id != "auto"
        and policy.requested_provider_id not in descriptor_ids
    ):
        raise ValueError(f"unknown provider id: {policy.requested_provider_id}")

    evaluations: list[ProviderEvaluation] = []
    eligible: list[ProviderEvaluation] = []
    preference_default = len(policy.preferred_provider_ids)
    for order, descriptor in enumerate(descriptors):
        if policy.requested_provider_id != "auto" and (
            descriptor.provider_id != policy.requested_provider_id
        ):
            continue
        if not descriptor.implemented:
            evaluations.append(
                _evaluation(
                    descriptor,
                    order,
                    status="rejected",
                    reasons=["provider is registered but not implemented"],
                )
            )
            continue
        provider = providers.get(descriptor.provider_id)
        if provider is None:
            evaluations.append(
                _evaluation(
                    descriptor,
                    order,
                    status="unavailable",
                    reasons=["implemented provider instance is not available in this command"],
                )
            )
            continue
        if descriptor.requires_api_key and not policy.allow_authenticated_providers:
            evaluations.append(
                _evaluation(
                    descriptor,
                    order,
                    status="rejected",
                    reasons=["authenticated providers are disabled by selection policy"],
                )
            )
            continue
        if (
            descriptor.requires_api_key
            and descriptor.provider_id not in policy.available_credentials
        ):
            evaluations.append(
                _evaluation(
                    descriptor,
                    order,
                    status="rejected",
                    reasons=["required provider credential is not available"],
                )
            )
            continue
        try:
            metadata = provider.metadata()
        except Exception as exc:
            evaluations.append(
                _evaluation(
                    descriptor,
                    order,
                    status="metadata-failed",
                    reasons=[f"metadata failed with {type(exc).__name__}: {exc}"],
                )
            )
            continue
        license_rejection = _license_rejection(metadata, policy)
        if license_rejection is not None:
            evaluations.append(
                _evaluation(
                    descriptor,
                    order,
                    status="rejected",
                    reasons=[license_rejection],
                    metadata=metadata,
                )
            )
            continue
        if (
            policy.required_vertical_datum is not None
            and metadata.vertical_datum != policy.required_vertical_datum
        ):
            evaluations.append(
                _evaluation(
                    descriptor,
                    order,
                    status="rejected",
                    reasons=[
                        f"vertical datum {metadata.vertical_datum!r} does not match required "
                        f"{policy.required_vertical_datum!r}"
                    ],
                    metadata=metadata,
                )
            )
            continue
        try:
            coverage = provider.probe(aoi)
        except Exception as exc:
            evaluations.append(
                _evaluation(
                    descriptor,
                    order,
                    status="probe-failed",
                    reasons=[f"coverage probe failed with {type(exc).__name__}: {exc}"],
                    metadata=metadata,
                )
            )
            continue
        if not coverage.covered:
            evaluations.append(
                _evaluation(
                    descriptor,
                    order,
                    status="rejected",
                    reasons=["provider does not cover the normalized AOI", *coverage.reason],
                    metadata=metadata,
                    coverage=coverage,
                )
            )
            continue
        if policy.require_complete_coverage and not coverage.complete:
            evaluations.append(
                _evaluation(
                    descriptor,
                    order,
                    status="rejected",
                    reasons=["complete AOI coverage is required", *coverage.reason],
                    metadata=metadata,
                    coverage=coverage,
                )
            )
            continue
        semantic_rank, semantic_status, semantic_reason = _semantic_rank(
            policy.requested_terrain_mode,
            coverage.dataset_type,
            allow_fallback=policy.allow_semantic_fallback,
        )
        if semantic_rank is None:
            evaluations.append(
                _evaluation(
                    descriptor,
                    order,
                    status="rejected",
                    reasons=[semantic_reason],
                    metadata=metadata,
                    coverage=coverage,
                    semantic_status=semantic_status,
                )
            )
            continue
        resolution = coverage.horizontal_resolution_m or metadata.horizontal_resolution_m
        if policy.maximum_horizontal_resolution_m is not None and (
            resolution is None or resolution > policy.maximum_horizontal_resolution_m
        ):
            value = "unknown" if resolution is None else f"{resolution:g} m"
            evaluations.append(
                _evaluation(
                    descriptor,
                    order,
                    status="rejected",
                    reasons=[
                        f"horizontal resolution {value} exceeds required maximum "
                        f"{policy.maximum_horizontal_resolution_m:g} m"
                    ],
                    metadata=metadata,
                    coverage=coverage,
                    semantic_status=semantic_status,
                )
            )
            continue
        if policy.maximum_download_bytes is not None and (
            coverage.estimated_download_bytes is None
            or coverage.estimated_download_bytes > policy.maximum_download_bytes
        ):
            value = (
                "unknown"
                if coverage.estimated_download_bytes is None
                else str(coverage.estimated_download_bytes)
            )
            evaluations.append(
                _evaluation(
                    descriptor,
                    order,
                    status="rejected",
                    reasons=[
                        f"estimated download bytes {value} exceed or cannot satisfy maximum "
                        f"{policy.maximum_download_bytes}"
                    ],
                    metadata=metadata,
                    coverage=coverage,
                    semantic_status=semantic_status,
                )
            )
            continue
        preference_rank = (
            policy.preferred_provider_ids.index(descriptor.provider_id)
            if descriptor.provider_id in policy.preferred_provider_ids
            else preference_default
        )
        rank = (
            semantic_rank,
            0 if coverage.complete else 1,
            float(resolution) if resolution is not None else 1.0e100,
            1 if descriptor.requires_api_key else 0,
            _risk_rank(coverage.failure_probability),
            preference_rank,
            order,
            descriptor.provider_id,
        )
        reasons = [
            semantic_reason,
            "coverage is complete" if coverage.complete else "coverage is partial but allowed",
            (
                f"horizontal resolution is {resolution:g} m"
                if resolution is not None
                else "horizontal resolution is unknown"
            ),
            *coverage.reason,
        ]
        item = _evaluation(
            descriptor,
            order,
            status="eligible",
            reasons=reasons,
            metadata=metadata,
            coverage=coverage,
            semantic_status=semantic_status,
            rank=rank,
        )
        evaluations.append(item)
        eligible.append(item)

    eligible.sort(key=lambda item: item.rank or (999, 999, float("inf"), 9, 9, 9, 9, ""))
    return evaluations, [item.provider_id for item in eligible]


def _destination_artifacts(destination: Path) -> set[Path]:
    parent = destination.parent
    if not parent.exists():
        return set()
    prefixes = (destination.name, destination.stem + ".", destination.stem + "-")
    return {
        path.resolve()
        for path in parent.iterdir()
        if path.is_file() and any(path.name.startswith(prefix) for prefix in prefixes)
    }


def _write_trace_to_manifest(acquisition: object, trace: ProviderSelectionTrace) -> None:
    raw_path = getattr(acquisition, "acquisition_manifest_path", None)
    if raw_path is None:
        return
    path = Path(raw_path).expanduser().resolve()
    if not path.is_file():
        raise ProviderFetchError(f"selected provider acquisition manifest is missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderFetchError(
            f"selected provider acquisition manifest is not valid JSON: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderFetchError("selected provider acquisition manifest root is not an object")
    payload["provider_selection"] = trace.model_dump(mode="json")
    temporary = path.with_name(f".{path.name}.provider-selection.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        json.loads(temporary.read_text(encoding="utf-8"))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def fetch_with_provider_selection(
    *,
    aoi: AreaOfInterest,
    destination: Path,
    providers: dict[str, ElevationProvider],
    descriptors: list[ProviderDescriptor],
    policy: ProviderSelectionPolicy,
) -> ProviderFetchSelection:
    """Rank providers, attempt eligible fetches, and persist the exact fallback trace."""
    destination = destination.expanduser().resolve()
    evaluations, ranked_ids = evaluate_providers(
        aoi=aoi,
        providers=providers,
        descriptors=descriptors,
        policy=policy,
    )
    attempts: list[ProviderFetchAttempt] = []
    if not ranked_ids:
        trace = ProviderSelectionTrace(
            policy=policy,
            evaluations=evaluations,
            ranked_provider_ids=[],
            fetch_attempts=[],
            outcome="no-eligible-provider",
            selection_reasons=["every registered candidate failed a hard selection requirement"],
        )
        raise ProviderSelectionError("no provider satisfied the selection policy", trace)

    evaluation_by_id = {item.provider_id: item for item in evaluations}
    for rank_order, provider_id in enumerate(ranked_ids, start=1):
        before = _destination_artifacts(destination)
        try:
            acquisition = providers[provider_id].fetch(aoi, destination)
        except Exception as exc:
            retained = sorted(str(path) for path in (_destination_artifacts(destination) - before))
            attempts.append(
                ProviderFetchAttempt(
                    provider_id=provider_id,
                    rank_order=rank_order,
                    status="fetch-failed-artifacts-retained" if retained else "fetch-failed",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    retained_destination_artifacts=retained,
                )
            )
            if retained:
                trace = ProviderSelectionTrace(
                    policy=policy,
                    evaluations=evaluations,
                    ranked_provider_ids=ranked_ids,
                    fetch_attempts=attempts,
                    outcome="failed-with-retained-evidence",
                    selection_reasons=[
                        f"{provider_id} left destination evidence after failure; fallback stopped "
                        "to avoid overwriting it"
                    ],
                )
                raise ProviderSelectionError(
                    f"provider {provider_id} failed and left destination evidence", trace
                ) from exc
            continue

        evaluation = evaluation_by_id[provider_id]
        dataset_name = None
        acquired_dataset = getattr(acquisition, "dataset", None)
        if isinstance(acquired_dataset, DatasetMetadata):
            dataset_name = acquired_dataset.dataset_name
        elif evaluation.dataset is not None:
            raw_name = evaluation.dataset.get("dataset_name")
            dataset_name = str(raw_name) if raw_name is not None else None
        attempts.append(
            ProviderFetchAttempt(
                provider_id=provider_id,
                rank_order=rank_order,
                status="selected",
            )
        )
        reasons = [
            f"{provider_id} ranked {rank_order} after semantic, coverage, resolution, credential, "
            "operational-risk, preference, and stable registry tie-break rules",
            *evaluation.reasons,
        ]
        if any(item.status == "fetch-failed" for item in attempts[:-1]):
            reasons.append(
                "higher-ranked provider fetches failed without leaving destination evidence; "
                "fallback continued"
            )
        trace = ProviderSelectionTrace(
            policy=policy,
            evaluations=evaluations,
            ranked_provider_ids=ranked_ids,
            fetch_attempts=attempts,
            selected_provider=provider_id,
            selected_dataset=dataset_name,
            outcome="selected",
            selection_reasons=reasons,
        )
        try:
            _write_trace_to_manifest(acquisition, trace)
        except Exception as exc:
            retained = sorted(str(path) for path in _destination_artifacts(destination))
            attempts[-1] = ProviderFetchAttempt(
                provider_id=provider_id,
                rank_order=rank_order,
                status="manifest-recording-failed",
                error_type=type(exc).__name__,
                error_message=str(exc),
                retained_destination_artifacts=retained,
            )
            failed_trace = trace.model_copy(
                update={
                    "fetch_attempts": attempts,
                    "selected_provider": None,
                    "selected_dataset": None,
                    "outcome": "failed-with-retained-evidence",
                    "selection_reasons": [
                        "provider fetch succeeded but the complete selection trace was not "
                        "recorded; retained evidence was not overwritten"
                    ],
                }
            )
            raise ProviderSelectionError(
                "provider selection trace could not be recorded in source acquisition evidence",
                failed_trace,
            ) from exc
        return ProviderFetchSelection(acquisition=acquisition, trace=trace)

    trace = ProviderSelectionTrace(
        policy=policy,
        evaluations=evaluations,
        ranked_provider_ids=ranked_ids,
        fetch_attempts=attempts,
        outcome="all-fetches-failed",
        selection_reasons=["all eligible providers failed before publishing destination evidence"],
    )
    summary = "; ".join(
        f"{item.provider_id}: {item.error_type}: {item.error_message}" for item in attempts
    )
    raise ProviderSelectionError(f"all provider fetches failed ({summary})", trace)
