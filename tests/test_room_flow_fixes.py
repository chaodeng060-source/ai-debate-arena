"""宣传前体检修复的回归测试（单机流程向，B 编号对应 /tmp/debate-arena-audit-20260924/REPORT.md）。

只测流程正确性：一方缺席不该拖死全场、重启不该认错回稿、同队撞名不该悄悄打残、
队列里的坏参数不该卡死后面的比赛、本地路径不该发给够不着它的外部 AI。不碰真模型、不连外网。
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading

import pytest

from arena import room


# ── B6：辩手资料索引 vs 同题回避，以前读两个不同目录 ──────────────────

def test_scout_index_and_precedent_exclusion_read_same_directory(monkeypatch, tmp_path):
    """以前 _reference_paths 读 TRANSCRIPT_DIR/reference，_same_topic_reference_paths
    读 REFERENCE_DIR——同一份往届稿放在两处时，索引那边看到了却挂不上回避名单，
    同题答案照样进了辩手手里。现在两边统一读 REFERENCE_DIR。"""
    ref_dir = tmp_path / "reference"
    ref_dir.mkdir()
    other_dir = tmp_path / "elsewhere" / "reference"
    other_dir.mkdir(parents=True)
    monkeypatch.setattr(room, "REFERENCE_DIR", ref_dir)
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path / "elsewhere")

    same = "## 辩题：时间赋予生命意义/生命赋予时间意义\n# 评委\n往届判词\n"
    (other_dir / "same-topic-in-data-dir.md").write_text(same, encoding="utf-8")
    (ref_dir / "same-topic-in-reference-dir.md").write_text(same, encoding="utf-8")

    topic = "时间与生命，谁赋予谁意义（正方：时间赋予生命意义；反方：生命赋予时间意义）"
    kept, excluded = room._reference_paths(topic)
    assert kept == [], f"同题往届稿不该进辩手索引，实际 kept={kept}"
    assert len(excluded) == 1 and "same-topic-in-reference-dir.md" in excluded[0]
    assert room._precedent_verdict_text(topic), "评审席该看到的同题判词还在"


# ── B15：备赛题发给外部 AI 时带服务器本地绝对路径 ──────────────────

def test_external_scout_prompt_does_not_leak_local_absolute_path(monkeypatch, tmp_path):
    """外部 AI 没有这台服务器的文件系统访问权限，reference_paths 里的本地绝对路径对它没用，
    只是白白泄漏目录结构。本机引擎（有 Read 工具能真打开文件）该拿到能用的真路径。"""
    ref_dir = tmp_path / "reference"
    ref_dir.mkdir()
    # 不带「## 辩题：」头，_same_topic_reference_paths 抓不到标题行，不会被同题回避（B6）
    # 挡在索引之外——这条测试要的是「真的进了索引」的那份材料会不会带上服务器本地路径。
    (ref_dir / "some-precedent.md").write_text("这是一份往届参考资料，与本场辩题无关。\n", encoding="utf-8")
    monkeypatch.setattr(room, "REFERENCE_DIR", ref_dir)
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)

    seen_prompts: dict[str, str] = {}

    def fake_cli(d, system, prompt, timeout, *, research_tools=False, kind="speech",
                 request_context=None):
        seen_prompts[d["label"]] = prompt
        return "{}"

    async def fake_emit(*a, **k):
        return "msg"

    monkeypatch.setattr(room, "_run_cli", fake_cli)
    monkeypatch.setattr(room, "_emit_to_room", fake_emit)

    roster = [
        {"name": "正方一辩", "side": "pro", "seat": 1, "label": "外部甲", "engine": "external",
         "model": "aisay:u1", "effort": "-", "owner": "o1"},
        {"name": "反方一辩", "side": "con", "seat": 1, "label": "本机乙", "engine": "claude",
         "model": "claude-fable-5", "effort": "max"},
    ]
    asyncio.run(room._run_prep("随便一道题", "正方立场", "反方立场", roster, fmt="mini", timeout=30))

    ext_prompt = seen_prompts["外部甲"]
    local_prompt = seen_prompts["本机乙"]
    assert str(tmp_path) not in ext_prompt, "外部席位的备赛题里不该出现服务器本地绝对路径"
    assert "some-precedent.md" in ext_prompt, "文件名还是要给，外部 AI 知道有这份材料存在"
    assert str(ref_dir / "some-precedent.md") in local_prompt, "本机引擎该拿到能被 Read 工具打开的真路径"


# ── B16：/api/debate/status、/api/debate/queue 返回赛录文件绝对路径 ──────────

def test_running_snapshot_exposes_filename_not_absolute_path(monkeypatch, tmp_path):
    """out_path 以前序列化成完整绝对路径，把服务器目录结构透给打得到这两个只读端点的任何人。
    现在只给文件名——run_id 已经够定位这份赛录了。"""
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)
    room._RUNS.clear()
    try:
        room._register_run("debate-xyz", out_path=tmp_path / "debate-xyz.json")
        snap = room._running_snapshot()
        assert len(snap) == 1
        assert snap[0]["run_id"] == "debate-xyz"
        assert snap[0]["out_path"] == "debate-xyz.json"
        assert "/" not in snap[0]["out_path"] and str(tmp_path) not in snap[0]["out_path"]
    finally:
        room._RUNS.clear()


# ── B11：选手榜接口不认 DEBATE_DATA_DIR ──────────────────────────────

def test_board_endpoint_uses_configured_transcript_dir(monkeypatch, tmp_path):
    """/api/debate/board 以前调 load_records() 不传目录，用的是 tools/board.py 自己仓库相对的
    默认值，不认真正落盘赛录的 TRANSCRIPT_DIR——换了部署目录榜就是空的。现在传 TRANSCRIPT_DIR。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)
    roster = [{"name": "正方一辩", "side": "pro", "seat": 1, "model": "alpha"},
              {"name": "反方一辩", "side": "con", "seat": 1, "model": "beta"}]
    record = {"run_id": "r1", "status": "completed", "roster": roster,
              "jury": {"status": "decided", "winner": "pro", "ballots": [{"valid": True}]}}
    (tmp_path / "debate-r1.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    app = FastAPI()
    app.include_router(room.router)
    c = TestClient(app)
    resp = c.get("/api/debate/board")
    assert resp.status_code == 200
    body = resp.json()
    assert body["matches"] == 1, body
    table = {row["key"]: row for row in body["table"]}
    assert table["alpha"]["played"] == 1 and table["alpha"]["won"] == 1


# ── B3：重启后外部席位把旧回稿当新回稿 ──────────────────────────────

def test_external_seq_does_not_reuse_stale_reply_after_restart(monkeypatch, tmp_path):
    """_EXTERNAL_SEQ 只在内存，进程重启后从 1 重数，会撞上重启前同名的旧回稿文件、
    0 秒读到陈旧内容。现在重启后先扫投稿箱已有编号，序号接着数、不撞旧文件名。"""
    monkeypatch.setattr(room, "INBOX_ROOT", tmp_path)
    run_id = "restart-sim"
    folder = tmp_path / run_id
    folder.mkdir(parents=True)
    (folder / "0001-正方一辩.request.json").write_text("{}", encoding="utf-8")
    (folder / "0001-正方一辩.reply.txt").write_text(
        '{"preferred_role": "opening"}  <- 重启前这一席的备赛回稿', encoding="utf-8",
    )
    room._EXTERNAL_SEQ.clear()   # 模拟进程重启：内存里的序号计数器归零

    seat = {"engine": "external", "run_id": run_id, "name": "正方一辩"}
    got = room._external_speak(seat, "SYSTEM", "现在轮到你：正方一辩·立论", 1, kind="speech")

    assert got == "", f"不该读到重启前的陈旧回稿，实际拿到：{got!r}"
    new_reqs = sorted(p.name for p in folder.glob("*.request.json"))
    assert new_reqs == ["0001-正方一辩.request.json", "0002-正方一辩.request.json"], new_reqs
    new_request = json.loads((folder / "0002-正方一辩.request.json").read_text("utf-8"))
    assert new_request["kind"] == "speech"


# ── B17：出题编号自增没加锁 ──────────────────────────────────────────

def test_external_seq_assignment_is_thread_safe_under_concurrent_calls(monkeypatch, tmp_path):
    """读现值、加一、写回不是原子操作；备赛阶段多个外部席位的收集轮跑在不同线程里
    （asyncio.to_thread），不加锁时两个线程可能抢到同一个编号、出题文件互相覆盖。"""
    monkeypatch.setattr(room, "INBOX_ROOT", tmp_path)
    monkeypatch.setattr(room, "_EXTERNAL_SEQ", {})
    run_id = "concurrent-seq"
    n = 40
    seen: list[int] = []
    lock = threading.Lock()

    def worker():
        seq = room._next_external_seq(run_id)
        with lock:
            seen.append(seq)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(seen) == list(range(1, n + 1)), sorted(seen)


# ── B2：质询里一方缺答，整场直接判失败 ──────────────────────────────

def test_crossfire_records_unanswered_instead_of_failing_whole_match(monkeypatch):
    """以前质询问或答缺一次就 break、外层拿 len(exchanges) != rounds 直接 RuntimeError
    整场判失败。现在缺答记「未作答」、接着打完剩下的轮次，转录里留痕。"""
    asker = {"name": "正方一辩", "side": "pro", "seat": 1, "label": "正方一辩"}
    answerer = {"name": "反方一辩", "side": "con", "seat": 1, "label": "反方一辩"}

    def fake_cli(d, system, prompt, timeout, *, kind="speech"):
        if d["name"] == answerer["name"] and kind == "crossfire_a":
            return ""   # 被质询方到点没回
        return f"{d['name']}-{kind}"

    async def fake_emit(body, *, title, **k):
        return "msg"

    monkeypatch.setattr(room, "_run_cli", fake_cli)
    monkeypatch.setattr(room, "_emit_to_room", fake_emit)

    exchanges = asyncio.run(room._run_crossfire(
        asker, answerer, "甲/乙", "甲", "乙", "zh", [], 2, 5,
    ))
    assert len(exchanges) == 2, "缺答也要凑够 rounds 轮，不能提前中断"
    assert all(ex["a"] for ex in exchanges), "未作答也要留一句人话痕迹，不是空字符串"
    assert all(ex.get("unanswered") for ex in exchanges), exchanges
    assert all(ex["q"] for ex in exchanges)


# ── B5：同队两席 label 一样时备赛整段悄悄跳过 ──────────────────────────

def test_launch_rejects_pool_with_duplicate_label(monkeypatch, tmp_path):
    """同队两席 label 撞名时，_draw_roster 抽签后那一队的备赛整段悄悄跳过
    （_run_prep 里 len(labels) < 2 直接 continue，队伍拿不到任何战术板）。
    现在开赛请求里只要 pool 有重复 label 就直接拒，不等抽完签才发现打残了。"""
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)
    monkeypatch.setattr(room, "QUEUE_PATH", tmp_path / "queue.json")
    room._RUNS.clear()
    pool = [
        {"engine": "external", "model": "aisay:u1", "effort": "-", "label": "Claude"},
        {"engine": "external", "model": "aisay:u2", "effort": "-", "label": "Claude"},
        {"engine": "external", "model": "aisay:u3", "effort": "-", "label": "GPT"},
        {"engine": "external", "model": "aisay:u4", "effort": "-", "label": "GPT"},
    ]
    body = {"topic": "甲/乙", "format": "mini", "pool": pool, "prep": False, "bench": False, "draw": True}

    async def main():
        status, payload = await room._launch(dict(body))
        assert status == 400 and "label" in payload["error"], payload
        assert room._RUNS == {}, "拒绝的请求不能占用并发名额"
    asyncio.run(main())
    room._RUNS.clear()


def test_queue_add_rejects_pool_with_duplicate_label(monkeypatch, tmp_path):
    """入队一样要挡：不能只在 start 端点挡，排队进去、真出队时才炸。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setattr(room, "QUEUE_PATH", tmp_path / "queue.json")
    pool = [
        {"engine": "external", "model": "aisay:u1", "effort": "-", "label": "Claude"},
        {"engine": "external", "model": "aisay:u2", "effort": "-", "label": "Claude"},
        {"engine": "external", "model": "aisay:u3", "effort": "-", "label": "GPT"},
        {"engine": "external", "model": "aisay:u4", "effort": "-", "label": "GPT"},
    ]
    app = FastAPI()
    app.include_router(room.router)
    c = TestClient(app)
    r = c.post("/api/debate/queue", json={"pool": pool, "label": "坏的一场", "prep": False, "bench": False})
    assert r.status_code == 400 and "label" in r.json()["error"], r.json()
    assert room._read_queue() == [], "拒绝的请求不能进队列"


# ── B8：队列里参数坏的比赛卡死整个队列，直接 start 返回 500 ──────────────

def test_queue_add_rejects_bad_timeout_before_it_ever_queues(monkeypatch, tmp_path):
    """以前入队只查 pool/judge_pool，timeout 这种坏参数照单全收进队列，出队时 _launch 里
    int('abc') 直接 ValueError，坏项卡在队首、后面的比赛永远开不了。现在入队就用跟 _launch
    一样的校验，当场 400，根本进不了队列。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setattr(room, "QUEUE_PATH", tmp_path / "queue.json")
    app = FastAPI()
    app.include_router(room.router)
    c = TestClient(app)
    r = c.post("/api/debate/queue", json={"timeout": "abc", "prep": False, "label": "坏的一场"})
    assert r.status_code == 400, r.json()
    assert room._read_queue() == []


def test_launch_rejects_bad_timeout_with_400_not_uncaught_exception(tmp_path, monkeypatch):
    """直接 /api/debate/start 传坏 timeout：以前 _launch 里裸 int('abc') 炸 ValueError，
    FastAPI 没抓、前端看到的是不带错误信息的 500。现在 _launch 自己接住，回清楚的 400。"""
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)
    room._RUNS.clear()

    async def main():
        status, payload = await room._launch({"timeout": "abc", "topic": "甲/乙"})
        assert status == 400 and "timeout" in payload["error"], payload
    asyncio.run(main())


def test_launch_rejects_non_dict_body(tmp_path, monkeypatch):
    """POST /api/debate/start 传一个 JSON 数组：以前 body.get(...) 直接 AttributeError → 裸 500。"""
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)
    room._RUNS.clear()

    async def main():
        status, payload = await room._launch(["not", "an", "object"])
        assert status == 400, payload
    asyncio.run(main())


def test_drain_queue_survives_unexpected_launch_crash_and_continues(monkeypatch, tmp_path):
    """就算校验都通过了，_launch 因为别的原因意外崩溃，出队循环也不能被带死——
    坏项照样弹出、下一场照常开（B8 第二层防线：try/except 包住 _launch 调用）。"""
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)
    monkeypatch.setattr(room, "QUEUE_PATH", tmp_path / "queue.json")
    monkeypatch.setattr(room, "_QUEUE_PACE_SECONDS", 0.01)
    monkeypatch.setattr(room, "MAX_CONCURRENT", 1)
    room._RUNS.clear()

    async def fake_run_match(*args, run_id=None, **kwargs):
        room._CUR_RUN.set(run_id)
        room._register_run(run_id, out_path=room.TRANSCRIPT_DIR / f"{run_id}.json")

    async def no_emit(*a, **k):
        return "x"

    monkeypatch.setattr(room, "_run_match", fake_run_match)
    monkeypatch.setattr(room, "_emit_to_room", no_emit)
    monkeypatch.setattr(room, "_finish_interrupted_record", lambda *a, **k: None)

    calls = {"n": 0}
    real_register = room._register_run

    def flaky_register(run_id, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash after validation")
        return real_register(run_id, *a, **kw)
    monkeypatch.setattr(room, "_register_run", flaky_register)

    room._write_queue([
        {"id": "bad", "body": {"topic": "甲/乙", "format": "mini", "prep": False, "bench": False, "draw": False}},
        {"id": "good", "body": {"topic": "丙/丁", "format": "mini", "prep": False, "bench": False, "draw": False}},
    ])

    async def main():
        await asyncio.wait_for(room._drain_queue(), timeout=10)
        assert room._read_queue() == [], "崩溃的一项也要出队，不能卡死后面的"
    asyncio.run(main())
    room._RUNS.clear()


# ── 移植自原先的实现：防同一场被并发 resume 两次写坏 state ──────────────

def _resume_state(run_id: str) -> dict:
    return {
        "schema_version": 2, "run_id": run_id, "status": "failed", "phase": "match",
        "topic": "甲/乙", "pro_side": "甲", "con_side": "乙", "format": "mini", "lang": "zh",
        "crossfire_rounds": 1, "prep_enabled": False,
        "roster": [dict(row) for row in room.ROSTER_MINI],
        "transcript": [], "crossfire": [], "jury": None,
    }


def test_same_checkpoint_has_exactly_one_resume_owner(monkeypatch, tmp_path):
    """同一份赛录 checkpoint 不能被两个 resume 同时推进，否则会重复叫辩手、
    两边互相覆盖赛录状态。"""
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)
    out = tmp_path / "same-match.json"
    out.write_text(json.dumps(_resume_state("same-match"), ensure_ascii=False), encoding="utf-8")
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fake_schedule(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("第二个推进者进了赛程")
        entered.set()
        await release.wait()

    monkeypatch.setattr(room, "_run_schedule", fake_schedule)

    async def main():
        first = asyncio.create_task(room.resume_match(out, timeout=30))
        await entered.wait()
        try:
            with pytest.raises(room.DebateOwnershipError, match="already has an active owner"):
                await room.resume_match(out, timeout=30)
        finally:
            release.set()
            await first

    asyncio.run(main())
    assert calls == 1
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert len(saved["resume_receipts"]) == 1
    assert saved["resume_receipts"][0]["from_status"] == "failed"


def test_resume_owner_is_released_after_failure(monkeypatch, tmp_path):
    """一次 resume 失败不能留下死锁的所有权；下一次显式 resume 仍能正常接手。"""
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)
    out = tmp_path / "retry-match.json"
    out.write_text(json.dumps(_resume_state("retry-match"), ensure_ascii=False), encoding="utf-8")
    calls = 0

    async def fake_schedule(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(room, "_run_schedule", fake_schedule)

    async def main():
        with pytest.raises(RuntimeError, match="provider unavailable"):
            await room.resume_match(out, timeout=30)
        await room.resume_match(out, timeout=30)

    asyncio.run(main())
    assert calls == 2
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert len(saved["resume_receipts"]) == 2


def test_live_match_blocks_resume_of_its_checkpoint(monkeypatch, tmp_path):
    """原始赛程还在跑的时候，外部 resume 命令不能成为第二个写者
    （比如运维手滑对一份还在直播的赛录跑了 tools/resume.py）。"""
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)
    run_id = "live-match"
    out = tmp_path / f"{run_id}.json"
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fake_match(*_args, **_kwargs):
        out.write_text(json.dumps(_resume_state(run_id), ensure_ascii=False), encoding="utf-8")
        entered.set()
        await release.wait()

    monkeypatch.setattr(room, "_run_match", fake_match)
    room._RUNS.clear()

    async def main():
        room._register_run(run_id)
        live = asyncio.create_task(room._run_match_guarded(run_id=run_id))
        await entered.wait()
        try:
            with pytest.raises(room.DebateOwnershipError, match="already has an active owner"):
                await room.resume_match(out, timeout=30)
        finally:
            release.set()
            await live

    asyncio.run(main())
    assert room._RUNS == {}


def test_checkpoint_owner_lock_crosses_process_boundary(tmp_path):
    """CLI 手动 resume 和服务进程分属两个 PID 时，也只能有一个推进者——进程内的 set
    挡不了跨进程，靠 Linux flock。"""
    out = tmp_path / "cross-process.json"
    lock_dir = tmp_path / ".locks"
    lock_dir.mkdir()
    lock_path = lock_dir / f"{out.name}.lock"
    child = subprocess.Popen(
        [
            sys.executable, "-c",
            (
                "import fcntl,sys; "
                "f=open(sys.argv[1], 'a+'); "
                "fcntl.flock(f.fileno(), fcntl.LOCK_EX); "
                "print('locked', flush=True); "
                "sys.stdin.read(1)"
            ),
            str(lock_path),
        ],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "locked"
    try:
        with pytest.raises(room.DebateOwnershipError, match="already has an active owner"):
            with room._claim_match_owner(out):
                pytest.fail("跨进程文件锁未生效")
    finally:
        assert child.stdin is not None
        child.stdin.write("x")
        child.stdin.flush()
        assert child.wait(timeout=5) == 0


# ── 文档字符串：独立版仓库没有 server.py ──────────────────────────────

def test_queue_startup_docstring_is_host_neutral():
    """独立版仓库没有 server.py，文档字符串不该点名主项目那份 server.py 的 lifespan。"""
    doc = room.debate_queue_startup.__doc__ or ""
    assert "server.py" not in doc
