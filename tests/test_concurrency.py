"""多场并发：_RUNNING 单例 → _RUNS 注册表 + ContextVar 当前场。

外部 AI 参赛时一张桌一场太慢，必须能并发开多场。
这里只测引擎侧的并发骨架：上限闸、占位时序、每场各自的 run_id/推流标签、按场叫停、
队列在有空位时继续出队。真 CLI / 真推流全 mock 掉。
"""
from __future__ import annotations

import asyncio

import pytest

from arena import room as dr


class _Fake:
    """替身 _run_match：注册自己的 out_path、记下在自己上下文里看到的 run_id / 标签，
    然后挂在一个 Event 上等测试放行。"""

    def __init__(self) -> None:
        self.gates: dict[str, asyncio.Event] = {}
        self.seen: dict[str, dict] = {}

    async def __call__(self, *args, run_id=None, **kwargs):
        assert run_id, "_launch 必须把预分配的 run_id 传进来"
        dr._CUR_RUN.set(run_id)
        dr._register_run(run_id, out_path=dr.TRANSCRIPT_DIR / f"{run_id}.json")
        gate = self.gates.setdefault(run_id, asyncio.Event())
        self.seen[run_id] = {"ctx": dr._CUR_RUN.get(), "tag": dr._run_tag()}
        await gate.wait()
        # 放行后再读一次：并发场都在时标签应该带短号
        self.seen[run_id]["tag_after"] = dr._run_tag()

    def release(self, run_id: str) -> None:
        self.gates.setdefault(run_id, asyncio.Event()).set()


@pytest.fixture
def arena(monkeypatch, tmp_path):
    fake = _Fake()
    monkeypatch.setattr(dr, "_run_match", fake)
    monkeypatch.setattr(dr, "TRANSCRIPT_DIR", tmp_path)
    monkeypatch.setattr(dr, "QUEUE_PATH", tmp_path / "queue.json")
    monkeypatch.setattr(dr, "_finish_interrupted_record", lambda *a, **k: None)
    monkeypatch.setattr(dr, "_QUEUE_PACE_SECONDS", 0.01)

    async def _no_emit(body, *, title, **k):
        fake.seen.setdefault("_emits", []).append((dr._CUR_RUN.get(), title))
        return "x"
    monkeypatch.setattr(dr, "_emit_to_room", _no_emit)
    dr._RUNS.clear()
    yield fake
    dr._RUNS.clear()


def _body(topic="甲/乙"):
    return {"topic": topic, "format": "mini", "prep": False, "bench": False, "draw": False}


def test_default_limit_one_keeps_old_409_behaviour(arena, monkeypatch):
    monkeypatch.setattr(dr, "MAX_CONCURRENT", 1)

    async def main():
        s1, p1 = await dr._launch(_body())
        assert s1 == 200 and p1["run_id"].startswith("debate-")
        s2, p2 = await dr._launch(_body("丙/丁"))
        assert s2 == 409 and p2["error"] == "already_running"
        assert p2["running"] == 1 and p2["max_concurrent"] == 1
        await asyncio.sleep(0)   # 让 task 跑到 gate
        assert list(dr._RUNS) == [p1["run_id"]]
        arena.release(p1["run_id"])
        await dr._RUNS[p1["run_id"]]["task"]
        assert dr._RUNS == {}, "场打完必须注销，否则下一场永远 409"
        s3, _ = await dr._launch(_body("戊/己"))
        assert s3 == 200
        for rid in list(dr._RUNS):
            arena.release(rid)
            await dr._RUNS[rid]["task"]
    asyncio.run(main())


def test_two_slots_run_side_by_side_with_own_context(arena, monkeypatch):
    monkeypatch.setattr(dr, "MAX_CONCURRENT", 2)

    async def main():
        s1, p1 = await dr._launch(_body("甲/乙"))
        s2, p2 = await dr._launch(_body("丙/丁"))
        s3, p3 = await dr._launch(_body("戊/己"))
        assert (s1, s2, s3) == (200, 200, 409)
        a, b = p1["run_id"], p2["run_id"]
        assert a != b
        await asyncio.sleep(0)
        assert set(dr._RUNS) == {a, b}
        # 每个 task 在自己的上下文里看到的是自己的 run_id，标签带各自短号
        assert arena.seen[a]["ctx"] == a and arena.seen[b]["ctx"] == b
        assert arena.seen[a]["tag"] == f"[#{a[-6:]}] "
        assert arena.seen[b]["tag"] == f"[#{b[-6:]}] "
        assert arena.seen[a]["tag"] != arena.seen[b]["tag"]
        # 主线程（不在任何场的上下文里）看不到标签
        assert dr._run_tag() == ""
        # status 两场都在
        snap = dr._running_snapshot()
        assert {r["run_id"] for r in snap} == {a, b}
        assert all(r["out_path"].endswith(f"{r['run_id']}.json") for r in snap)
        arena.release(a); arena.release(b)
        await asyncio.gather(dr._RUNS[a]["task"], dr._RUNS[b]["task"])
        assert dr._RUNS == {}
    asyncio.run(main())


def test_stop_by_run_id_only_cancels_that_match(arena, monkeypatch):
    monkeypatch.setattr(dr, "MAX_CONCURRENT", 2)

    class _Req:
        def __init__(self, q):
            self.query_params = q

    async def main():
        _, p1 = await dr._launch(_body("甲/乙"))
        _, p2 = await dr._launch(_body("丙/丁"))
        a, b = p1["run_id"], p2["run_id"]
        await asyncio.sleep(0)
        resp = await dr.debate_stop(_Req({"run_id": a}))
        import json
        data = json.loads(resp.body)
        assert data["stopped"] == [a]
        # A 被 cancel → 自己的 finally 注销；B 还在
        try:
            await dr._RUNS[a]["task"]
        except (asyncio.CancelledError, KeyError):
            pass
        await asyncio.sleep(0)
        assert a not in dr._RUNS and b in dr._RUNS
        # 不带 run_id = 全停
        resp = await dr.debate_stop(_Req({}))
        assert json.loads(resp.body)["stopped"] == [b]
        try:
            await dr._RUNS[b]["task"]
        except (asyncio.CancelledError, KeyError):
            pass
        await asyncio.sleep(0)
        assert dr._RUNS == {}
        # 空场再停：not running
        resp = await dr.debate_stop(_Req({}))
        assert json.loads(resp.body)["note"] == "not running"
    asyncio.run(main())


def test_queue_drains_into_free_slots(arena, monkeypatch):
    """上限 2：队列里 3 场 → 先开 2 场，放掉一场后第 3 场自动补上。"""
    monkeypatch.setattr(dr, "MAX_CONCURRENT", 2)

    async def main():
        dr._write_queue([
            {"id": "q1", "body": _body("甲/乙")},
            {"id": "q2", "body": _body("丙/丁")},
            {"id": "q3", "body": _body("戊/己")},
        ])
        drain = asyncio.create_task(dr._drain_queue())
        for _ in range(20):
            await asyncio.sleep(0.01)
            if len(dr._RUNS) == 2:
                break
        assert len(dr._RUNS) == 2, "上限 2 应先开两场"
        assert [r["id"] for r in dr._read_queue()] == ["q3"]
        first = next(iter(dr._RUNS))
        arena.release(first)
        for _ in range(60):
            await asyncio.sleep(0.1)
            if first not in dr._RUNS and len(dr._RUNS) == 2:
                break
        assert first not in dr._RUNS and len(dr._RUNS) == 2, "放掉一场后队列第 3 场应自动补位"
        assert dr._read_queue() == []
        for rid in list(dr._RUNS):
            arena.release(rid)
        await asyncio.gather(*[row["task"] for row in dr._RUNS.values()], return_exceptions=True)
        try:
            await asyncio.wait_for(drain, timeout=10)
        except asyncio.TimeoutError:
            drain.cancel()
            pytest.fail("drain 没有在队列清空后退出")
        assert dr._RUNS == {}
    asyncio.run(main())


def test_cli_gate_skips_external_seats(monkeypatch, tmp_path):
    """external 席位不占 _CLI_GATE（干等外部 AI 不烧额度）；CLI 席位占。"""
    from arena import room
    monkeypatch.setattr(room, "INBOX_ROOT", tmp_path)
    gate_hits = []
    real_gate = room._CLI_GATE

    class _Spy:
        def __enter__(self):
            gate_hits.append("in"); return real_gate.__enter__()
        def __exit__(self, *a):
            gate_hits.append("out"); return real_gate.__exit__(*a)
    monkeypatch.setattr(room, "_CLI_GATE", _Spy())
    monkeypatch.setattr(room, "_run_cli_once", lambda *a, **k: "稿")
    # external：到点白卷，不碰闸
    ext = {"engine": "external", "model": "aisay:u", "name": "正方一辩", "run_id": "r", "effort": "-"}
    assert room._run_cli(ext, "s", "p", timeout=5) == ""
    assert gate_hits == []
    # cli：过闸
    cli = {"engine": "claude", "model": "claude-fable-5", "name": "正方二辩", "effort": "high"}
    assert room._run_cli(cli, "s", "p", timeout=60) == "稿"
    assert gate_hits == ["in", "out"]
