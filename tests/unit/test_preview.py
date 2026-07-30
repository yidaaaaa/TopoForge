from pathlib import Path

import numpy as np
from PIL import Image

from topoforge.rendering import render_elevation_preview


def test_preview_is_nonempty_png(tmp_path: Path) -> None:
    elevations = np.arange(80, dtype=np.float32).reshape(8, 10)
    output = render_elevation_preview(elevations, tmp_path / "preview.png", width_px=320)
    assert output.stat().st_size > 100
    with Image.open(output) as image:
        assert image.format == "PNG"
        assert image.width == 320
        assert image.height > 72
