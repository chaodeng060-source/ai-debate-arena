"""写接口的请求体不是合法 JSON（写到一半、空 body、不是 UTF-8）时，回 400 加一句说明，不是光秃秃的 500。

Content-Type 闸（见 test_cross_site_guard.py）只管「声明的是不是 application/json」，管不到 JSON 本身
写坏了：Starlette 的 Request.json() 解析失败直接抛 JSONDecodeError，以前没人接，调用方只拿到一个
Internal Server Error，看不出是自己的请求体有问题。五个带请求体的写接口都走同一条读法。
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena import room

RUN_ID = "debate-json"

WRITE_ENDPOINTS = [
    "/api/debate/start",
    "/api/debate/queue",
    "/api/debate/topics/suggest",
    f"/api/debate/{RUN_ID}/vote",
    f"/api/debate/{RUN_ID}/like",
]

BAD_BODIES = {
    "truncated": '{"topic": "甲/乙",'.encode("utf-8"),
    "empty": b"",
    "not_utf8": b"\xc3\x28",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)
    monkeypatch.setattr(room, "QUEUE_PATH", tmp_path / "queue.json")
    monkeypatch.setattr(room, "TOPIC_SUGGEST_PATH", tmp_path / "topic_suggestions.json")
    monkeypatch.setattr(room, "_kick_drain", lambda: None)   # 入队也不真的出队开赛
    launches: list = []

    async def fake_launch(body):
        launches.append(body)
        return 200, {"ok": True, "run_id": "debate-fake"}

    monkeypatch.setattr(room, "_launch", fake_launch)

    # /like 先认场次再读请求体：放一场正在打的比赛，让请求走到读 body 那一步
    state = {
        "run_id": RUN_ID, "status": "running", "phase": "match", "format": "mini", "lang": "zh",
        "topic": "甲/乙", "pro_side": "甲", "con_side": "乙", "roster": [], "schedule": [],
        "transcript": [
            {"speaker": "正方一辩", "side": "pro", "stage": "立论", "text": "正文",
             "schedule_index": 0, "chars": 2, "limit": 100, "truncated": False,
             "elapsed_sec": 1.0, "defection_hits": [], "msg_id": "m1"},
        ],
        "crossfire": [], "bench": [], "jury": None,
    }
    (tmp_path / f"{RUN_ID}.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    app = FastAPI()
    app.include_router(room.router)
    # raise_server_exceptions=False：读 body 炸掉时拿到的是真实的 500 响应，而不是异常直接穿透测试
    return TestClient(app, raise_server_exceptions=False), launches


@pytest.mark.parametrize("body", list(BAD_BODIES.values()), ids=list(BAD_BODIES))
@pytest.mark.parametrize("path", WRITE_ENDPOINTS)
def test_malformed_json_body_gets_400_with_a_readable_error(env, tmp_path, path, body):
    client, launches = env
    r = client.post(path, content=body, headers={"content-type": "application/json"})

    assert r.status_code == 400, (r.status_code, r.text)
    assert r.json() == {"error": "request body is not valid JSON"}
    # 什么都没发生：没开赛、没入队、没收荐题、没落票、没记赞
    assert launches == []
    assert room._read_queue() == []
    assert room._read_suggestions() == []
    assert not (tmp_path / "votes").exists()
    assert not (tmp_path / "likes").exists()
