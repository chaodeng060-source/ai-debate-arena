"""GET /viewer：观赛单页由引擎同源挂出来，纯 HTML/CSS/JS，不需要 npm/构建。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena import room


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(room.router)
    return TestClient(app)


def test_viewer_page_is_served_same_origin():
    resp = _client().get("/viewer")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    # 三个只读数据来源都在页面脚本里被引用到，页面不是空壳
    assert "/record" in body and "/events" in body and "/votes" in body
    assert "<script>" in body and "</script>" in body
    # 不依赖任何构建产物或外部 CDN——单文件、离线可用
    assert "node_modules" not in body
    assert "cdn." not in body and "unpkg.com" not in body and "jsdelivr" not in body


def test_viewer_page_missing_file_returns_clean_error(monkeypatch, tmp_path):
    monkeypatch.setattr(room, "STATIC_DIR", tmp_path / "does-not-exist")
    resp = _client().get("/viewer")
    assert resp.status_code == 500
    assert resp.json() == {"error": "viewer page missing"}
