#!/usr/bin/env python3
"""Export and reopen per-tile Bambu Studio project 3MF evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from topoforge.validation.bambu_projects import (
    generate_bambu_project_evidence,
    verify_bambu_project_evidence,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--print-set", type=Path, required=True)
    value.add_argument("--slice-set", type=Path, required=True)
    value.add_argument("--bambu-studio", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--timeout", type=float, default=1800.0)
    value.add_argument("--verify-only", action="store_true")
    return value


def main() -> None:
    args = parser().parse_args()
    if args.verify_only:
        result = verify_bambu_project_evidence(
            args.output,
            print_set_dir=args.print_set,
            slice_set_dir=args.slice_set,
            bambu_studio=args.bambu_studio,
        )
    else:
        result = generate_bambu_project_evidence(
            args.print_set,
            args.slice_set,
            args.bambu_studio,
            args.output,
            timeout_seconds=args.timeout,
        ).model_dump(mode="json")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
