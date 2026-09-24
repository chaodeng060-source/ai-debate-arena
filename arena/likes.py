"""点赞：给发言、质询、评委插问点赞，观众票之外一个更轻的信号。

跟观众票（arena/audience.py）摆在一起比较：投票是「盲投、一人一票、结构化、进榜」，
点赞不是——点赞没有盲投窗口（赛中赛后都能点）、不进任何榜、就是一个 ♡ 计数器，
看观众此刻在为哪一段鼓掌。

一人对同一段只算一次：liked=true 幂等（重复点没有第二次），liked=false 取消。
voter_id 规则跟投票一样（非空、≤120 字，自报，没有平台身份做后盾——见 README
「已知限制」，点赞编号一样能刷）。

`seq` 必须是这场公开事件流里真实存在的发言、质询或评委插问——复用
`arena.room._public_events` 的口径，评审票（type=jury）不能点赞。

存储：$DEBATE_DATA_DIR/likes/<run_id>.json，原子写；同一 run_id 的读改写用
进程内锁串行——落盘前那半步（读旧值、改、写回）不是原子的，两个请求前后脚到
不加锁会互相踩掉对方那一票。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

VOTER_ID_MAX = 120
# 能点赞的事件类型：发言、质询、评委插问——评审票（type=jury）不能点赞，
# 跟 arena/room.py `_public_events` 的 type 枚举一致。
LIKEABLE_TYPES = {"speech", "crossfire", "bench"}


def likes_dir(transcript_dir: Path) -> Path:
    return transcript_dir / "likes"


def likes_path(transcript_dir: Path, run_id: str) -> Path:
    return likes_dir(transcript_dir) / f"{run_id}.json"


# 每个 run_id 一把锁：同一场的点赞请求互相排队，不同场互不阻塞。
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(run_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(run_id)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[run_id] = lock
        return lock


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
        if isinstance(data, dict) and isinstance(data.get("likes"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"likes": {}}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def likeable_seqs(events: list[dict]) -> set[int]:
    """发言、质询、评委插问的 seq 集合；评审票（type=jury）不在其中。"""
    return {int(e["seq"]) for e in events if e.get("type") in LIKEABLE_TYPES}


def validate_like(body: dict) -> tuple[Optional[dict], Optional[str]]:
    voter_id = str(body.get("voter_id") or "").strip()
    if not voter_id or len(voter_id) > VOTER_ID_MAX:
        return None, "voter_id required (≤120 chars)"
    if "seq" not in body:
        return None, "seq required"
    try:
        seq = int(body.get("seq"))
    except (TypeError, ValueError):
        return None, "seq must be an integer"
    liked = body.get("liked")
    liked = True if liked is None else bool(liked)
    return {"voter_id": voter_id, "seq": seq, "liked": liked}, None


def record_like(transcript_dir: Path, run_id: str, events: list[dict], body: dict) -> tuple[int, dict]:
    """点一次赞或取消。events 是这场的公开事件流（调用方用 room._public_events 现算，
    避免这个模块直接 import room 造成循环导入）。返回 (http 状态码, 响应体)。"""
    like, err = validate_like(body)
    if err:
        return 400, {"error": err}
    if like["seq"] not in likeable_seqs(events):
        return 400, {"error": "seq is not a likeable event in this match"}
    path = likes_path(transcript_dir, run_id)
    seq_key = str(like["seq"])
    with _lock_for(run_id):
        data = _read(path)
        data.setdefault("run_id", run_id)
        bucket = data["likes"].setdefault(seq_key, {})
        if like["liked"]:
            bucket[like["voter_id"]] = True
        else:
            bucket.pop(like["voter_id"], None)
            if not bucket:
                data["likes"].pop(seq_key, None)
        _write(path, data)
        count = len(data["likes"].get(seq_key, {}))
    return 200, {"ok": True, "run_id": run_id, "seq": like["seq"], "liked": like["liked"], "count": count}


def public_likes(transcript_dir: Path, run_id: str, voter_id: str = "") -> dict:
    """{"counts": {seq: 数}, "mine": [voter_id 点过的 seq, ...]}。voter_id 不给就 mine 是空列表。"""
    data = _read(likes_path(transcript_dir, run_id))
    counts = {int(seq_key): len(bucket) for seq_key, bucket in data["likes"].items() if bucket}
    mine = sorted(int(seq_key) for seq_key, bucket in data["likes"].items() if voter_id and voter_id in bucket)
    return {"counts": counts, "mine": mine}


# ── 路由：挂在 room.router 下（同 audience.py 的 /vote /votes 那样）───────────
router = APIRouter()


def _deps():
    from arena import room as dr
    return dr


@router.post("/api/debate/{run_id}/like")
async def debate_like(run_id: str, req: Request):
    """给一段发言/质询/评委插问点赞或取消。body: {voter_id, seq, liked?: true}。
    liked 默认 true；false 取消。赛中赛后都能点——不像投票那样有盲投窗口。"""
    dr = _deps()
    state, err = dr._load_record(run_id)
    if err:
        status, body = err
        return JSONResponse(body, status_code=status)
    payload_in = await req.json()
    events = dr._public_events(state)
    status, payload = record_like(dr.TRANSCRIPT_DIR, run_id, events,
                                  payload_in if isinstance(payload_in, dict) else {})
    return JSONResponse(payload, status_code=status)


@router.get("/api/debate/{run_id}/likes")
async def debate_likes(run_id: str, req: Request):
    """{"counts": {seq: 数}, "mine": [...]}（?voter_id= 才会给 mine）。"""
    dr = _deps()
    state, err = dr._load_record(run_id)
    if err:
        status, body = err
        return JSONResponse(body, status_code=status)
    payload = public_likes(dr.TRANSCRIPT_DIR, run_id, (req.query_params.get("voter_id") or "").strip())
    return JSONResponse(payload)
