"""观赛只读接口：GET /api/debate/{run_id}/record（整场回看）与
GET /api/debate/{run_id}/events?since=（增量轮询）。

三件事分开测：run_id 校验挡目录穿越、投影不泄内部字段（评委模型身份/对调票/
owner/raw/服务器路径）、事件流的 seq 语义（只增不减、since 过滤、next_seq）。
"""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena import room


# ── run_id 校验（直接测纯函数，不受 HTTP 客户端的 URL 归一化影响）──────────────

def test_safe_run_id_accepts_engine_format_rejects_traversal():
    assert room._safe_run_id("debate-20260924-175200-abcd1234") == "debate-20260924-175200-abcd1234"
    for bad in ("", "  ", "..", ".", "../etc/passwd", "a/b", "/etc/passwd",
                "a" * 200, "de\x00bate", "de bate"):
        assert room._safe_run_id(bad) is None, bad


def test_record_path_stays_inside_transcript_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)
    p = room._record_path("debate-x")
    assert p == (tmp_path / "debate-x.json").resolve()
    assert room._record_path("../x") is None
    assert room._record_path("..") is None


# ── 测试数据：手工造一份「完整字段都在」的赛录，故意埋几个不该泄漏的标记串 ──────

ROSTER = [
    {"name": "正方一辩", "side": "pro", "seat": 1, "label": "外部甲", "engine": "external",
     "model": "aisay:甲", "effort": "-", "owner": "OWNER_LEAK_1", "fact_base": "",
     "strategy_board": "STRATEGY_LEAK"},
    {"name": "反方一辩", "side": "con", "seat": 1, "label": "外部乙", "engine": "external",
     "model": "aisay:乙", "effort": "-", "owner": "OWNER_LEAK_2"},
]

SCHEDULE = [
    {"index": 0, "stage": "正方一辩·立论", "side": "pro", "seat": 1, "seconds": 180},
    {"index": 1, "stage": "反方一辩·立论", "side": "con", "seat": 1, "seconds": 180},
    {"index": 2, "stage": "交互质询·正方问", "side": "pro", "seat": -1, "seconds": 0},
]

TRANSCRIPT = [
    {"speaker": "正方一辩", "side": "pro", "stage": "正方一辩·立论", "text": "开场立论正文",
     "schedule_index": 0, "chars": 6, "limit": 1170, "truncated": False, "elapsed_sec": 12.3,
     "defection_hits": [], "msg_id": "debate_aaaaaaaaaaaa"},
    {"speaker": "反方一辩", "side": "con", "stage": "反方一辩·立论", "text": "反方立论正文被掐断",
     "schedule_index": 1, "chars": 1200, "limit": 1170, "truncated": True, "elapsed_sec": 30.0,
     "defection_hits": ["双方都有道理"], "msg_id": "debate_bbbbbbbbbbbb"},
]

CROSSFIRE = [
    {"stage": "交互质询·正方问", "schedule_index": 2, "exchanges": [
        {"q": "问题一", "a": "回答一", "asker": "正方一辩", "answerer": "反方一辩", "unanswered": False},
        {"q": "（未作答）", "a": "（未作答）", "asker": "正方一辩", "answerer": "反方一辩", "unanswered": True},
    ]},
]

BENCH = [
    {"judge": "评委甲", "judge_label": "Claude Opus 5", "target": "pro",
     "question": "插问内容", "answerer": "正方一辩", "answer": "作答内容"},
]

JURY = {
    "status": "decided", "winner": "pro", "counts": {"pro": 2, "con": 1, "tie": 0, "uncertain": 0},
    "position_checked": True, "position_unstable": False,
    "position_checked_judges": ["评委甲"], "position_unstable_judges": [],
    "position_recheck_enabled": True,
    "score_totals": {"pro": 85.5, "con": 80.0},
    "mvp": {"speaker": "正方一辩", "votes": 2, "of": 3,
            "tally": {"正方一辩": 2, "反方一辩": 1}, "model": "aisay:甲", "side": "pro"},
    "panel": [{"name": "评委甲", "label": "Claude Opus 5"}, {"name": "评委乙", "label": "GPT-5.5"},
              {"name": "评委丙", "label": "Gemini"}],
    "ballots": [
        {"judge": "评委甲", "judge_label": "Claude Opus 5", "role": "primary", "valid": True,
         "winner": "pro", "presented_winner": "A", "margin": "clear", "reason": "PRIMARY_理由甲",
         "uncertainty": "不确定甲", "evidence": [{"speech_id": "S01", "quote": "引用甲"}],
         "scores": {"pro": {"rubric": [8, 8, 8, 8], "rubric_total": 56.0, "discretion": 20, "total": 76.0},
                    "con": {"rubric": [6, 6, 6, 6], "rubric_total": 42.0, "discretion": 15, "total": 57.0}},
         "score_vote_consistent": True, "mvp": {"pid": "P01", "speaker": "正方一辩"},
         "presentation": {"pro": "A", "con": "B"}, "raw": "RAW_LEAK_甲"},
        {"judge": "评委乙", "judge_label": "GPT-5.5", "role": "primary", "valid": True,
         "winner": "pro", "presented_winner": "A", "margin": "narrow", "reason": "PRIMARY_理由乙",
         "uncertainty": "", "evidence": [{"speech_id": "S02", "quote": "引用乙"}],
         "scores": None, "score_vote_consistent": None, "mvp": None,
         "presentation": {"pro": "A", "con": "B"}, "raw": "RAW_LEAK_乙"},
        {"judge": "评委丙", "judge_label": "Gemini", "role": "primary", "valid": False,
         "error": "unparseable", "raw": "RAW_LEAK_丙"},
        {"judge": "评委甲", "judge_label": "Claude Opus 5", "role": "recheck", "valid": True,
         "winner": "con", "presented_winner": "B", "margin": "narrow", "reason": "RECHECK_MARK_LEAK",
         "uncertainty": "", "evidence": [{"speech_id": "S01", "quote": "引用甲"}],
         "scores": None, "score_vote_consistent": None, "mvp": None,
         "presentation": {"pro": "B", "con": "A"}, "raw": "RAW_LEAK_recheck"},
    ],
}


def _write_state(tmp_path, run_id="debate-test1", **overrides):
    state = {
        "schema_version": 2, "run_id": run_id, "status": "completed", "phase": "done",
        "topic": "T/T'", "pro_side": "T", "con_side": "T'", "format": "mini", "lang": "zh",
        "crossfire_rounds": 2, "bench_enabled": True,
        "draw_note": "抽签结果：正方 外部甲　｜　反方 外部乙",
        "started_at": "2026-09-24T10:00:00+0800", "finished_at": "2026-09-24T10:20:00+0800",
        "schedule": SCHEDULE, "roster": ROSTER, "transcript": TRANSCRIPT, "crossfire": CROSSFIRE,
        "bench": BENCH, "jury": JURY, "panel": JURY["panel"],
        "resume_receipts": [{"pid": 12345, "claimed_at": "x"}],
        "error": "SHOULD_NOT_LEAK_error_text",
    }
    state.update(overrides)
    (tmp_path / f"{run_id}.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return state


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)
    app = FastAPI()
    app.include_router(room.router)
    return TestClient(app)


# ── /record：整场回看 ──────────────────────────────────────────────────

def test_record_projects_public_fields_and_hides_internal_ones(tmp_path, monkeypatch):
    _write_state(tmp_path)
    c = _client(tmp_path, monkeypatch)
    resp = c.get("/api/debate/debate-test1/record")
    assert resp.status_code == 200
    body = resp.json()

    assert body["run_id"] == "debate-test1"
    assert body["status"] == "completed" and body["phase"] == "done" and body["done"] is True
    assert body["topic"] == "T/T'" and body["draw_note"].startswith("抽签结果")
    assert body["roster"] == [
        {"name": "正方一辩", "side": "pro", "seat": 1, "label": "外部甲", "engine": "external",
         "model": "aisay:甲", "effort": "-"},
        {"name": "反方一辩", "side": "con", "seat": 1, "label": "外部乙", "engine": "external",
         "model": "aisay:乙", "effort": "-"},
    ], body["roster"]

    raw = json.dumps(body, ensure_ascii=False)
    # 内部字段/敏感标记串一律不许出现在响应里
    for leaked in ("OWNER_LEAK", "STRATEGY_LEAK", "SHOULD_NOT_LEAK_error_text",
                   "RAW_LEAK", "RECHECK_MARK_LEAK", "judge_label", "resume_receipts",
                   "Claude Opus 5", "GPT-5.5", "Gemini",   # 评委的模型身份——评委盲审
                   str(tmp_path)):                          # 服务器绝对路径
        assert leaked not in raw, f"不该出现在响应里：{leaked}"


def test_record_events_are_ordered_speech_crossfire_bench_jury(tmp_path, monkeypatch):
    _write_state(tmp_path)
    c = _client(tmp_path, monkeypatch)
    body = c.get("/api/debate/debate-test1/record").json()
    events = body["events"]
    assert [e["type"] for e in events] == ["speech", "speech", "crossfire", "bench", "jury"]
    assert [e["seq"] for e in events] == [0, 1, 2, 3, 4]
    assert body["next_seq"] == 4

    speech1, speech2, crossfire, bench, jury = events
    assert speech1["speaker"] == "正方一辩" and speech1["truncated"] is False
    assert speech2["truncated"] is True and speech2["defection_hits"] == ["双方都有道理"]
    assert crossfire["exchanges"][1]["unanswered"] is True
    assert bench["judge"] == "评委甲" and "judge_label" not in bench

    # 评委票：对调票（role=recheck）被过滤掉，只剩 3 张原序票；无效票只留 judge/valid/error
    assert len(jury["ballots"]) == 3
    winners = [b.get("winner") for b in jury["ballots"] if b["valid"]]
    assert winners == ["pro", "pro"]
    invalid = next(b for b in jury["ballots"] if not b["valid"])
    assert set(invalid) == {"judge", "valid", "error"}
    assert invalid["error"] == "unparseable"
    # panel 只留 name，不带 label
    assert jury["panel"] == [{"name": "评委甲"}, {"name": "评委乙"}, {"name": "评委丙"}]
    # jury 顶层 mvp 是辩手不是评委，模型信息照留
    assert jury["mvp"]["model"] == "aisay:甲"


# ── /events：增量轮询 ──────────────────────────────────────────────────

def test_events_since_filters_and_stabilizes_seq(tmp_path, monkeypatch):
    _write_state(tmp_path)
    c = _client(tmp_path, monkeypatch)

    everything = c.get("/api/debate/debate-test1/events").json()  # 不给 since = 全部
    assert len(everything["events"]) == 5 and everything["next_seq"] == 4

    caught_up = c.get("/api/debate/debate-test1/events?since=4").json()
    assert caught_up["events"] == [] and caught_up["next_seq"] == 4 and caught_up["done"] is True

    partial = c.get("/api/debate/debate-test1/events?since=1").json()
    assert [e["type"] for e in partial["events"]] == ["crossfire", "bench", "jury"]
    assert partial["next_seq"] == 4

    bad = c.get("/api/debate/debate-test1/events?since=not-a-number")
    assert bad.status_code == 400


def test_events_done_false_while_match_still_running(tmp_path, monkeypatch):
    _write_state(tmp_path, status="running", phase="match", jury=None, bench=[],
                 finished_at=None)
    c = _client(tmp_path, monkeypatch)
    body = c.get("/api/debate/debate-test1/record").json()
    assert body["done"] is False
    # 还没到评审阶段：jury/bench 都还没有事件
    assert [e["type"] for e in body["events"]] == ["speech", "speech", "crossfire"]


# ── 错误路径：坏 run_id / 没有这场 ──────────────────────────────────────

def test_unknown_or_malformed_run_id_rejected(tmp_path, monkeypatch):
    _write_state(tmp_path)
    c = _client(tmp_path, monkeypatch)

    missing = c.get("/api/debate/debate-does-not-exist/record")
    assert missing.status_code == 404 and missing.json() == {"error": "no such match"}

    # 带空格：URL 编码后仍是单个 path 段，真的会走到我们的 handler，校验会拒
    malformed = c.get("/api/debate/debate%20test/record")
    assert malformed.status_code == 400 and malformed.json() == {"error": "invalid run_id"}

    malformed_events = c.get("/api/debate/debate%20test/events")
    assert malformed_events.status_code == 400


# ── 观众投票不在这两个端点里重复：盲投规则完全交给 audience.py 现成端点 ─────

def test_audience_ballots_not_embedded_in_viewer_endpoints(tmp_path, monkeypatch):
    """观众票有 audience.py 自己的 /vote /votes（已经处理盲投规则）；这两个端点不重复
    实现投票，也不该在响应里带出任何观众票分布——即使 state 上真的挂了 audience 汇总。"""
    _write_state(tmp_path, audience={"voters": 9, "all": {"pro": 5, "con": 4}})
    c = _client(tmp_path, monkeypatch)
    body = c.get("/api/debate/debate-test1/record").json()
    assert "audience" not in body
    assert "audience" not in json.dumps(body)
