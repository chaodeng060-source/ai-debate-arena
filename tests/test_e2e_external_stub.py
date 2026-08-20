"""端到端冒烟：一场 mini 赛从开赛走到裁决，**全部席位都是外部 AI**（辩手 4 + 评委 3），
稿子由 tools/bridge.py 的 stub 桥代填——零额度，不起任何真模型。

验的是引擎流程没被今晚的刀（外部席位 / 多场并发 / 观众席 / 评委外部席 / kind 标注）弄坏：
备赛→发言→质询→插问→评委票→观众关票→赛录落盘→榜能算。**不验辩论质量**——stub 的胜负分数没有评审含义。
明天服务重启后还要对生产端点再跑一遍真的（curl），这条是预演，不替代。
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arena import room as dr  # noqa: E402
from tools import bridge as bridge  # noqa: E402
from tools import board as board  # noqa: E402


def _ext(label: str, owner: str) -> dict:
    """外部席位：model 填 aisay 上那个 AI 的标识，owner 填它主人（评委回避按 owner 比）。"""
    return {"engine": "external", "model": f"aisay:{label}", "effort": "-", "label": label, "owner": owner}


def test_full_external_mini_match_completes_with_stub_bridge(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    emits: list[str] = []

    async def no_emit(body, *, title, **k):
        emits.append(title)
        return "x"

    monkeypatch.setattr(dr, "TRANSCRIPT_DIR", tmp_path)
    monkeypatch.setattr(dr, "INBOX_ROOT", inbox)
    monkeypatch.setattr(dr, "_emit_to_room", no_emit)
    monkeypatch.setattr(dr, "_fact_base_for", lambda topic: "")
    monkeypatch.setenv("DEBATE_POSITION_RECHECK", "off")
    dr._RUNS.clear()

    # 桥在后台线程盯投稿箱；比赛打完后它连续 20s 没新 request 自己退
    t = threading.Thread(target=bridge.run, args=(inbox, None, bridge.stub_handler),
                         kwargs={"poll": 0.5, "idle_exit": 20}, daemon=True)
    t.start()

    pool = [_ext("城里甲", "aisay:u1"), _ext("城里乙", "aisay:u2"), _ext("城里丙", "aisay:u3"), _ext("城里丁", "aisay:u4")]
    judges = [_ext("城里评委一", "aisay:j1"), _ext("城里评委二", "aisay:j2"), _ext("城里评委三", "aisay:j3")]

    asyncio.run(dr._run_match(
        "时间赋予生命意义/生命赋予时间意义", "时间赋予生命意义", "生命赋予时间意义", "mini", "zh",
        timeout=40, draw=False, crossfire_rounds=1, prep_enabled=True, bench_enabled=True,
        pool=pool, judge_pool=judges, seed=7,
    ))

    outs = [p for p in tmp_path.glob("debate-*.json")]
    assert len(outs) == 1, outs
    state = json.loads(outs[0].read_text(encoding="utf-8"))
    run_id = state["run_id"]

    # 席位全是外部的、都挂着本场 run_id
    assert all(d["engine"] == "external" and d["run_id"] == run_id for d in state["roster"])
    # 发言全部由桥回了稿（没有白卷）
    assert state["transcript"], "转录不能为空"
    assert all((row.get("text") or "").strip() for row in state["transcript"]), \
        [row.get("stage") for row in state["transcript"] if not (row.get("text") or "").strip()]
    # 质询、插问都走到了
    assert state["crossfire"], "质询实录不能为空"
    assert state.get("bench"), "插问实录不能为空（评委插问走外部席）"
    assert all((row.get("answer") or "").strip() for row in state["bench"])
    # 评委席三位都是外部评委（有池、够三席、不需要补位），三张原序票有效、判出胜负
    jury = state["jury"]
    assert jury["status"] not in {"judge_failed", "position_unstable"}, jury
    assert jury["winner"] in {"pro", "con"}, jury
    assert jury["primary_ballots"] == 3, jury
    panel = state.get("panel") or []
    assert len(panel) == 3 and {j.get("engine") for j in panel} == {"external"}, panel
    # 赛录终态 + 观众席关票汇总挂上
    assert state["status"] == "completed" and state["phase"] == "done"
    assert "audience" in state
    # 投稿箱里每一条 request 都有对应 reply，kind 覆盖到今晚标注的各种体裁
    reqs = sorted((inbox / run_id).glob("*.request.json"))
    assert reqs
    kinds = {json.loads(p.read_text("utf-8"))["kind"] for p in reqs}
    assert {"speech", "crossfire_q", "crossfire_a", "bench_answer", "prep", "ballot", "bench_question"} <= kinds, kinds
    missing = [p.name for p in reqs if not p.with_name(p.name[: -len(".request.json")] + ".reply.txt").exists()]
    assert not missing, missing
    # 榜能从这份赛录算出来：四位外部辩手都计参赛，胜方两位计胜
    records, skipped = board.load_records(tmp_path)
    assert len(records) == 1 and skipped == 0
    tally = board.tally(records)
    assert tally["matches"] == 1 and tally["decided"] == 1
    table = {r["key"]: r for r in tally["table"]}
    assert set(table) == {"aisay:城里甲", "aisay:城里乙", "aisay:城里丙", "aisay:城里丁"}, table
    assert all(r["played"] == 1 for r in table.values())
    assert sum(r["won"] for r in table.values()) == 2, table
    assert sum(r["mvp"] for r in table.values()) == 1, table
    assert board.to_markdown(tally)
    # 推流走到了评审团和观众席
    assert any("评审团" in x for x in emits) and any("观众席" in x for x in emits)
