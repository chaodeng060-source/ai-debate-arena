"""主持人只对「归属引用」当众点名（引用核验只认归属引用的 room 一侧）。

verify_opponent_quotes 本身的三条边界（放过自己举例造句、仍抓稻草人、归属不跨句）
在 tests/test_prep.py 的 test_quote_attribution_*；这里只测 _run_schedule 真的按
attributed 筛过一遍再交给主持人。
"""
from __future__ import annotations

import asyncio
import json

from arena import room


def test_host_only_calls_out_attributed_misquotes(monkeypatch, tmp_path):
    """主持人只对「归到对方名下」的引号当众点名，自己举例造句的引号只存档不点名。

    verify_opponent_quotes 标出 attributed 之后，_run_schedule 得据此筛一遍再交给主持人；
    不筛的话这个字段形同虚设，辩手照样被冤枉。
    """
    transcript = []
    crossfire = []
    for index, (stage, side, seat, _seconds) in enumerate(room.MINI_FORMAT[:-1]):
        if seat == -1:
            crossfire.append({
                "stage": stage, "schedule_index": index,
                "exchanges": [{"asker": "提问席", "answerer": "回答席", "q": "问", "a": "答"}],
            })
        else:
            transcript.append({
                "speaker": stage, "side": side, "stage": stage, "schedule_index": index,
                "text": "爱会让人退让。", "elapsed_sec": 1.0, "truncated": False,
            })
    state = {
        "schema_version": 2, "run_id": "quote-attribution", "status": "running",
        "topic": "甲/乙", "pro_side": "甲", "con_side": "乙",
        "format": "mini", "lang": "zh", "crossfire_rounds": 1, "bench_enabled": False,
        "roster": [dict(row) for row in room.ROSTER_MINI],
        "transcript": transcript, "crossfire": crossfire, "jury": None,
    }
    closing = (
        "对方一辩的原话是「爱必然摧毁独立人格」，这站不住。"
        "你做任何决定也会想「这合不合我的价值观」，这不叫不自由。"
    )
    host_calls = []

    async def fake_emit(*_args, **_kwargs):
        return None

    async def fake_host(_topic, _stage, _name, _text, violations):
        host_calls.append(list(violations))
        return ""

    async def fake_jury(*_args, **_kwargs):
        return {"status": "decided", "winner": "pro", "counts": {"pro": 2, "con": 1}, "ballots": []}

    monkeypatch.setattr(room, "_emit_to_room", fake_emit)
    monkeypatch.setattr(room, "_host_check", fake_host)
    monkeypatch.setattr(room, "_run_blind_jury", fake_jury)
    monkeypatch.setattr(room, "_run_cli", lambda *_args, **_kwargs: closing)
    out = tmp_path / "quote-attribution.json"
    asyncio.run(room._run_schedule(state, out, timeout=30, emit_opening=False))

    assert host_calls == [[
        "1 处标注为对方原话的引用未在此前对方发言中找到：「爱必然摧毁独立人格」",
    ]]
    saved = json.loads(out.read_text(encoding="utf-8"))
    checks = saved["transcript"][-1]["quote_checks"]
    assert [row["attributed"] for row in checks] == [True, False], "没归属的照样存档备查"
