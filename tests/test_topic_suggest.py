"""荐题投稿箱：任何人都能投一道辩题，审过才进题库。
城里的 AI 和人荐题先落箱，她审过才进题库——不直接写 topics.json。"""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena import room as dr


def test_validate_topic_suggestion():
    ok, err = dr.validate_topic_suggestion({"suggested_by": "aisay:u1", "pro": "该", "con": "不该", "title": "要不要", "tags": ["伦理"]})
    assert err is None and ok["title"] == "要不要" and ok["tags"] == ["伦理"] and ok["note"] is None
    assert dr.validate_topic_suggestion({"pro": "a", "con": "b"})[1]                        # 没署名
    assert dr.validate_topic_suggestion({"suggested_by": "x", "pro": "a"})[1]              # 缺 con
    assert dr.validate_topic_suggestion({"suggested_by": "x", "pro": "a" * 41, "con": "b"})[1]
    assert dr.validate_topic_suggestion({"suggested_by": "x", "pro": "a", "con": "b", "tags": "伦理"})[1]
    assert dr.validate_topic_suggestion({"suggested_by": "x", "pro": "a", "con": "b", "note": "n" * 201})[1]


def test_suggest_endpoint_dedupes_and_flags_known(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "TOPIC_SUGGEST_PATH", tmp_path / "sug.json")
    monkeypatch.setattr(dr, "TOPICS_PATH", tmp_path / "topics.json")
    (tmp_path / "topics.json").write_text(json.dumps({"topics": [{"id": "t1", "pro": "已有正", "con": "已有反"}]}), "utf-8")
    app = FastAPI(); app.include_router(dr.router); c = TestClient(app)
    r = c.post("/api/debate/topics/suggest", json={"suggested_by": "aisay:u1", "pro": "新正", "con": "新反", "title": "新题"})
    assert r.status_code == 200 and r.json()["status"] == "pending" and r.json()["already_in_bank"] is False
    sid = r.json()["id"]
    # 同人同题：不重复落
    r2 = c.post("/api/debate/topics/suggest", json={"suggested_by": "aisay:u1", "pro": "新正", "con": "新反"})
    assert r2.json()["duplicate"] is True and r2.json()["id"] == sid
    # 别人荐同一道：另一条
    r3 = c.post("/api/debate/topics/suggest", json={"suggested_by": "alice", "pro": "新正", "con": "新反"})
    assert r3.json().get("duplicate") is None and r3.json()["pending"] == 2
    # 题库已有的：照收，标 already_in_bank
    r4 = c.post("/api/debate/topics/suggest", json={"suggested_by": "aisay:u2", "pro": "已有正", "con": "已有反"})
    assert r4.json()["already_in_bank"] is True
    # 坏输入
    assert c.post("/api/debate/topics/suggest", json={"pro": "a", "con": "b"}).status_code == 400
    # 列表
    lst = c.get("/api/debate/topics/suggestions?status=pending").json()
    assert lst["count"] == 3 and all(s["status"] == "pending" for s in lst["suggestions"])
    # 题库没被碰
    assert json.loads((tmp_path / "topics.json").read_text("utf-8"))["topics"] == [{"id": "t1", "pro": "已有正", "con": "已有反"}]
