"""点赞：给发言/质询/评委插问点赞，观众票之外一个更轻的信号。
纯函数测点赞/取消/幂等/seq 校验/并发不丢票；路由测接线（run_id 400/404、counts 与 mine 对得上、
赛后照样能点——跟投票的盲投窗口不一样）。
"""
from __future__ import annotations

import json
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena import likes as li
from arena import room


EVENTS = [
    {"seq": 0, "type": "speech"},
    {"seq": 1, "type": "speech"},
    {"seq": 2, "type": "crossfire"},
    {"seq": 3, "type": "bench"},
    {"seq": 4, "type": "jury"},   # 评审票不能点赞
]


# ── 纯函数：校验 / 点赞 / 取消 / 幂等 ──────────────────────────────────────

def test_likeable_seqs_excludes_jury():
    assert li.likeable_seqs(EVENTS) == {0, 1, 2, 3}


def test_validate_like_checks_voter_id_and_seq():
    ok, err = li.validate_like({"voter_id": "v1", "seq": 0})
    assert err is None and ok == {"voter_id": "v1", "seq": 0, "liked": True}
    assert li.validate_like({"voter_id": "", "seq": 0})[1]
    assert li.validate_like({"voter_id": "x" * 121, "seq": 0})[1]
    assert li.validate_like({"voter_id": "v1"})[1]                 # 缺 seq
    assert li.validate_like({"voter_id": "v1", "seq": "abc"})[1]   # seq 不是整数
    ok, _ = li.validate_like({"voter_id": " v1 ", "seq": "2", "liked": False})
    assert ok == {"voter_id": "v1", "seq": 2, "liked": False}      # seq 接受数字字符串、liked 可显式 false


def test_like_unlike_idempotent_and_seq_must_exist(tmp_path):
    # seq 是这场真实存在但不可点赞的类型（评审票）→ 400
    code, p = li.record_like(tmp_path, "debate-x", EVENTS, {"voter_id": "v1", "seq": 4})
    assert code == 400 and "seq" in p["error"]
    # seq 压根不在事件流里 → 400
    code, p = li.record_like(tmp_path, "debate-x", EVENTS, {"voter_id": "v1", "seq": 99})
    assert code == 400

    # 正常点赞
    code, p = li.record_like(tmp_path, "debate-x", EVENTS, {"voter_id": "v1", "seq": 0})
    assert code == 200 and p == {"ok": True, "run_id": "debate-x", "seq": 0, "liked": True, "count": 1}
    # 幂等：同一人重复点，计数不变
    code, p = li.record_like(tmp_path, "debate-x", EVENTS, {"voter_id": "v1", "seq": 0})
    assert code == 200 and p["count"] == 1
    # 另一人点同一段 → 2
    code, p = li.record_like(tmp_path, "debate-x", EVENTS, {"voter_id": "v2", "seq": 0})
    assert code == 200 and p["count"] == 2
    # 取消
    code, p = li.record_like(tmp_path, "debate-x", EVENTS, {"voter_id": "v1", "seq": 0, "liked": False})
    assert code == 200 and p["liked"] is False and p["count"] == 1
    # 取消一个没点过的：幂等，不报错、不变负数
    code, p = li.record_like(tmp_path, "debate-x", EVENTS, {"voter_id": "v9", "seq": 0, "liked": False})
    assert code == 200 and p["count"] == 1

    counts = li.public_likes(tmp_path, "debate-x")
    assert counts["counts"] == {0: 1}
    mine_v2 = li.public_likes(tmp_path, "debate-x", voter_id="v2")
    assert mine_v2["mine"] == [0]
    mine_v1 = li.public_likes(tmp_path, "debate-x", voter_id="v1")
    assert mine_v1["mine"] == [], "v1 已经取消了，不该再出现在 mine 里"

    # 落盘格式：原子写出来的就是 {"run_id":..., "likes": {"0": {"v2": true}}}
    data = json.loads((tmp_path / "likes" / "debate-x.json").read_text("utf-8"))
    assert data["run_id"] == "debate-x"
    assert data["likes"] == {"0": {"v2": True}}


def test_concurrent_likes_do_not_lose_votes(tmp_path):
    """40 个不同 voter 同时给同一 seq 点赞：加了锁的读改写不应该互相踩掉对方那一票。"""
    n = 40

    def hit(i):
        li.record_like(tmp_path, "debate-conc", EVENTS, {"voter_id": f"v{i}", "seq": 1, "liked": True})

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    result = li.public_likes(tmp_path, "debate-conc")
    assert result["counts"] == {1: n}, result


# ── 路由：挂在 room.router 下（同 audience.py 的 /vote /votes 那样）───────────

def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)
    app = FastAPI()
    app.include_router(room.router)
    return TestClient(app)


def _write_state(tmp_path, run_id="debate-y", **overrides):
    state = {
        "run_id": run_id, "status": "running", "phase": "match", "format": "mini", "lang": "zh",
        "topic": "T/T'", "pro_side": "T", "con_side": "T'", "roster": [], "schedule": [],
        "transcript": [
            {"speaker": "正方一辩", "side": "pro", "stage": "立论", "text": "正文",
             "schedule_index": 0, "chars": 2, "limit": 100, "truncated": False,
             "elapsed_sec": 1.0, "defection_hits": [], "msg_id": "m1"},
        ],
        "crossfire": [], "bench": [], "jury": None,
    }
    state.update(overrides)
    (tmp_path / f"{run_id}.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return state


def test_like_route_validates_run_id_voter_id_and_seq(tmp_path, monkeypatch):
    _write_state(tmp_path)
    c = _client(tmp_path, monkeypatch)

    # run_id 格式不对 → 400（跟 /record 一致，两者都走 room._load_record）
    bad = c.post("/api/debate/debate%20test/like", json={"voter_id": "v1", "seq": 0})
    assert bad.status_code == 400 and bad.json() == {"error": "invalid run_id"}

    # 没这场 → 404
    missing = c.post("/api/debate/debate-nope/like", json={"voter_id": "v1", "seq": 0})
    assert missing.status_code == 404 and missing.json() == {"error": "no such match"}

    # voter_id 非法 → 400
    bad_voter = c.post("/api/debate/debate-y/like", json={"voter_id": "", "seq": 0})
    assert bad_voter.status_code == 400

    # seq 不是这场真实事件 → 400
    bad_seq = c.post("/api/debate/debate-y/like", json={"voter_id": "v1", "seq": 99})
    assert bad_seq.status_code == 400

    # 正常点赞
    ok = c.post("/api/debate/debate-y/like", json={"voter_id": "v1", "seq": 0})
    assert ok.status_code == 200 and ok.json()["count"] == 1

    # GET /likes：counts 和 mine 对得上
    got = c.get("/api/debate/debate-y/likes?voter_id=v1")
    assert got.status_code == 200 and got.json() == {"counts": {"0": 1}, "mine": [0]}
    # 换个没点过的人看，mine 是空
    other = c.get("/api/debate/debate-y/likes?voter_id=someone-else")
    assert other.json()["mine"] == []

    # GET 也走一样的 run_id 校验
    assert c.get("/api/debate/debate-nope/likes").status_code == 404
    assert c.get("/api/debate/debate%20test/likes").status_code == 400


def test_like_works_after_match_closed_unlike_voting(tmp_path, monkeypatch):
    """赛后（phase=done/status=completed）投票会被拒（409），点赞不受这个窗口约束。"""
    _write_state(tmp_path, status="completed", phase="done")
    c = _client(tmp_path, monkeypatch)

    like_resp = c.post("/api/debate/debate-y/like", json={"voter_id": "v1", "seq": 0})
    assert like_resp.status_code == 200, like_resp.text

    vote_resp = c.post("/api/debate/debate-y/vote", json={"voter_id": "v1", "side": "pro"})
    assert vote_resp.status_code == 409, "投票该被关票窗口拒掉，跟点赞不共用规则"
