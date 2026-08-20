"""桥（tools/bridge.py）：投稿箱扫描 + stub 代填 + 回稿能被引擎的解析器吃进去。

stub 只为流程验收（零额度场）；这里盯的是「它回的东西引擎认不认」——尤其评委票：
evidence 必须能在盲审转录里原文找回来，否则验收场三张票全无效、流程就验不到裁决那一步。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arena.prep import (  # noqa: E402
    EXTERNAL_KINDS, blind_transcript, build_ballot_prompt, build_bench_question_prompt,
    external_paths, external_reply_envelope_path, external_reply_output, external_request,
    parse_ballot, parse_bench_question,
)
from tools import bridge as bridge  # noqa: E402


def _drop(inbox: Path, run_id: str, seq: int, seat: str, *, kind: str, prompt: str = "写一段发言",
          deadline: float | None = None) -> tuple[Path, Path]:
    req_path, reply_path = external_paths(inbox, run_id, seq, seat)
    req = external_request(run_id=run_id, seq=seq, seat=seat, system="sys", prompt=prompt,
                           deadline_epoch=deadline if deadline is not None else time.time() + 60, kind=kind)
    req_path.parent.mkdir(parents=True, exist_ok=True)
    req_path.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
    return req_path, reply_path


def test_external_request_rejects_unknown_kind():
    with pytest.raises(ValueError):
        external_request(run_id="r", seq=1, seat="s", system="", prompt="", deadline_epoch=1.0, kind="poem")
    for kind in EXTERNAL_KINDS:
        assert external_request(run_id="r", seq=1, seat="s", system="", prompt="", deadline_epoch=1.0,
                                kind=kind)["kind"] == kind


def test_pending_skips_answered_and_expired(tmp_path):
    inbox = tmp_path / "inbox"
    _drop(inbox, "run-a", 1, "正方一辩", kind="speech")
    _, replied = _drop(inbox, "run-a", 2, "反方一辩", kind="speech")
    replied.write_text("已回", encoding="utf-8")
    _drop(inbox, "run-a", 3, "评委甲", kind="ballot", deadline=time.time() - 1)
    _drop(inbox, "run-b", 1, "正方一辩", kind="speech")

    only_a = bridge.pending_requests(inbox, "run-a")
    assert [r["seq"] for _, _, r in only_a] == [1]
    everything = bridge.pending_requests(inbox, None)
    assert [(r["run_id"], r["seq"]) for _, _, r in everything] == [("run-a", 1), ("run-b", 1)]


def test_run_once_with_stub_answers_every_kind_atomically(tmp_path):
    inbox = tmp_path / "inbox"
    for i, kind in enumerate(EXTERNAL_KINDS, 1):
        _drop(inbox, "run-x", i, f"席{i}", kind=kind)
    n = bridge.run(inbox, "run-x", bridge.stub_handler, once=True)
    assert n == len(EXTERNAL_KINDS)
    for i in range(1, len(EXTERNAL_KINDS) + 1):
        _, reply = external_paths(inbox, "run-x", i, f"席{i}")
        assert reply.exists() and reply.read_text(encoding="utf-8").strip()
    assert not list((inbox / "run-x").glob("*.tmp")), "写回必须走 tmp→rename，不能留半截"
    # 再跑一遍什么都不该做
    assert bridge.run(inbox, "run-x", bridge.stub_handler, once=True) == 0


def test_v2_bridge_filters_agent_and_writes_valid_structured_receipt(tmp_path):
    inbox = tmp_path / "inbox"
    for seq, agent_id in ((1, "agent:a"), (2, "agent:b")):
        req_path, _reply_path = external_paths(inbox, "run-v2", seq, f"席{seq}")
        req = external_request(
            run_id="run-v2", seq=seq, seat=f"席{seq}", system="sys", prompt="写一段发言",
            deadline_epoch=time.time() + 60, kind="speech",
            participant={"agent_id": agent_id, "owner": f"owner-{seq}",
                         "session_id": f"run-v2:{agent_id}", "capabilities": ["memory", "mcp"]},
            turn={"phase": "match", "stage": "speech"},
        )
        req_path.parent.mkdir(parents=True, exist_ok=True)
        req_path.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")

    assert bridge.run(inbox, "run-v2", bridge.stub_handler, once=True, agent_id="agent:a") == 1
    req_path, reply_path = external_paths(inbox, "run-v2", 1, "席1")
    receipt = json.loads(external_reply_envelope_path(reply_path).read_text("utf-8"))
    request = json.loads(req_path.read_text("utf-8"))
    assert receipt["protocol_version"] == 2
    assert receipt["request_id"] == request["request_id"]
    assert receipt["agent_id"] == "agent:a"
    assert receipt["status"] == "completed"
    assert external_reply_output(request, receipt)
    assert reply_path.exists(), "v1 bridge readers keep getting the text projection"
    _req_b, reply_b = external_paths(inbox, "run-v2", 2, "席2")
    assert not reply_b.exists()


def test_v2_reply_rejects_crossed_request_or_agent_identity():
    request = external_request(
        run_id="run-v2", seq=1, seat="席1", system="", prompt="", deadline_epoch=time.time() + 60,
        participant={"agent_id": "agent:a", "session_id": "run-v2:agent:a"},
    )
    good = {
        "protocol_version": 2, "request_id": request["request_id"], "agent_id": "agent:a",
        "status": "completed", "output": "答复",
    }
    assert external_reply_output(request, good) == "答复"
    with pytest.raises(ValueError, match="request_id"):
        external_reply_output(request, good | {"request_id": "other:0001"})
    with pytest.raises(ValueError, match="agent_id"):
        external_reply_output(request, good | {"agent_id": "agent:b"})


def test_pending_can_repair_an_invalid_v2_receipt(tmp_path):
    inbox = tmp_path / "inbox"
    req_path, reply_path = external_paths(inbox, "run-repair", 1, "席1")
    request = external_request(
        run_id="run-repair", seq=1, seat="席1", system="", prompt="",
        deadline_epoch=time.time() + 60,
        participant={"agent_id": "agent:a", "session_id": "run-repair:agent:a"},
    )
    req_path.parent.mkdir(parents=True)
    req_path.write_text(json.dumps(request), encoding="utf-8")
    external_reply_envelope_path(reply_path).write_text(json.dumps({
        "protocol_version": 2, "request_id": request["request_id"],
        "agent_id": "agent:b", "status": "completed", "output": "wrong",
    }), encoding="utf-8")
    assert [row[2]["request_id"] for row in bridge.pending_requests(inbox)] == [request["request_id"]]


def test_stub_ballot_is_accepted_by_parse_ballot():
    transcript = [
        {"speaker": "正方一辩", "side": "pro", "stage": "正方一辩立论",
         "text": "我方认为时间赋予生命意义。第一，没有时间的刻度，意义无从谈起。第二，正因为有限，选择才有重量。"},
        {"speaker": "反方一辩", "side": "con", "stage": "反方一辩立论",
         "text": "我方认为生命赋予时间意义。时间本身只是物理量，是人的体验让它有了方向。"},
        {"speaker": "正方一辩", "side": "pro", "stage": "正方总结",
         "text": "对方把体验和意义混为一谈。体验发生在时间里，这恰恰说明时间在先。"},
    ]
    blinded, side_to_label = blind_transcript(transcript)
    prompt = build_ballot_prompt(topic="时间赋予生命意义/生命赋予时间意义", blinded=blinded)
    raw = bridge.stub_handler({"kind": "ballot", "seat": "评委甲", "prompt": prompt})
    ballot = parse_ballot(raw, transcript=transcript, side_to_label=side_to_label, ballot_id="b1")
    assert ballot["valid"], ballot
    assert len(ballot["evidence"]) >= 2
    assert ballot["winner"] in {"pro", "con"}
    assert ballot["scores"] and ballot["mvp"]


def test_stub_bench_question_is_accepted():
    blinded, side_to_label = blind_transcript([
        {"speaker": "正方一辩", "side": "pro", "stage": "正方一辩立论", "text": "立论。"},
    ])
    prompt = build_bench_question_prompt(topic="t/u", blinded=blinded)
    raw = bridge.stub_handler({"kind": "bench_question", "seat": "评委乙", "prompt": prompt})
    assert parse_bench_question(raw, side_to_label=side_to_label)


def test_stub_speech_respects_char_limit():
    raw = bridge.stub_handler({"kind": "speech", "seat": "正方一辩", "prompt": "请发言，不超过 120 字。"})
    assert raw and len(raw) <= 120


def test_aisay_handler_is_not_ready_and_does_not_fake(tmp_path):
    inbox = tmp_path / "inbox"
    _, reply = _drop(inbox, "run-y", 1, "正方一辩", kind="speech")
    assert bridge.run(inbox, "run-y", bridge.aisay_handler, once=True) == 0
    assert not reply.exists()
