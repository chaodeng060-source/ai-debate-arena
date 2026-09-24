"""tools/demo.py 的两处加固：只认本机 Host（防 DNS rebinding）、缺依赖时给一句人话。

DNS rebinding：恶意网页把自己的域名解析改指 127.0.0.1，浏览器就把它当「同源」，能读能写
本机端口——跨站检查（Sec-Fetch-Site / Origin）对它无效，只有 Host 头露馅（是对方的域名）。
demo 服务只绑 127.0.0.1，正常访问的 Host 只会是 127.0.0.1 或 localhost。
"""
from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

from fastapi.testclient import TestClient

from tools import demo

ROOT = Path(__file__).resolve().parents[1]


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


# ── 缺依赖：一句人话说清该装哪个 extra，而不是一屏 traceback ─────────────────────

def test_demo_without_uvicorn_says_which_extra_to_install(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "uvicorn", None)       # 模拟没装 uvicorn
    monkeypatch.setenv("DEBATE_POSITION_RECHECK", "off")
    assert demo.main([]) == 2
    out = capsys.readouterr()
    assert "uvicorn" in out.err and '.[demo]' in out.err, out.err
    assert "观赛地址" not in out.out, "服务起不来，就别先打印一个打不开的观赛地址"


def _run_demo_with_blocked_modules(*modules: str) -> subprocess.CompletedProcess:
    """子进程里把模块挡掉再跑 demo.py：挡的是 import 期就会失败的依赖，没法在本进程里模拟。
    uvicorn 一并挡住兜底——万一 import 意外成功，也不会真的起服务把测试卡住。"""
    blocked = ", ".join(repr(m) for m in (*modules, "uvicorn"))
    code = (f"import sys, runpy\nfor m in ({blocked},):\n    sys.modules[m] = None\n"
            "runpy.run_path('tools/demo.py', run_name='__main__')")
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                          capture_output=True, text=True, timeout=120)


def test_demo_without_fastapi_prints_install_hint_not_traceback():
    out = _run_demo_with_blocked_modules("fastapi")
    assert out.returncode != 0
    assert "Traceback" not in out.stderr, out.stderr
    assert "fastapi" in out.stderr and '.[demo]' in out.stderr, out.stderr


def test_demo_on_native_windows_points_to_wsl():
    out = _run_demo_with_blocked_modules("fcntl")   # 原生 Windows 的 Python 没有 fcntl
    assert out.returncode != 0
    assert "Traceback" not in out.stderr, out.stderr
    assert "WSL" in out.stderr, out.stderr
