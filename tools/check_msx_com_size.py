#!/usr/bin/env python3
"""Fail a build when an MSX-DOS COM exceeds its safe transient ceiling."""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("maximum", type=int)
    arguments = parser.parse_args()
    size = arguments.image.stat().st_size
    if size > arguments.maximum:
        parser.error(
            f"{arguments.image} is {size} bytes; safe maximum is "
            f"{arguments.maximum} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
