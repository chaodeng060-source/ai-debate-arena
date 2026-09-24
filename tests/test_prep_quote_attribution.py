"""41cb9b0 移植（引用核验只认「归属引用」）的三条边界测试。

tests/test_prep.py:386/404 两条既有断言已经在原地补上 attributed/attribution 两个 key；
这里补主项目那次一起加的三个场景（放过自己举例造句、仍抓稻草人、归属不跨句边界），
落在新文件里，不再碰 test_prep.py 其它内容。
"""
from __future__ import annotations

import asyncio
import json

from arena import prep, room


def test_quote_attribution_spares_self_authored_examples():
    """辩手自己举例造句用的引号，不该被当成「捏造对方原话」。"""
    transcript = [{"speaker": "反方一辩", "side": "con", "text": "爱会让人退让。"}]
    findings = prep.verify_opponent_quotes(
        "你做任何决定也会想「这合不合我的价值观」，这不叫不自由。",
        side="pro",
        transcript=transcript,
    )
    assert len(findings) == 1
    assert findings[0]["attributed"] is False, "自己举例造句不该被判为引用对方"


def test_quote_attribution_still_catches_strawman():
    """把没说过的话按到对方头上——这个必须继续抓，一个都不能放。

    放走稻草人比误报更严重：辩手可以捏造一个好打的版本再打，评委看不出来。
    归属词一出现就是「我引的是你的话」，就得对得上。
    """
    transcript = [{"speaker": "反方一辩", "side": "con", "text": "爱会让人退让。"}]
    for prefix, marker in (
        ("对方一辩的原话是", "对方"),
        ("你方声称", "你方"),
        ("他刚才说", "刚才"),
        ("对方承认", "对方"),
    ):
        findings = prep.verify_opponent_quotes(
            f"{prefix}「爱必然摧毁独立人格」，这站不住。",
            side="pro",
            transcript=transcript,
        )
        assert len(findings) == 1, f"{prefix} 应当被抓住"
        assert findings[0]["attributed"] is True, f"{prefix} 属于归属引用"
        assert findings[0]["attribution"] == marker


def test_quote_attribution_does_not_reach_across_sentences():
    """上一句的「对方」不能粘到下一句自己的举例上。

    归属窗口若无限长，一段话里只要出现过一次「对方」，后面所有引号都会被判成
    引用对方——那等于没修。
    """
    transcript = [{"speaker": "反方一辩", "side": "con", "text": "爱会让人退让。"}]
    text = (
        "对方把自由说窄了。真正的自由是你早上醒来想着"
        "「今天要不要给他打个电话」，这种牵挂不是枷锁。"
    )
    findings = prep.verify_opponent_quotes(
        text, side="pro", transcript=transcript,
    )
    assert len(findings) == 1
    assert findings[0]["attributed"] is False, "隔了一句的「对方」不该粘过来"


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
