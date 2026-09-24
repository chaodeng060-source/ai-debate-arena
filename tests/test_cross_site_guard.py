"""本机服务防跨站：用户开着服务时随手打开的任意网页，不能借他的浏览器往 127.0.0.1 发写请求。

浏览器允许任何网页跨站发「简单请求」：Content-Type 是 text/plain / 表单、不带 CORS 预检，
响应读不到，但请求本身照样送达。Starlette 的 Request.json() 不看 Content-Type，body 是 JSON
就照样解析执行——所以一个恶意网页能替用户开赛（默认阵容是本机 codex/claude CLI，烧的是他的额度）、
排队、荐题、投票。这里用真 HTTP 请求实测这件事被挡住，而正常用法（JSON 请求、curl、自带观赛页）不受影响。
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena import room


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)
    monkeypatch.setattr(room, "QUEUE_PATH", tmp_path / "queue.json")
    monkeypatch.setattr(room, "TOPIC_SUGGEST_PATH", tmp_path / "topic_suggestions.json")
    # 入队后不真的出队开赛：这里只测请求有没有被收下，绝不能在测试里拉起本机 CLI 席位。
    monkeypatch.setattr(room, "_kick_drain", lambda: None)
    app = FastAPI()
    app.include_router(room.router)
    return TestClient(app)


@pytest.fixture
def launches(monkeypatch):
    """替换真正的开赛：记下收到了什么，不起任何比赛任务。"""
    calls: list = []

    async def fake_launch(body):
        calls.append(body)
        return 200, {"ok": True, "run_id": "debate-fake"}

    monkeypatch.setattr(room, "_launch", fake_launch)
    return calls


# 网页不经预检就能发出去的三种 Content-Type（fetch no-cors / <form>）
SIMPLE_REQUEST_TYPES = [
    "text/plain",
    "text/plain;charset=UTF-8",
    "application/x-www-form-urlencoded",
    "multipart/form-data; boundary=x",
]


@pytest.mark.parametrize("content_type", SIMPLE_REQUEST_TYPES)
def test_start_refuses_simple_request_body_so_a_web_page_cannot_launch_a_match(
        client, launches, content_type):
    body = json.dumps({"topic": "甲/乙", "format": "mini"})
    r = client.post("/api/debate/start", content=body, headers={"content-type": content_type})
    assert r.status_code == 415, r.text
    assert launches == [], "非 JSON 的请求体不许被当成开赛参数执行"


def test_other_write_endpoints_refuse_text_plain_too(client):
    queue_body = json.dumps({"topic": "甲/乙", "prep": False, "label": "网页偷偷排的一场"})
    r = client.post("/api/debate/queue", content=queue_body, headers={"content-type": "text/plain"})
    assert r.status_code == 415, r.text
    assert room._read_queue() == []

    suggest_body = json.dumps({"suggested_by": "someone", "pro": "正", "con": "反"})
    r = client.post("/api/debate/topics/suggest", content=suggest_body,
                    headers={"content-type": "text/plain"})
    assert r.status_code == 415, r.text
    assert room._read_suggestions() == []

    # 观众票挂在 audience.router 下，经 room.router 一起挂出来，同一道闸也要管到
    vote_body = json.dumps({"voter_id": "v1", "side": "pro"})
    r = client.post("/api/debate/debate-x/vote", content=vote_body,
                    headers={"content-type": "text/plain"})
    assert r.status_code == 415, r.text


def test_json_writes_and_bodyless_stop_still_work(client, launches):
    r = client.post("/api/debate/start", json={"topic": "甲/乙"})
    assert r.status_code == 200, r.text
    r = client.post("/api/debate/start", content=json.dumps({"topic": "丙/丁"}),
                    headers={"content-type": "application/json; charset=utf-8"})
    assert r.status_code == 200, r.text
    assert launches == [{"topic": "甲/乙"}, {"topic": "丙/丁"}]
    # 没有请求体的写请求（叫停）不看 Content-Type，curl -X POST 照常能用
    assert client.post("/api/debate/stop").status_code == 200
