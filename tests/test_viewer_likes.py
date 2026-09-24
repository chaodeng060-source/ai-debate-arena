"""观赛页的点赞按钮：脚本里用到了 /like /likes，卡片上有点赞按钮（class=like-btn）。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena import room


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(room.router)
    return TestClient(app)


def test_viewer_page_wires_up_like_endpoints_and_button():
    body = _client().get("/viewer").text
    assert "/like" in body and "/likes" in body
    assert "like-btn" in body
    # 台词一律走 textContent/createElement，不拼 innerHTML；点赞按钮延续这个写法
    assert 'el("button", "like-btn"' in body
