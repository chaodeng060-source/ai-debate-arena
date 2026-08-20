from __future__ import annotations

import importlib.util
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "export", Path(__file__).resolve().parents[1] / "tools" / "export.py"
)
assert SPEC and SPEC.loader
export = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(export)


def _record() -> dict:
    return {
        "schema_version": 2,
        "run_id": "debate-test",
        "status": "completed",
        "topic": "可以以成败论英雄/不能以成败论英雄",
        "pro_side": "可以以成败论英雄",
        "con_side": "不能以成败论英雄",
        "format": "mini",
        "schedule": [
            {"index": index, "stage": stage}
            for index, stage in enumerate(export.MINI_STAGE_ORDER)
        ],
        "chars_per_second": 6.5,
        "rules_digest": "abc123",
        "_source_name": "debate-test.json",
        "_source_sha256": "f" * 64,
        "prep": {
            "status": "complete",
            "board_char_limit": 800,
            "teams": {
                "pro": {
                    "opening_label": "Model A",
                    "rebuttal_label": "Model B",
                    "board": "用可检验的社会影响来比较英雄。",
                    "source_urls": ["https://example.com/a"],
                    "unresolved": ["英雄的范围仍有分歧"],
                }
            },
        },
        "roster": [
            {"name": "正方一辩", "engine": "codex", "model": "gpt-5.5", "effort": "xhigh"},
            {"name": "反方一辩", "engine": "claude", "model": "claude-opus-5", "effort": "max"},
        ],
        "transcript": [
            {
                "speaker": "正方一辩", "side": "pro", "stage": "正方一辩·立论",
                "schedule_index": 0,
                "text": "成功不是全部，但它是社会影响的可见结果。", "chars": 21,
                "limit": 1170, "truncated": False, "elapsed_sec": 1.0,
            },
            {
                "speaker": "反方一辩", "side": "con", "stage": "反方一辩·立论",
                "schedule_index": 1,
                "text": "英雄价值不能被结果倒推。", "chars": 13,
                "limit": 1170, "truncated": False, "elapsed_sec": 1.0,
            },
            {
                "speaker": "正方二辩", "side": "pro", "stage": "正方二辩·驳论",
                "schedule_index": 4,
                "text": "质询之后，正方回应反方。", "chars": 14,
                "limit": 780, "truncated": False, "elapsed_sec": 1.0,
            },
        ],
        "crossfire": [{
            "stage": "交互质询·正方问",
            "schedule_index": 2,
            "exchanges": [{
                "asker": "正方一辩", "answerer": "反方一辩",
                "q": "失败者也都算英雄吗？", "a": "不，仍需勇气与公共价值。",
            }],
        }],
        "jury": {
            "status": "decided", "winner": "con",
            "counts": {"pro": 1, "con": 2, "tie": 0, "uncertain": 0},
            "ballots": [{
                "ballot_id": "ballot-1", "valid": True, "winner": "con",
                "reason": "反方守住了题面。",
                "evidence": [{"speech_id": "S02", "quote": "不能被结果倒推"}],
            }],
        },
    }


def test_markdown_contains_prep_crossfire_jury_and_integrity() -> None:
    md = export.build_md(_record())
    assert "赛前备赛" in md
    assert "交互质询逐字记录" in md
    assert "失败者也都算英雄吗" in md
    assert "形成稳定多数" in md
    assert "6.5 字/秒" in md
    assert "JSON SHA-256" in md
    assert "自动诊断（不是质量分，也不决定胜负）" in md
    assert "成功不是全部，但它是社会影响的可见结果。" in md
    assert "英雄价值不能被结果倒推。" in md
    assert md.index("英雄价值不能被结果倒推") < md.index("交互质询逐字记录")
    assert md.index("交互质询逐字记录") < md.index("质询之后，正方回应反方")


def test_pdf_build_includes_complete_record_sections(tmp_path: Path) -> None:
    out = tmp_path / "debate.pdf"
    assert export.build_pdf(_record(), out) is True
    assert out.is_file()
    assert out.stat().st_size > 1_000
