#!/usr/bin/env python3
"""Resume an interrupted schema-v2 debate from its atomic JSON checkpoint."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arena.room import resume_match


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcript", type=Path)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    asyncio.run(resume_match(args.transcript, timeout=max(30, min(args.timeout, 900))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
