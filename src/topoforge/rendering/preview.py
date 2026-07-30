"""Headless shaded-relief preview rendering using only measured elevations."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import numpy.typing as npt
from PIL import Image, ImageDraw


def render_elevation_preview(
    elevations_m: npt.NDArray[np.float32],
    path: Path,
    *,
    title: str = "TopoForge terrain preview",
    width_px: int = 960,
) -> Path:
    """Render a deterministic hillshade/color preview without inventing detail."""
    if elevations_m.ndim != 2 or min(elevations_m.shape) < 2:
        raise ValueError("Preview elevations must be a 2-D grid of at least 2 x 2")
    if not bool(np.all(np.isfinite(elevations_m))):
        raise ValueError("Preview elevations must be finite")
    path.parent.mkdir(parents=True, exist_ok=True)
    values = elevations_m.astype(np.float64)
    low, high = np.percentile(values, (0.5, 99.5))
    span = max(float(high - low), float(np.finfo(np.float64).eps))
    normalized = np.clip((values - low) / span, 0.0, 1.0)

    gradient_y, gradient_x = np.gradient(values)
    slope_x = -gradient_x / max(float(np.std(gradient_x)), 1e-9)
    slope_y = gradient_y / max(float(np.std(gradient_y)), 1e-9)
    normal_z = np.ones_like(values) * 1.8
    norm = np.sqrt(slope_x**2 + slope_y**2 + normal_z**2)
    nx, ny, nz = slope_x / norm, slope_y / norm, normal_z / norm
    light = np.array([-0.45, 0.55, 0.70], dtype=np.float64)
    light /= np.linalg.norm(light)
    shade = np.clip(nx * light[0] + ny * light[1] + nz * light[2], 0.15, 1.0)

    stops = np.array(
        [
            [31, 78, 61],
            [73, 123, 76],
            [154, 145, 88],
            [150, 111, 78],
            [224, 222, 211],
        ],
        dtype=np.float64,
    )
    position = normalized * (len(stops) - 1)
    lower = np.minimum(position.astype(np.int64), len(stops) - 2)
    fraction = (position - lower)[..., None]
    color = stops[lower] * (1.0 - fraction) + stops[lower + 1] * fraction
    rgb = np.clip(color * (0.50 + 0.55 * shade[..., None]), 0, 255).astype(np.uint8)

    terrain = Image.fromarray(rgb)
    target_height = max(1, round(width_px * values.shape[0] / values.shape[1]))
    terrain = terrain.resize((width_px, target_height), Image.Resampling.LANCZOS)
    header_px = 72
    canvas = Image.new("RGB", (width_px, target_height + header_px), (20, 25, 29))
    canvas.paste(terrain, (0, header_px))
    draw = ImageDraw.Draw(canvas)
    draw.text((20, 14), title, fill=(245, 247, 248))
    draw.text(
        (20, 40),
        f"Measured elevation range: {float(np.min(values)):.2f}-{float(np.max(values)):.2f} m",
        fill=(182, 193, 199),
    )
    temporary = path.with_name(f".{path.name}.tmp")
    canvas.save(temporary, format="PNG", optimize=True)
    temporary.replace(path)
    return path
