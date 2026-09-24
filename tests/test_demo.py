"""tools/demo.py：一条命令起服务 + 打一场全 external 席位的演示赛。

零额度：辩手评委全是外部席位，稿子由 tools/bridge.py 的 stub handler 代填。
这里验的是「demo.py 真的能把引擎、桥、观赛页三样接到一块跑通一场」，不是辩论质量。
"""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from arena import room
from tools import demo


def test_demo_pool_and_run_id_are_well_formed():
    """不用真跑比赛也能测的部分：配置本身的形状对不对。"""
    assert len(demo.DEMO_POOL) == 4
    assert all(seat["engine"] == "external" and seat["effort"] == "-" for seat in demo.DEMO_POOL)
    assert len(demo.DEMO_JUDGES) == 3
    assert all(seat["engine"] == "external" and seat["effort"] == "-" for seat in demo.DEMO_JUDGES)
    # 席位标签不能撞名，撞了 _draw_roster 抽签会在同队悄悄吞掉一席
    assert len({s["label"] for s in demo.DEMO_POOL}) == 4

    run_id = demo.new_run_id()
    assert room._safe_run_id(run_id) == run_id, "demo 生成的 run_id 应该能过我们自己的校验"


def test_demo_match_completes_end_to_end_through_the_real_app(tmp_path, monkeypatch):
    """跟 tests/test_e2e_external_stub.py 一样零额度，区别是这次走的是 demo.py 自己
    拼的 app（lifespan 起桥 + 开赛）和我们新加的 /record 只读接口，而不是直接调内部函数。"""
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)
    monkeypatch.setattr(room, "INBOX_ROOT", tmp_path / "inbox")
    monkeypatch.setenv("DEBATE_POSITION_RECHECK", "off")   # 演示只跑一场，位置复判用不上
    room._RUNS.clear()

    run_id = demo.new_run_id()
    app = demo.build_app(run_id, timeout=30, crossfire_rounds=1)

    with TestClient(app) as c:
        # TestClient 用 with 才会真的触发 lifespan：桥线程和比赛 task 这时才起来。
        deadline = time.time() + 90
        rec = None
        while time.time() < deadline:
            resp = c.get(f"/api/debate/{run_id}/record")
            assert resp.status_code == 200, resp.text
            rec = resp.json()
            if rec["done"]:
                break
            time.sleep(0.5)

        assert rec is not None and rec["done"] is True, "演示赛没能在时限内打完"
        assert rec["status"] == "completed", rec

        # mini 赛制：6 段长稿 + 2 段交互质询（各自一个事件）+ 3 位评委插问 + 1 条评审票事件
        types = [e["type"] for e in rec["events"]]
        assert types.count("speech") == 6, types
        assert types.count("crossfire") == 2, types
        assert types.count("bench") == 3, types
        assert types[-1] == "jury", types
        assert len(types) == 12, types

        # 四个外部辩手席位都在场、都真的发了言（没有白卷——桥确实把请求接住回了稿）
        assert len(rec["roster"]) == 4 and all(s["engine"] == "external" for s in rec["roster"])
        speeches = [e for e in rec["events"] if e["type"] == "speech"]
        assert all((e.get("text") or "").strip() for e in speeches), speeches

        jury_event = rec["events"][-1]
        assert jury_event["status"] in {"decided", "disputed", "position_unstable"}, jury_event
        assert len(jury_event["ballots"]) == 3, jury_event["ballots"]   # 三张原序票，对调票已关
        # 评委是盲审，事件里不该带出评委的模型身份
        assert all("label" not in j for j in jury_event["panel"])

        # 观赛页跟接口是同一个 app 挂出来的，同一个 run_id 能直接打开
        viewer = c.get(f"/viewer?run_id={run_id}")
        assert viewer.status_code == 200
        assert "text/html" in viewer.headers["content-type"]

        # 观众票端点也在同一个 app 上（沿用 audience.py 现成的，demo 不用另外接线）
        votes = c.get(f"/api/debate/{run_id}/votes")
        assert votes.status_code == 200
