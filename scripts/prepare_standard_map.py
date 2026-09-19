"""Prepare bounded local display tiles from an installed standard-map original."""

from __future__ import annotations

import argparse
from pathlib import Path

from topoforge.web.reference_maps import prepare_standard_map_tiles


def main() -> None:
    """Build the pixel-space pyramid in the chosen local application's state."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    result = prepare_standard_map_tiles(args.state_dir)
    print(f"Prepared {len(result.tiles)} local tiles across {result.max_level + 1} levels")


if __name__ == "__main__":
    main()
