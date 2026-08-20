"""评委票 / 插问走外部席位——别处的 AI 也能当评委。
同一条投稿箱协议：request 上 kind=ballot / bench_question，桥按 kind 分发；到点白卷 = 无效票，不崩场。
顺带：对调票默认关、每 5 场抽 1 场开、从不给外部评委（她 21:33 说额度不够了）。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from arena import room as dr
from arena import prep


def test_parse_pool_keeps_owner_for_recusal():
    """之前 parse_pool 把 owner 丢了——API 开的场回避根本比不了。"""
    pool = dr.parse_pool([
        {"engine": "external", "model": "aisay:u1", "effort": "-", "label": "小A", "owner": "h1"},
        {"engine": "external", "model": "aisay:u2", "effort": "-", "label": "小B"},
        "fable-5:xhigh", "gpt-5.6-sol:xhigh",
    ])
    assert pool[0]["owner"] == "h1" and "owner" not in pool[1] and "owner" not in pool[2]


def test_parse_judge_pool_any_count_same_validation():
    jp = dr.parse_judge_pool([{"engine": "external", "model": "aisay:j1", "effort": "-", "label": "评委J1", "owner": "h9"}])
    assert len(jp) == 1 and jp[0]["owner"] == "h9"
    with pytest.raises(ValueError):
        dr.parse_judge_pool([])
    with pytest.raises(ValueError):
        dr.parse_judge_pool([{"engine": "nope", "model": "x", "effort": "-"}])
    with pytest.raises(ValueError):
        dr.parse_judge_pool("fable-5:xhigh")


def test_external_judge_ballot_goes_through_inbox_with_kind(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "INBOX_ROOT", tmp_path)
    monkeypatch.setattr(dr, "JUDGE_TIMEOUT_MIN", 1)
    judge = {"engine": "external", "model": "aisay:j1", "owner": "h9", "name": "评委甲", "label": "J1",
             "effort": "-", "run_id": "run-j"}
    captured = {}

    def bridge():
        # 等 request 落盘，读 kind，回一张票
        for _ in range(100):
            req, rep = prep.external_paths(tmp_path, "run-j", 1, "评委甲")
            if req.exists():
                data = json.loads(req.read_text("utf-8"))
                captured["kind"] = data["kind"]
                captured["system"] = data["system"]
                rep.write_text('{"winner":"pro"}', encoding="utf-8")
                return
            time.sleep(0.05)
    t = threading.Thread(target=bridge); t.start()
    raw, err = asyncio.run(dr._ask_judge(judge, "请出票", timeout=5, max_tokens=100))
    t.join()
    assert raw == '{"winner":"pro"}' and err == ""
    assert captured["kind"] == "ballot" and captured["system"] == dr.JUDGE_SYSTEM
    # 插问用 bench_question
    judge2 = dict(judge, name="评委乙")
    captured.clear()

    def bridge2():
        for _ in range(100):
            req, rep = prep.external_paths(tmp_path, "run-j", 2, "评委乙")
            if req.exists():
                captured["kind"] = json.loads(req.read_text("utf-8"))["kind"]
                rep.write_text("你方如何回应？", encoding="utf-8")
                return
            time.sleep(0.05)
    t = threading.Thread(target=bridge2); t.start()
    raw, _ = asyncio.run(dr._ask_judge(judge2, "提一个问题", timeout=5, max_tokens=50, kind="bench_question"))
    t.join()
    assert raw == "你方如何回应？" and captured["kind"] == "bench_question"
    # 没人接：到点白卷，不抛
    judge3 = dict(judge, name="评委丙")
    raw, err = asyncio.run(dr._ask_judge(judge3, "请出票", timeout=1, max_tokens=100))
    assert raw == "" and err == ""


def test_deepseek_judge_engine_does_not_hijack_external(monkeypatch, tmp_path):
    """DEBATE_JUDGE_ENGINE=deepseek 只管本机席位；外部评委照走投稿箱。"""
    monkeypatch.setattr(dr, "JUDGE_ENGINE", "deepseek")
    monkeypatch.setattr(dr, "INBOX_ROOT", tmp_path)
    monkeypatch.setattr(dr, "JUDGE_TIMEOUT_MIN", 1)

    async def boom(*a, **k):
        raise AssertionError("external judge must not hit deepseek")
    monkeypatch.setattr(dr, "_deepseek", boom)
    judge = {"engine": "external", "model": "aisay:j1", "name": "评委甲", "effort": "-", "run_id": "run-d"}
    raw, err = asyncio.run(dr._ask_judge(judge, "请出票", timeout=1, max_tokens=10))
    assert raw == "" and err == ""   # 白卷而不是炸


def test_position_recheck_default_samples_one_in_five(monkeypatch):
    monkeypatch.delenv("DEBATE_POSITION_RECHECK", raising=False)
    monkeypatch.delenv("DEBATE_POSITION_RECHECK_EVERY", raising=False)
    dr._RECHECK_COUNTER["n"] = 0
    flags = [dr._position_recheck_enabled() for _ in range(10)]
    assert flags == [False, False, False, False, True, False, False, False, False, True]
    monkeypatch.setenv("DEBATE_POSITION_RECHECK", "on")
    assert dr._position_recheck_enabled() is True
    monkeypatch.setenv("DEBATE_POSITION_RECHECK", "off")
    assert dr._position_recheck_enabled() is False
    monkeypatch.setenv("DEBATE_POSITION_RECHECK", "sample")
    monkeypatch.setenv("DEBATE_POSITION_RECHECK_EVERY", "2")
    dr._RECHECK_COUNTER["n"] = 0
    assert [dr._position_recheck_enabled() for _ in range(4)] == [False, True, False, True]


def test_blind_jury_never_sends_recheck_to_external_judges(monkeypatch):
    """对调票开着时，外部评委也只收一张原序票；本机席位两张。"""
    monkeypatch.setattr(dr, "_position_recheck_enabled", lambda: True)
    monkeypatch.setattr(dr, "_precedent_verdict_text", lambda topic: "")
    asked: list[tuple[str, bool]] = []

    async def fake_ask(judge, prompt, *, timeout, max_tokens, kind="ballot"):
        asked.append((judge["name"], "对调" in prompt or "swap" in prompt.lower()))
        return "", ""
    monkeypatch.setattr(dr, "_ask_judge", fake_ask)
    monkeypatch.setattr(dr, "blind_transcript", lambda *a, **k: ("", {"pro": "A", "con": "B"}))
    monkeypatch.setattr(dr, "build_ballot_prompt", lambda **k: "票")
    monkeypatch.setattr(dr, "parse_ballot", lambda raw, **k: {"valid": False, "ballot_id": k.get("ballot_id")})
    monkeypatch.setattr(dr, "aggregate_ballots", lambda ballots: {"ballots": ballots, "winner": None})
    panel = [
        {"engine": "external", "model": "aisay:j1", "name": "评委甲", "label": "J1", "run_id": "r"},
        {"engine": "claude", "model": "claude-opus-5", "name": "评委乙", "label": "Opus", "effort": "high"},
        {"engine": "external", "model": "aisay:j2", "name": "评委丙", "label": "J2", "run_id": "r"},
    ]
    res = asyncio.run(dr._run_blind_jury("t", [], [], stage_order=(), panel=panel, roster=[], timeout=5))
    names = [n for n, _ in asked]
    assert names.count("评委甲") == 1 and names.count("评委丙") == 1 and names.count("评委乙") == 2
    assert res["position_recheck_enabled"] is True


def test_run_schedule_draws_panel_from_judge_pool_with_run_id(tmp_path, monkeypatch):
    """开赛请求带 judge_pool → 赛录 state["judge_pool"] → _run_schedule 按回避抽席、每席挂 run_id。"""
    import asyncio as _a
    seen = {}

    async def fake_schedule(state, out, *, timeout, emit_opening):
        # 模拟 _run_schedule 里抽席那几行的行为，直接调真函数验证
        panel = state.get("panel") or dr._draw_panel(
            seed=state.get("draw_seed"), candidates=state.get("judge_pool") or None, roster=state["roster"])
        for j in panel:
            j["run_id"] = state["run_id"]
        seen["panel"] = panel
        seen["state"] = state

    async def no_emit(*a, **k):
        return None
    monkeypatch.setattr(dr, "TRANSCRIPT_DIR", tmp_path)
    monkeypatch.setattr(dr, "_run_schedule", fake_schedule)
    monkeypatch.setattr(dr, "_emit_to_room", no_emit)
    monkeypatch.setattr(dr, "_fact_base_for", lambda topic: "")
    pool = dr.parse_pool([
        {"engine": "external", "model": "aisay:u1", "effort": "-", "label": "小A", "owner": "hA"},
        {"engine": "external", "model": "aisay:u2", "effort": "-", "label": "小B", "owner": "hB"},
        {"engine": "external", "model": "aisay:u3", "effort": "-", "label": "小C", "owner": "hC"},
        {"engine": "external", "model": "aisay:u4", "effort": "-", "label": "小D", "owner": "hD"},
    ])
    judge_pool = dr.parse_judge_pool([
        {"engine": "external", "model": "aisay:jA", "effort": "-", "label": "A家评委", "owner": "hA"},  # 回避
        {"engine": "external", "model": "aisay:jX", "effort": "-", "label": "X", "owner": "hX"},
        {"engine": "external", "model": "aisay:jY", "effort": "-", "label": "Y", "owner": "hY"},
    ])
    _a.run(dr._run_match("t", "p", "c", "mini", "zh", timeout=5, draw=True, prep_enabled=False,
                         bench_enabled=False, pool=pool, seed=3, judge_pool=judge_pool))
    state, panel = seen["state"], seen["panel"]
    assert len(state["judge_pool"]) == 3
    owners = [j.get("owner") for j in panel]
    assert "hA" not in owners and {"hX", "hY"} <= set(owners)
    assert sum(1 for j in panel if j.get("engine") != "external") == 1      # 一席本机补位
    assert all(j["run_id"] == state["run_id"] for j in panel)
    assert sorted(j["name"] for j in panel) == ["评委丙", "评委乙", "评委甲"]
