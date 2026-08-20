#!/usr/bin/env python3
"""Attach a blinded three-ballot jury receipt to an existing debate record.

This is intentionally separate from the live runner.  It lets an already
completed production match be re-adjudicated without replaying or mutating the
original transcript.  The source JSON remains untouched and is identified by
name plus SHA-256 in the derived record.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arena.prep import ordered_debate_events, verify_opponent_quotes
from arena.room import FULL_FORMAT, MINI_FORMAT, _run_blind_jury


def stage_order_for(source: dict) -> tuple[str, ...]:
    schedule = source.get("schedule") or []
    if schedule:
        return tuple(
            str(row.get("stage") or "")
            for row in sorted(schedule, key=lambda row: int(row.get("index", 0)))
        )
    fmt = MINI_FORMAT if source.get("format") == "mini" else FULL_FORMAT
    return tuple(stage for stage, _side, _seat, _seconds in fmt)


def build_adjudicated_record(
    source: dict,
    *,
    source_name: str,
    source_sha256: str,
    jury: dict,
    stage_order: tuple[str, ...] = (),
) -> dict:
    record = copy.deepcopy(source)
    transcript = record.get("transcript") or []
    prior_speeches: list[dict] = []
    prior_crossfire: list[dict] = []
    for event in ordered_debate_events(
        transcript, record.get("crossfire") or [], stage_order=stage_order,
    ):
        if event["kind"] == "crossfire":
            prior_crossfire.append(event["row"])
            continue
        speech = event["row"]
        speech["quote_checks"] = verify_opponent_quotes(
            str(speech.get("text") or ""),
            side=str(speech.get("side") or ""),
            transcript=prior_speeches,
            crossfire=prior_crossfire,
        )
        prior_speeches.append(speech)
    if record.get("verdict") and not record.get("legacy_verdict"):
        record["legacy_verdict"] = record["verdict"]
    record["schema_version"] = 2
    record["run_id"] = record.get("run_id") or f"legacy-{Path(source_name).stem}"
    record["status"] = "completed" if jury.get("status") != "judge_failed" else "judge_failed"
    record["jury"] = jury
    record["adjudicated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    record["upstream_source"] = {"name": source_name, "sha256": source_sha256}
    record.setdefault("chars_per_second", 6.5)
    return record


async def _main(args: argparse.Namespace) -> int:
    source_path = Path(args.json_path)
    raw = source_path.read_bytes()
    source = json.loads(raw)
    transcript = source.get("transcript") or []
    if len(transcript) < 2:
        raise ValueError("at least two completed speeches are required")
    if source.get("jury") and not args.force:
        raise ValueError("record already has jury; pass --force to replace it")

    stage_order = stage_order_for(source)
    jury = await _run_blind_jury(
        str(source.get("topic") or ""),
        transcript,
        source.get("crossfire") or [],
        stage_order=stage_order,
        roster=source.get("roster") or [],
    )
    output = Path(args.output) if args.output else source_path.with_name(source_path.stem + "-jury.json")
    record = build_adjudicated_record(
        source,
        source_name=source_path.name,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        jury=jury,
        stage_order=stage_order,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(output)
    print(json.dumps({
        "output": str(output),
        "jury_status": jury.get("status"),
        "winner": jury.get("winner"),
        "counts": jury.get("counts"),
    }, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("json_path")
    parser.add_argument("--output", default="")
    parser.add_argument("--force", action="store_true")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
