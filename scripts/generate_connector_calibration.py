"""Generate the compact P2S connector-clearance calibration bundle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from topoforge.tiling.calibration import generate_connector_calibration


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    result = generate_connector_calibration(arguments.output)
    print(
        json.dumps(
            {
                "status": "succeeded",
                "output_dir": str(result.output_dir),
                "core_3mf": str(result.core_3mf_path),
                "dimensions_mm": result.inspection.dimensions_mm,
                "object_count": result.inspection.object_count,
                "triangle_count": result.inspection.triangle_count,
                "strict_warning_count": result.inspection.strict_warning_count,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
