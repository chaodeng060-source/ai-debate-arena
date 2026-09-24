"""本机 MCP 服务（tools/mcp_server.py）。

先测普通函数（`*_impl`，不依赖 `mcp` 包，随时能测）；再一条端到端——引擎那边真发一道
外部出题（复用 room._external_speak，扔进后台线程，因为它会阻塞等回稿），MCP 的
next_turn 在主线程里拿到题、submit_turn 交稿，_external_speak 那头读到的应该正好是
这段文字。最后是 FastMCP 注册相关的测试：装了 `mcp` 包就真的建服务、核对工具名和参数；
没装就在函数体内用 pytest.importorskip 单独跳过那几条，不影响上面的普通函数测试。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from arena import room
from tools import mcp_server


# ── 上场：next_turn（纯函数）───────────────────────────────────────────

def test_next_turn_pending_false_when_nothing_queued(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server._room, "INBOX_ROOT", tmp_path / "inbox")
    result = mcp_server.next_turn_impl(agent_id="agent:nobody", run_id="", wait_seconds=0)
    assert result == {"pending": False}


def test_next_turn_wait_seconds_is_capped(tmp_path, monkeypatch):
    """客户端传 9999 秒也不能真的等那么久——夹到 MAX_WAIT_SECONDS。这里把上限本身
    调小到 0.2s 来测夹没夹住，不然这条用例得跑 50 秒。"""
    monkeypatch.setattr(mcp_server._room, "INBOX_ROOT", tmp_path / "inbox")
    monkeypatch.setattr(mcp_server, "MAX_WAIT_SECONDS", 0.2)
    started = time.time()
    result = mcp_server.next_turn_impl(wait_seconds=9999)
    elapsed = time.time() - started
    assert result == {"pending": False}
    assert elapsed < 2, f"wait_seconds 必须被夹到 MAX_WAIT_SECONDS，实际等了 {elapsed}s"


# ── 上场：submit_turn（纯函数）─────────────────────────────────────────

def test_submit_turn_rejects_empty_text_and_unknown_id(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server._room, "INBOX_ROOT", tmp_path / "inbox")
    assert mcp_server.submit_turn_impl("whatever:0001", "   ")["ok"] is False
    assert mcp_server.submit_turn_impl("no-such-run:0001", "稿子")["ok"] is False


def test_submit_turn_rejects_already_answered(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    monkeypatch.setattr(mcp_server._room, "INBOX_ROOT", inbox)
    monkeypatch.setattr(room, "INBOX_ROOT", inbox)
    seat = {"engine": "external", "run_id": "dup-run", "name": "正方一辩", "agent_id": "agent:x"}

    result: dict = {}

    def speak():
        result["text"] = room._external_speak(seat, "S", "P", 15, kind="speech")

    t = threading.Thread(target=speak)
    t.start()

    turn = None
    deadline = time.time() + 10
    while time.time() < deadline:
        turn = mcp_server.next_turn_impl(agent_id="agent:x", run_id="dup-run", wait_seconds=1)
        if turn.get("pending"):
            break
    assert turn and turn["pending"] is True, turn

    first = mcp_server.submit_turn_impl(turn["request_id"], "第一次交的稿")
    assert first["ok"] is True
    t.join(timeout=15)
    assert result["text"] == "第一次交的稿"

    second = mcp_server.submit_turn_impl(turn["request_id"], "想再交一次")
    assert second["ok"] is False, "已经回过的 request_id 必须被拒绝"


# ── 端到端：引擎真发一道题，MCP 接住、交稿，引擎读到同一段文字 ────────────

def test_next_turn_and_submit_turn_round_trip_with_real_external_request(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    monkeypatch.setattr(mcp_server._room, "INBOX_ROOT", inbox)
    monkeypatch.setattr(room, "INBOX_ROOT", inbox)
    run_id = "e2e-run"
    seat = {"engine": "external", "run_id": run_id, "name": "正方一辩",
            "agent_id": "agent:probe", "model": "ext:probe"}

    result: dict = {}

    def speak():
        result["text"] = room._external_speak(
            seat, "SYSTEM", "现在轮到你：正方一辩·立论", 20, kind="speech",
        )

    t = threading.Thread(target=speak)
    t.start()

    turn = None
    deadline = time.time() + 10
    while time.time() < deadline:
        turn = mcp_server.next_turn_impl(agent_id="agent:probe", run_id=run_id, wait_seconds=1)
        if turn.get("pending"):
            break
    assert turn and turn["pending"] is True, turn
    assert turn["run_id"] == run_id and turn["kind"] == "speech" and turn["seat"] == "正方一辩"
    assert turn["system"] == "SYSTEM" and turn["prompt"] == "现在轮到你：正方一辩·立论"
    assert turn["deadline_epoch"]
    # 出题投影里不许带服务器路径
    assert str(inbox) not in json.dumps(turn, ensure_ascii=False)

    reply_text = "这是 MCP 交的稿：题面负担在我方定义下成立。"
    submitted = mcp_server.submit_turn_impl(turn["request_id"], reply_text)
    assert submitted == {"ok": True, "request_id": turn["request_id"], "chars": len(reply_text)}

    t.join(timeout=15)
    assert result["text"] == reply_text, "引擎那头 _external_speak 应该原样读到 MCP 交的稿"


# ── 看台：list_matches / read_match / vote / like（纯函数）────────────────

def _write_match(tmp_path, run_id, **overrides):
    started_at = overrides.pop("started_at", "2026-09-24T10:00:00+0800")
    state = {
        "run_id": run_id, "status": "completed", "phase": "done", "format": "mini", "lang": "zh",
        "topic": f"{run_id} 的辩题", "pro_side": "甲", "con_side": "乙", "started_at": started_at,
        "roster": [
            {"name": "正方一辩", "side": "pro", "seat": 1, "label": "甲队", "engine": "external",
             "model": "ext:probe-model", "effort": "-"},
        ],
        "schedule": [{"index": 0, "stage": "正方一辩·立论", "side": "pro", "seat": 1, "seconds": 180}],
        "transcript": [
            {"speaker": "正方一辩", "side": "pro", "stage": "正方一辩·立论", "text": "开场立论正文",
             "schedule_index": 0, "chars": 6, "limit": 1170, "truncated": False, "elapsed_sec": 1.0,
             "defection_hits": [], "msg_id": "m1"},
        ],
        "crossfire": [], "bench": [],
        # 评委身份（judge_label / panel.label）是内部字段，投影里必须消失——见下面的断言。
        "jury": {"status": "decided", "winner": "pro", "counts": {"pro": 3, "con": 0},
                 "panel": [{"name": "评委甲", "label": "JUDGE_MODEL_LEAK"}],
                 "ballots": [{"judge": "评委甲", "judge_label": "JUDGE_MODEL_LEAK", "role": "primary",
                              "valid": True, "winner": "pro", "reason": "R", "evidence": []}]},
    }
    state.update(overrides)
    (tmp_path / f"{run_id}.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return state


def test_list_matches_caps_at_20_and_has_no_leaks(tmp_path, monkeypatch):
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)
    for i in range(25):
        _write_match(tmp_path, f"debate-{i:03d}", started_at=f"2026-09-{(i % 28) + 1:02d}T00:00:00+0800")

    out = mcp_server.list_matches_impl()
    assert len(out["matches"]) == 20
    for m in out["matches"]:
        assert set(m) == {"run_id", "status", "topic", "started_at"}

    raw = json.dumps(out, ensure_ascii=False)
    assert str(tmp_path) not in raw and "JUDGE_MODEL_LEAK" not in raw


def test_read_match_reuses_public_projection_and_validates_run_id(tmp_path, monkeypatch):
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)
    _write_match(tmp_path, "debate-read")

    out = mcp_server.read_match_impl("debate-read")
    assert out["run_id"] == "debate-read" and out["events"]
    raw = json.dumps(out, ensure_ascii=False)
    assert str(tmp_path) not in raw and "JUDGE_MODEL_LEAK" not in raw

    # run_id 校验也是复用 room._load_record：格式不对 400、没这场 404
    missing = mcp_server.read_match_impl("debate-does-not-exist")
    assert missing["status_code"] == 404
    bad = mcp_server.read_match_impl("bad id")
    assert bad["status_code"] == 400


def test_vote_records_as_ai(tmp_path, monkeypatch):
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)
    _write_match(tmp_path, "debate-vote", status="running", phase="match")

    out = mcp_server.vote_impl("debate-vote", "mcp-voter", "pro")
    assert out["ok"] is True

    data = json.loads((tmp_path / "votes" / "debate-vote.json").read_text("utf-8"))
    entry = data["votes"]["mcp-voter"]
    assert entry["voter_kind"] == "ai" and entry["side"] == "pro"


def test_like_records_via_mcp(tmp_path, monkeypatch):
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)
    _write_match(tmp_path, "debate-like", status="running", phase="match")

    out = mcp_server.like_impl("debate-like", "mcp-liker", 0)
    assert out == {"ok": True, "run_id": "debate-like", "seq": 0, "liked": True, "count": 1}

    again = mcp_server.like_impl("debate-like", "mcp-liker", 0, liked=False)
    assert again["count"] == 0


# ── FastMCP 注册：装了 mcp 包才跑，没装就在函数体内单独跳过这几条 ───────────

def test_tools_are_registered_with_expected_names_and_params():
    pytest.importorskip("mcp", reason="mcp 包没装，跳过 FastMCP 注册测试；工具逻辑本身在上面的用例里已经测过")
    server = mcp_server.build_server()
    tools = asyncio.run(server.list_tools())
    by_name = {t.name: t for t in tools}

    assert set(by_name) == {"next_turn", "submit_turn", "list_matches", "read_match", "vote", "like"}
    assert set(by_name["next_turn"].inputSchema["properties"]) == {"agent_id", "run_id", "wait_seconds"}
    assert set(by_name["submit_turn"].inputSchema["properties"]) == {"request_id", "text"}
    assert set(by_name["list_matches"].inputSchema.get("properties", {})) == set()
    assert set(by_name["read_match"].inputSchema["properties"]) == {"run_id", "since"}
    assert set(by_name["vote"].inputSchema["properties"]) == {"run_id", "voter_id", "side", "favorite", "reason"}
    assert set(by_name["like"].inputSchema["properties"]) == {"run_id", "voter_id", "seq", "liked"}
    # 没有任何管理工具（开赛/停赛/排队）——这套引擎没有鉴权，管理动作不该经 MCP 开放
    assert not ({"start_match", "stop_match", "queue", "start", "stop"} & set(by_name))


def test_registered_vote_tool_round_trips_through_call_tool(tmp_path, monkeypatch):
    """走真正的 MCP 调用路径（server.call_tool），不是直接调 `vote_impl`——确认注册接线
    本身也是对的，不只是 `_impl` 函数自己对。"""
    pytest.importorskip("mcp", reason="mcp 包没装，跳过 FastMCP 注册测试；工具逻辑本身在上面的用例里已经测过")
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)
    _write_match(tmp_path, "debate-call", status="running", phase="match")
    server = mcp_server.build_server()

    async def call():
        return await server.call_tool(
            "vote", {"run_id": "debate-call", "voter_id": "v-call", "side": "con"},
        )

    blocks = asyncio.run(call())
    payload = json.loads(blocks[0].text)
    assert payload["ok"] is True

    data = json.loads((tmp_path / "votes" / "debate-call.json").read_text("utf-8"))
    entry = data["votes"]["v-call"]
    assert entry["voter_kind"] == "ai" and entry["side"] == "con"
