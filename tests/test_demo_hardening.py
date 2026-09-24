"""tools/demo.py 的两处加固：只认本机 Host（防 DNS rebinding）、缺依赖时给一句人话。

DNS rebinding：恶意网页把自己的域名解析改指 127.0.0.1，浏览器就把它当「同源」，能读能写
本机端口——跨站检查（Sec-Fetch-Site / Origin）对它无效，只有 Host 头露馅（是对方的域名）。
demo 服务只绑 127.0.0.1，正常访问的 Host 只会是 127.0.0.1 或 localhost。
"""
from __future__ import annotations

import sys
import types

from fastapi.testclient import TestClient

from tools import demo


def test_demo_app_only_answers_to_loopback_host_names():
    app = demo.build_app("debate-demo-hosttest", allowed_hosts=demo.LOOPBACK_HOSTS)
    c = TestClient(app, base_url="http://127.0.0.1:8877")   # 不进 with：不触发 lifespan、不开赛
    assert c.get("/viewer").status_code == 200
    assert c.get("/viewer", headers={"host": "localhost:8877"}).status_code == 200
    rebound = c.get("/api/debate/status", headers={"host": "attacker.example:8877"})
    assert rebound.status_code == 400, rebound.text


def test_demo_main_serves_with_loopback_host_check(monkeypatch):
    captured: dict = {}
    fake_uvicorn = types.ModuleType("uvicorn")

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    fake_uvicorn.run = fake_run
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setenv("DEBATE_POSITION_RECHECK", "off")   # main() 里是 setdefault，这里兜住还原

    assert demo.main(["--port", "8899"]) == 0
    assert captured["host"] == "127.0.0.1" and captured["port"] == 8899
    c = TestClient(captured["app"], base_url="http://127.0.0.1:8899")
    assert c.get("/viewer").status_code == 200
    assert c.get("/viewer", headers={"host": "attacker.example:8899"}).status_code == 400
