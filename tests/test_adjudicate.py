from __future__ import annotations

import importlib.util
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "adjudicate",
    Path(__file__).resolve().parents[1] / "tools" / "adjudicate.py",
)
assert SPEC and SPEC.loader
adjudicate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adjudicate)


def test_build_adjudicated_record_is_derived_and_checks_quotes() -> None:
    source = {
        "topic": "成败能否论英雄",
        "verdict": "旧单评委意见",
        "transcript": [
            {"side": "pro", "text": "成败不是唯一尺度。"},
            {"side": "con", "text": "对方说「成败就是唯一尺度」，但这不是原话。"},
        ],
    }
    jury = {"status": "decided", "winner": "con", "counts": {"con": 2}}
    out = adjudicate.build_adjudicated_record(
        source,
        source_name="source.json",
        source_sha256="a" * 64,
        jury=jury,
    )
    assert "jury" not in source
    assert out["legacy_verdict"] == "旧单评委意见"
    assert out["jury"] == jury
    assert out["upstream_source"]["sha256"] == "a" * 64
    # attributed 标明这处引号有没有把话归给对方（「对方说『X』」）。
    # 只有归属引用才会让主持人当众点名；自己举例造句的引号仍记录但不指控。
    assert out["transcript"][1]["quote_checks"] == [
        {
            "quote": "成败就是唯一尺度", "status": "not_exactly_found",
            "attributed": True, "attribution": "对方",
        }
    ]
