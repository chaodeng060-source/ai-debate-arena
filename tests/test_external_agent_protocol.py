from __future__ import annotations

import json
import threading
import time

import pytest

from arena import prep
from arena import room


def _external(label: str, owner: str) -> dict:
    return {
        "engine": "external",
        "model": f"agent:{label}",
        "effort": "-",
        "label": label,
        "owner": owner,
        "agent_id": f"agent:{label}",
        "session_id": f"owner-session:{label}",
        "capabilities": ["memory", "mcp", "web_search", "memory"],
    }


def test_external_pool_keeps_owner_managed_agent_contract() -> None:
    pool = room.parse_pool([
        _external("A", "owner-a"),
        _external("B", "owner-b"),
        _external("C", "owner-c"),
        _external("D", "owner-d"),
    ])

    assert pool[0]["agent_id"] == "agent:A"
    assert pool[0]["session_id"] == "owner-session:A"
    assert pool[0]["capabilities"] == ["memory", "mcp", "web_search"]


@pytest.mark.parametrize("field", ["api_key", "token", "credentials", "mcp_config", "memory_body"])
def test_external_pool_rejects_private_runtime_material(field: str) -> None:
    seats = [_external(label, f"owner-{label}") for label in "ABCD"]
    seats[0][field] = "must-stay-with-owner"
    with pytest.raises(ValueError, match="owner-side"):
        room.parse_pool(seats)


def test_protocol_v2_carries_stable_session_and_turn_metadata_without_private_runtime() -> None:
    req = prep.external_request(
        run_id="run-1",
        seq=7,
        seat="正方一辩",
        system="system",
        prompt="prompt",
        deadline_epoch=123.0,
        kind="prep",
        participant={
            "agent_id": "agent:A",
            "owner": "owner-a",
            "session_id": "owner-session:A",
            "capabilities": ["memory", "mcp", "web_search"],
        },
        turn={
            "phase": "prep",
            "stage": "discussion",
            "side": "pro",
            "round_index": 2,
            "turn_index": 3,
            "reply_to_turn_index": 2,
        },
    )

    assert req["protocol_version"] == 2
    assert req["request_id"] == "run-1:0007"
    assert req["participant"]["session_id"] == "owner-session:A"
    assert req["participant"]["capabilities"] == ["memory", "mcp", "web_search"]
    assert req["turn"] == {
        "phase": "prep",
        "stage": "discussion",
        "side": "pro",
        "round_index": 2,
        "turn_index": 3,
        "reply_to_turn_index": 2,
    }
    encoded = json.dumps(req, ensure_ascii=False)
    assert all(secret not in encoded for secret in ("api_key", "credentials", "mcp_config", "memory_body"))


def test_external_speak_reuses_one_session_across_prep_and_match(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(room, "INBOX_ROOT", tmp_path)
    monkeypatch.setattr(room, "_EXTERNAL_SEQ", {})
    seat = _external("A", "owner-a") | {
        "name": "正方一辩",
        "side": "pro",
        "seat": 1,
        "run_id": "run-session",
    }

    class _StopAfterRequest(RuntimeError):
        pass

    def stop_after_write(_seconds: float) -> None:
        raise _StopAfterRequest

    monkeypatch.setattr(room.time, "sleep", stop_after_write)

    with pytest.raises(_StopAfterRequest):
        room._run_cli(
            seat,
            "system",
            "讨论",
            timeout=5,
            kind="prep",
            request_context={"phase": "prep", "stage": "discussion", "turn_index": 1},
        )
    with pytest.raises(_StopAfterRequest):
        room._run_cli(seat, "system", "立论", timeout=5, kind="speech")

    requests = [json.loads(path.read_text("utf-8")) for path in sorted((tmp_path / "run-session").glob("*.request.json"))]
    assert len(requests) == 2
    assert {row["participant"]["session_id"] for row in requests} == {"owner-session:A"}
    assert requests[0]["turn"]["stage"] == "discussion"
    assert requests[1]["turn"] == {"phase": "match", "stage": "speech", "side": "pro"}


def test_external_sequence_resumes_after_largest_request_on_disk(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(room, "INBOX_ROOT", tmp_path)
    monkeypatch.setattr(room, "_EXTERNAL_SEQ", {})
    folder = tmp_path / "run-resume"
    folder.mkdir()
    (folder / "0007-正方一辩.request.json").write_text("{}", encoding="utf-8")
    assert room._next_external_seq("run-resume") == 8


def test_external_speak_accepts_identity_checked_json_only_reply(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(room, "INBOX_ROOT", tmp_path)
    monkeypatch.setattr(room, "_EXTERNAL_SEQ", {})
    seat = _external("A", "owner-a") | {
        "name": "正方一辩", "side": "pro", "seat": 1, "run_id": "run-json",
    }

    def owner_bridge() -> None:
        req_path, reply_path = prep.external_paths(tmp_path, "run-json", 1, "正方一辩")
        for _ in range(100):
            if req_path.exists():
                request = json.loads(req_path.read_text("utf-8"))
                receipt = {
                    "protocol_version": 2,
                    "request_id": request["request_id"],
                    "agent_id": request["participant"]["agent_id"],
                    "status": "completed",
                    "output": "同一个哥哥继续作答",
                }
                prep.external_reply_envelope_path(reply_path).write_text(
                    json.dumps(receipt, ensure_ascii=False), encoding="utf-8",
                )
                return
            time.sleep(0.01)
        raise AssertionError("request was not written")

    thread = threading.Thread(target=owner_bridge)
    thread.start()
    output = room._run_cli(seat, "system", "prompt", timeout=5)
    thread.join()
    assert output == "同一个哥哥继续作答"


def test_external_match_state_declares_owner_managed_session(tmp_path, monkeypatch) -> None:
    seen: dict = {}

    async def fake_schedule(state, out, *, timeout, emit_opening):
        seen.update(state)

    async def no_emit(*_args, **_kwargs):
        return ""

    pool = room.parse_pool([_external(label, f"owner-{label}") for label in "ABCD"])
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)
    monkeypatch.setattr(room, "_run_schedule", fake_schedule)
    monkeypatch.setattr(room, "_emit_to_room", no_emit)
    monkeypatch.setattr(room, "_fact_base_for", lambda _topic: "")
    import asyncio
    asyncio.run(room._run_match(
        "甲/乙", "甲", "乙", "mini", "zh", timeout=5,
        prep_enabled=False, bench_enabled=False, pool=pool,
        prep_discussion_rounds=3, prep_discussion_seconds=420,
    ))
    assert seen["context_contract"]["contestant_session"] == "owner_managed_external"
    assert seen["context_contract"]["external_runtime"] == "owner_managed"
    assert seen["prep_discussion_rounds"] == 3
    assert seen["prep_discussion_seconds"] == 420


def test_invalid_v2_envelope_cannot_fall_back_to_unbound_text(tmp_path) -> None:
    request = prep.external_request(
        run_id="run-cross", seq=1, seat="正方一辩", system="", prompt="",
        deadline_epoch=time.time() + 60,
        participant={"agent_id": "agent:a", "session_id": "run-cross:agent:a"},
    )
    _req_path, reply_path = prep.external_paths(tmp_path, "run-cross", 1, "正方一辩")
    reply_path.parent.mkdir(parents=True)
    reply_path.write_text("这段裸文本不能绕过身份校验", encoding="utf-8")
    prep.external_reply_envelope_path(reply_path).write_text(json.dumps({
        "protocol_version": 2,
        "request_id": request["request_id"],
        "agent_id": "agent:b",
        "status": "completed",
        "output": "串线稿",
    }), encoding="utf-8")

    ready, output = room._read_external_reply(request, reply_path)
    assert ready is False and output == ""


def test_start_api_forwards_bounded_prep_discussion_controls(monkeypatch) -> None:
    captured: dict = {}

    async def fake_match(*_args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(room, "_run_match", fake_match)
    monkeypatch.setattr(room, "MAX_CONCURRENT", 1)
    room._RUNS.clear()

    import asyncio

    async def exercise() -> None:
        status, payload = await room._launch({
            "topic": "甲/乙", "format": "mini", "prep": True, "bench": False,
            "prep_discussion_rounds": 99, "prep_discussion_seconds": 5,
        })
        assert status == 200
        assert payload["prep_discussion_rounds"] == 6
        assert payload["prep_discussion_seconds"] == 30
        await room._RUNS[payload["run_id"]]["task"]

    asyncio.run(exercise())
    assert captured["prep_discussion_rounds"] == 6
    assert captured["prep_discussion_seconds"] == 30
    assert room._RUNS == {}
