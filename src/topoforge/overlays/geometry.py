"""Deterministic projection, clipping, draping, and mesh construction."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import rasterio
import trimesh
from affine import Affine
from PIL import Image, ImageDraw, ImageFont
from pyproj import CRS, Transformer
from rasterio.features import shapes
from rasterio.transform import xy
from shapely import affinity
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Point,
    Polygon,
    box,
    shape,
)
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union

from topoforge.exceptions import ConfigurationError
from topoforge.models import ScalingResult
from topoforge.overlays.models import OverlayKind, OverlaySourceConfig
from topoforge.overlays.sources import ParsedOverlayFeature

type FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class TerrainSurface:
    """Exact relationship between processed samples and manufacturing coordinates."""

    processed_crs: str
    transform: Affine
    elevations_m_north: npt.NDArray[np.float32]
    elevations_mm_south: FloatArray
    original_nodata_north: npt.NDArray[np.bool_]
    scaling: ScalingResult
    model_width_mm: float
    model_depth_mm: float
    metric_bounds_at_samples: tuple[float, float, float, float]
    nodata_geometry_mm: BaseGeometry

    @property
    def rows(self) -> int:
        return int(self.elevations_m_north.shape[0])

    @property
    def columns(self) -> int:
        return int(self.elevations_m_north.shape[1])

    @property
    def model_bounds(self) -> Polygon:
        return box(0.0, 0.0, self.model_width_mm, self.model_depth_mm)

    def metric_to_model(self, geometry: BaseGeometry) -> BaseGeometry:
        """Map processed metric coordinates to the exact mesh sample frame."""
        west, south, east, north = self.metric_bounds_at_samples
        scale_x = self.model_width_mm / (east - west)
        scale_y = self.model_depth_mm / (north - south)
        return affinity.affine_transform(
            geometry,
            [scale_x, 0.0, 0.0, scale_y, -west * scale_x, -south * scale_y],
        )

    def surface_z_mm(self, points_xy_mm: npt.ArrayLike) -> FloatArray:
        """Interpolate the same fixed-diagonal triangles used by the terrain mesh."""
        points = np.asarray(points_xy_mm, dtype=np.float64)
        if points.ndim != 2 or points.shape[1] != 2:
            raise ValueError("surface points must have shape (n, 2)")
        if not bool(np.all(np.isfinite(points))):
            raise ValueError("surface points must be finite")
        tolerance = 1e-9
        if (
            bool(np.any(points[:, 0] < -tolerance))
            or bool(np.any(points[:, 0] > self.model_width_mm + tolerance))
            or bool(np.any(points[:, 1] < -tolerance))
            or bool(np.any(points[:, 1] > self.model_depth_mm + tolerance))
        ):
            raise ConfigurationError("overlay geometry extends outside the model surface")
        x = np.clip(points[:, 0], 0.0, self.model_width_mm)
        y = np.clip(points[:, 1], 0.0, self.model_depth_mm)
        gx = x * (self.columns - 1) / self.model_width_mm
        gy = y * (self.rows - 1) / self.model_depth_mm
        column = np.minimum(np.floor(gx).astype(np.int64), self.columns - 2)
        row = np.minimum(np.floor(gy).astype(np.int64), self.rows - 2)
        u = gx - column
        v = gy - row
        at_east = gx >= self.columns - 1
        at_north = gy >= self.rows - 1
        column[at_east] = self.columns - 2
        row[at_north] = self.rows - 2
        u[at_east] = 1.0
        v[at_north] = 1.0

        heights = self.elevations_mm_south
        lower_left = heights[row, column]
        lower_right = heights[row, column + 1]
        upper_left = heights[row + 1, column]
        upper_right = heights[row + 1, column + 1]
        lower_triangle = v <= u
        result = np.empty(len(points), dtype=np.float64)
        result[lower_triangle] = (
            (1.0 - u[lower_triangle]) * lower_left[lower_triangle]
            + (u[lower_triangle] - v[lower_triangle]) * lower_right[lower_triangle]
            + v[lower_triangle] * upper_right[lower_triangle]
        )
        upper = ~lower_triangle
        result[upper] = (
            (1.0 - v[upper]) * lower_left[upper]
            + u[upper] * upper_right[upper]
            + (v[upper] - u[upper]) * upper_left[upper]
        )
        return result


@dataclass(frozen=True, slots=True)
class ModelOverlayFeature:
    """One clipped feature in +X East/+Y North model millimetres."""

    feature_id: str
    geometry: BaseGeometry
    properties: dict[str, Any]
    label: str | None = None


@dataclass(frozen=True, slots=True)
class FeatureTransformResult:
    """Clipping measurements and resulting model-frame features."""

    features: tuple[ModelOverlayFeature, ...]
    input_feature_count: int
    clipped_feature_count: int
    dropped_feature_count: int
    input_length_m: float
    clipped_length_m: float


def build_terrain_surface(
    processed_dem_path: Path,
    nodata_mask_path: Path,
    scaling: ScalingResult,
) -> TerrainSurface:
    """Reopen the verified processed raster and construct exact model mapping."""
    with rasterio.open(processed_dem_path) as dataset:
        if dataset.count != 1 or dataset.crs is None:
            raise ConfigurationError("processed DEM must be single-band with a CRS")
        if abs(dataset.transform.b) > 1e-12 or abs(dataset.transform.d) > 1e-12:
            raise ConfigurationError("processed DEM must be north-up before overlay mapping")
        elevations = dataset.read(1).astype(np.float32, copy=False)
        if not bool(np.all(np.isfinite(elevations))):
            raise ConfigurationError("processed DEM contains non-finite elevations")
        transform_value = dataset.transform
        processed_crs = str(dataset.crs)
        north_x, north_y = xy(transform_value, 0, 0, offset="center")
        south_x, south_y = xy(
            transform_value, dataset.height - 1, dataset.width - 1, offset="center"
        )
    with rasterio.open(nodata_mask_path) as dataset:
        nodata = dataset.read(1)
        if nodata.shape != elevations.shape:
            raise ConfigurationError("original NoData mask shape differs from processed DEM")
        values = {int(value) for value in np.unique(nodata)}
        if not values.issubset({0, 1}):
            raise ConfigurationError("original NoData mask must contain only 0 and 1")
    from topoforge.scaling import apply_vertical_scale

    elevations_mm_south = np.flipud(apply_vertical_scale(elevations, scaling))
    west = min(float(north_x), float(south_x))
    east = max(float(north_x), float(south_x))
    south = min(float(north_y), float(south_y))
    north = max(float(north_y), float(south_y))
    rows, columns = elevations.shape
    dx_mm = scaling.model_width_mm / (columns - 1)
    dy_mm = scaling.model_depth_mm / (rows - 1)
    mask_transform = Affine(
        dx_mm, 0.0, -dx_mm / 2.0, 0.0, -dy_mm, scaling.model_depth_mm + dy_mm / 2.0
    )
    nodata_parts: list[BaseGeometry] = []
    nodata_bool = nodata.astype(bool)
    if bool(np.any(nodata_bool)):
        for geometry_mapping, value in shapes(
            nodata.astype(np.uint8), mask=nodata_bool, transform=mask_transform
        ):
            if int(value) == 1:
                nodata_parts.append(shape(geometry_mapping))
    nodata_geometry = (
        unary_union(nodata_parts).intersection(
            box(0.0, 0.0, scaling.model_width_mm, scaling.model_depth_mm)
        )
        if nodata_parts
        else GeometryCollection()
    )
    return TerrainSurface(
        processed_crs=processed_crs,
        transform=transform_value,
        elevations_m_north=elevations,
        elevations_mm_south=elevations_mm_south,
        original_nodata_north=nodata_bool,
        scaling=scaling,
        model_width_mm=scaling.model_width_mm,
        model_depth_mm=scaling.model_depth_mm,
        metric_bounds_at_samples=(west, south, east, north),
        nodata_geometry_mm=nodata_geometry,
    )


def _line_parts(geometry: BaseGeometry) -> list[LineString]:
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return list(geometry.geoms)
    if isinstance(geometry, GeometryCollection):
        result: list[LineString] = []
        for child in geometry.geoms:
            result.extend(_line_parts(child))
        return result
    return []


def transform_features_to_model(
    surface: TerrainSurface,
    source: OverlaySourceConfig,
    parsed: tuple[ParsedOverlayFeature, ...],
    *,
    clip_to_model: bool,
) -> FeatureTransformResult:
    """Project, clip, simplify, and split features into model coordinates."""
    try:
        transformer = Transformer.from_crs(
            CRS.from_user_input(source.source_crs),
            CRS.from_user_input(surface.processed_crs),
            always_xy=True,
            force_over=True,
        )
    except Exception as exc:
        raise ConfigurationError(
            f"overlay source {source.source_id} CRS is invalid: {source.source_crs}"
        ) from exc
    metric_box = box(*surface.metric_bounds_at_samples)
    output: list[ModelOverlayFeature] = []
    clipped_count = 0
    dropped_count = 0
    input_length = 0.0
    clipped_length = 0.0
    for feature in parsed:
        try:
            metric = transform(transformer.transform, feature.geometry)
        except Exception as exc:
            raise ConfigurationError(
                f"overlay feature {feature.feature_id} failed CRS transformation"
            ) from exc
        if metric.is_empty or not all(math.isfinite(float(value)) for value in metric.bounds):
            raise ConfigurationError(
                f"overlay feature {feature.feature_id} transformed to empty/non-finite geometry"
            )
        input_length += float(metric.length)
        clipped = metric.intersection(metric_box) if clip_to_model else metric
        if clipped.is_empty:
            dropped_count += 1
            continue
        if not clipped.equals(metric):
            clipped_count += 1
        clipped_length += float(clipped.length)
        model_geometry = surface.metric_to_model(clipped)
        if source.style.simplify_tolerance_mm > 0 and source.kind is not OverlayKind.LABEL:
            model_geometry = model_geometry.simplify(
                source.style.simplify_tolerance_mm, preserve_topology=False
            )
        if source.kind is OverlayKind.LABEL:
            points = [model_geometry] if isinstance(model_geometry, Point) else []
            for part_index, point in enumerate(points):
                output.append(
                    ModelOverlayFeature(
                        feature_id=f"{feature.feature_id}-m{part_index:04d}",
                        geometry=point,
                        properties=feature.properties,
                        label=feature.label,
                    )
                )
            if not points:
                dropped_count += 1
            continue
        lines = _line_parts(model_geometry)
        if not lines:
            dropped_count += 1
            continue
        for part_index, line in enumerate(lines):
            if line.length <= 1e-9 or len(line.coords) < 2:
                dropped_count += 1
                continue
            output.append(
                ModelOverlayFeature(
                    feature_id=f"{feature.feature_id}-m{part_index:04d}",
                    geometry=line,
                    properties=feature.properties,
                )
            )
    return FeatureTransformResult(
        features=tuple(output),
        input_feature_count=len(parsed),
        clipped_feature_count=clipped_count,
        dropped_feature_count=dropped_count,
        input_length_m=input_length,
        clipped_length_m=clipped_length,
    )


def generate_contour_features(
    surface: TerrainSurface,
    source: OverlaySourceConfig,
) -> tuple[tuple[ParsedOverlayFeature, ...], tuple[float, ...]]:
    """Generate deterministic cell-boundary contours from the processed DEM."""
    interval = source.contour_interval_m
    if interval is None:
        raise AssertionError("validated contour interval disappeared")
    valid = ~surface.original_nodata_north
    elevations = surface.elevations_m_north.astype(np.float64)
    minimum = (
        float(np.min(elevations[valid]))
        if source.contour_min_elevation_m is None
        else source.contour_min_elevation_m
    )
    maximum = (
        float(np.max(elevations[valid]))
        if source.contour_max_elevation_m is None
        else source.contour_max_elevation_m
    )
    first = math.ceil(minimum / interval) * interval
    last = math.floor(maximum / interval) * interval
    if first > last:
        raise ConfigurationError(
            f"contour interval {interval:g} m produces no levels between "
            f"{minimum:g} and {maximum:g} m"
        )
    level_count = round((last - first) / interval) + 1
    if level_count > 500:
        raise ConfigurationError(
            f"contour request produces {level_count} levels; increase contour_interval_m"
        )
    levels = tuple(float(first + index * interval) for index in range(level_count))
    pixel = min(abs(surface.transform.a), abs(surface.transform.e))
    metric_sample_box = box(*surface.metric_bounds_at_samples)
    inner = metric_sample_box.buffer(-pixel * 1e-6)
    invalid_parts: list[BaseGeometry] = []
    if bool(np.any(~valid)):
        for geometry_mapping, value in shapes(
            (~valid).astype(np.uint8), mask=~valid, transform=surface.transform
        ):
            if int(value) == 1:
                invalid_parts.append(shape(geometry_mapping))
    invalid_guard = (
        unary_union(invalid_parts).buffer(pixel * 0.51) if invalid_parts else GeometryCollection()
    )
    features: list[ParsedOverlayFeature] = []
    feature_index = 0
    for level in levels:
        high = (elevations >= level) & valid
        if not bool(np.any(high)) or bool(np.all(high[valid])):
            continue
        boundaries: list[BaseGeometry] = []
        for geometry_mapping, value in shapes(
            high.astype(np.uint8), mask=valid, transform=surface.transform
        ):
            if int(value) != 1:
                continue
            boundary = shape(geometry_mapping).boundary.intersection(inner)
            if not invalid_guard.is_empty:
                boundary = boundary.difference(invalid_guard)
            if not boundary.is_empty:
                boundaries.append(boundary)
        for boundary in boundaries:
            for line in _line_parts(boundary):
                if line.length <= pixel * 0.25:
                    continue
                features.append(
                    ParsedOverlayFeature(
                        feature_id=f"contour-{feature_index:06d}",
                        geometry=line,
                        properties={
                            "elevation_m": level,
                            "algorithm": "threshold-cell-boundary",
                        },
                    )
                )
                feature_index += 1
    if not features:
        raise ConfigurationError("generated contour request produced no usable line features")
    return tuple(features), levels


def line_footprint(
    feature: ModelOverlayFeature,
    source: OverlaySourceConfig,
    model_bounds: Polygon,
) -> BaseGeometry:
    """Return a clipped print-width polygon for one line feature."""
    footprint = feature.geometry.buffer(
        source.style.line_width_mm / 2.0,
        cap_style="flat",
        join_style="mitre",
        mitre_limit=2.0,
    )
    return footprint.intersection(model_bounds)


def label_footprint(
    feature: ModelOverlayFeature,
    source: OverlaySourceConfig,
    model_bounds: Polygon,
    minimum_feature_mm: float,
) -> BaseGeometry:
    """Rasterize one label with a locked Pillow bitmap font into printable polygons."""
    if feature.label is None or not isinstance(feature.geometry, Point):
        raise AssertionError("validated label feature lost text or point geometry")
    font = ImageFont.load_default(size=16)
    left, top, right, bottom = font.getbbox(feature.label)
    width_px = max(1, round(right - left))
    height_px = max(1, round(bottom - top))
    bitmap = Image.new("1", (width_px, height_px), color=0)
    draw = ImageDraw.Draw(bitmap)
    draw.text((-left, -top), feature.label, fill=1, font=font)
    pixels = np.asarray(bitmap, dtype=np.uint8)
    pixel_box_mm = max(
        minimum_feature_mm,
        source.style.label_font_height_mm / float(height_px),
    )
    pixel_pitch_mm = pixel_box_mm + minimum_feature_mm * 0.1
    width_mm = (width_px - 1) * pixel_pitch_mm + pixel_box_mm
    height_mm = (height_px - 1) * pixel_pitch_mm + pixel_box_mm
    origin_x = float(feature.geometry.x) - width_mm / 2.0
    origin_y = float(feature.geometry.y) - height_mm / 2.0
    active = [(int(row), int(column)) for row, column in np.argwhere(pixels > 0)]
    if not active:
        raise ConfigurationError(f"label {feature.label!r} rendered to an empty bitmap")
    boxes: list[BaseGeometry] = []
    for row, column in active:
        x0 = origin_x + float(column) * pixel_pitch_mm
        y0 = origin_y + float(height_px - row - 1) * pixel_pitch_mm
        clipped = box(x0, y0, x0 + pixel_box_mm, y0 + pixel_box_mm).intersection(model_bounds)
        if not clipped.is_empty:
            boxes.append(clipped)
    return GeometryCollection(boxes)


def _polygon_parts(geometry: BaseGeometry) -> list[Polygon]:
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    if isinstance(geometry, GeometryCollection):
        polygons: list[Polygon] = []
        for child in geometry.geoms:
            polygons.extend(_polygon_parts(child))
        return polygons
    return []


def _draped_polygon_mesh(
    polygon: Polygon,
    surface: TerrainSurface,
    *,
    raised_height_mm: float,
    embed_depth_mm: float,
) -> tuple[trimesh.Trimesh, float]:
    thickness = raised_height_mm + embed_depth_mm
    planar = trimesh.creation.extrude_polygon(polygon, height=thickness, engine="earcut")
    if not isinstance(planar, trimesh.Trimesh) or len(planar.faces) == 0:
        raise ConfigurationError("overlay footprint extrusion produced no faces")
    vertices = np.asarray(planar.vertices, dtype=np.float64).copy()
    surface_z = surface.surface_z_mm(vertices[:, :2])
    top = vertices[:, 2] > thickness / 2.0
    vertices[top, 2] = surface_z[top] + raised_height_mm
    vertices[~top, 2] = surface_z[~top] - embed_depth_mm
    mesh = trimesh.Trimesh(
        vertices=vertices,
        faces=np.asarray(planar.faces, dtype=np.int64).copy(),
        process=False,
        validate=False,
    )
    if float(mesh.volume) < 0:
        mesh.invert()
    if not bool(mesh.is_watertight) or not bool(mesh.is_winding_consistent):
        raise ConfigurationError("draped overlay footprint is not a closed consistent mesh")
    if float(mesh.volume) <= 0:
        raise ConfigurationError("draped overlay footprint has non-positive volume")
    reopened_surface = surface.surface_z_mm(vertices[:, :2])
    error = float(np.max(np.abs(reopened_surface - surface_z)))
    return mesh, error


def build_layer_mesh(
    surface: TerrainSurface,
    source: OverlaySourceConfig,
    features: tuple[ModelOverlayFeature, ...],
    *,
    minimum_feature_mm: float,
    allow_original_nodata: bool,
) -> tuple[trimesh.Trimesh, float, float, tuple[BaseGeometry, ...]]:
    """Build one independent, watertight overlay object from model-frame features."""
    if source.kind is not OverlayKind.LABEL and source.style.line_width_mm < minimum_feature_mm:
        raise ConfigurationError(
            f"overlay {source.source_id} line_width_mm={source.style.line_width_mm:g} is below "
            f"printer minimum_feature_mm={minimum_feature_mm:g}"
        )
    footprints: list[BaseGeometry] = []
    for feature in features:
        footprint = (
            label_footprint(feature, source, surface.model_bounds, minimum_feature_mm)
            if source.kind is OverlayKind.LABEL
            else line_footprint(feature, source, surface.model_bounds)
        )
        if not footprint.is_empty:
            footprints.append(footprint)
    if not footprints:
        raise ConfigurationError(f"overlay source {source.source_id} has no footprint on model")
    footprint_union = unary_union(footprints)
    if not footprint_union.is_valid:
        footprint_union = footprint_union.buffer(0)
    nodata_overlap = (
        float(footprint_union.intersection(surface.nodata_geometry_mm).area)
        if not surface.nodata_geometry_mm.is_empty
        else 0.0
    )
    if nodata_overlap > 1e-9 and not allow_original_nodata:
        raise ConfigurationError(
            f"overlay {source.source_id} intersects {nodata_overlap:.6f} mm2 of original "
            "NoData; move/clip the source or explicitly set allow_original_nodata=true"
        )
    meshes: list[trimesh.Trimesh] = []
    maximum_error = 0.0
    for polygon in _polygon_parts(footprint_union):
        if polygon.area <= 1e-12:
            continue
        mesh, error = _draped_polygon_mesh(
            polygon,
            surface,
            raised_height_mm=source.style.raised_height_mm,
            embed_depth_mm=source.style.embed_depth_mm,
        )
        meshes.append(mesh)
        maximum_error = max(maximum_error, error)
    if not meshes:
        raise ConfigurationError(f"overlay source {source.source_id} produced no solid geometry")
    combined = trimesh.util.concatenate(meshes)
    combined.units = "mm"
    if not bool(combined.is_watertight) or not bool(combined.is_winding_consistent):
        raise ConfigurationError(f"overlay source {source.source_id} mesh is not watertight")
    return combined, maximum_error, nodata_overlap, tuple(footprints)
