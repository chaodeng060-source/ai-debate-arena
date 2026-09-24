#!/usr/bin/env python3
"""本机 MCP 服务：把这个仓的「上场」和「看台」接给任何支持 MCP 的客户端
（Claude Code、Claude Desktop、Cursor 这类），stdio 传输。只能在跟引擎同一台机器上用——
出题走本机投稿箱（见 tools/bridge.py），MCP 客户端跟本机 CLI handler、cmd handler 是同一件
事的另一种接法，不是一条新协议。

工具分两组：
- **上场**：`next_turn` / `submit_turn` —— 复用 tools/bridge.py 的投稿箱扫描
  （`pending_requests`）和回稿落盘（`write_reply`），不新写一套文件协议。
- **看台**：`list_matches` / `read_match` / `vote` / `like` —— 复用 arena/room.py 的
  只读投影（`_load_record` / `_public_record` / `_public_events`）和 arena/audience.py /
  arena/likes.py 已经做好的校验与记录逻辑，不重新实现一遍规则。

**不做任何管理工具**（开赛 / 停赛 / 排队）：这套引擎本来就没有鉴权（见 README「单机用 /
已知限制」），管理动作交给一个「谁连上就能调」的 MCP 服务器，风险跟把管理接口直接摆到公网
一样——这不是这次要补的洞，所以干脆不给。

工具输出只走已经在用的公开投影：不会出现服务器绝对路径、评委是哪家模型、对调票这些内部
字段。

**依赖边界**：下面这些 `_impl` 结尾的普通函数不依赖 `mcp` 包，可以被直接 import、直接测试；
`mcp.server.fastmcp.FastMCP` 只在 `build_server()` 真正要组出一个 MCP 服务实例时才导入——
没装 `mcp` 的环境里，这个文件的其余部分照样能被 import、被测，只是 `build_server()`/`main()`
那条路用不了。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arena import audience as _audience   # noqa: E402
from arena import likes as _likes         # noqa: E402
from arena import room as _room           # noqa: E402
from tools import bridge as _bridge       # noqa: E402

MAX_WAIT_SECONDS = 50   # next_turn 一次最多等这么久，客户端传更大的值也会被夹到这里
MAX_MATCHES = 20        # list_matches 最多给这么多场


# ── 上场：next_turn / submit_turn ────────────────────────────────────────
# 复用 tools/bridge.py 的投稿箱协议——MCP 客户端在这里扮演的角色，跟 bridge.py 的
# cmd handler、stub handler是同一个位置：读 request、写 reply，文件协议一个字不改。

def _turn_summary(req: dict) -> dict:
    """出题投影给 MCP 客户端：只给答题用得上的字段，不带服务器路径。"""
    return {
        "request_id": str(req.get("request_id") or ""),
        "run_id": str(req.get("run_id") or ""),
        "kind": str(req.get("kind") or ""),
        "seat": str(req.get("seat") or ""),
        "system": str(req.get("system") or ""),
        "prompt": str(req.get("prompt") or ""),
        "deadline_epoch": req.get("deadline_epoch"),
    }


def next_turn_impl(agent_id: str = "", run_id: str = "", wait_seconds: float = 30) -> dict:
    """取这个 Agent 最早一条没回的出题（复用 bridge.pending_requests，按 agent_id 过滤；
    不给 agent_id 就不按身份过滤，不给 run_id 就盯整个投稿箱）。最多等 wait_seconds 秒
    （上限 MAX_WAIT_SECONDS）；到点还没有就回 {"pending": false}。"""
    wait = max(0.0, min(float(wait_seconds or 0), MAX_WAIT_SECONDS))
    agent = str(agent_id or "").strip() or None
    run = str(run_id or "").strip() or None
    deadline = time.time() + wait
    while True:
        todo = _bridge.pending_requests(_room.INBOX_ROOT, run, agent_id=agent)
        if todo:
            _req_path, _reply_path, req = todo[0]
            return {"pending": True, **_turn_summary(req)}
        if time.time() >= deadline:
            return {"pending": False}
        time.sleep(min(1.0, max(0.0, deadline - time.time())))


def _find_pending(request_id: str) -> Optional[tuple[Path, Path, dict]]:
    """按 request_id 在投稿箱里找这条还没回的 request。request_id 形如
    "<run_id>:<seq:04d>"（见 arena/prep.py external_request），先切出 run_id 缩小扫描范围。"""
    rid = str(request_id or "").strip()
    if not rid:
        return None
    run_id = rid.split(":", 1)[0] if ":" in rid else ""
    todo = _bridge.pending_requests(_room.INBOX_ROOT, run_id or None)
    for req_path, reply_path, req in todo:
        if str(req.get("request_id") or "") == rid:
            return req_path, reply_path, req
    return None


def submit_turn_impl(request_id: str, text: str) -> dict:
    """交稿：用 bridge.write_reply 写回（协议 v2 信封 + v1 文本投影）。已经回过的（不再出现
    在 pending 列表里）、空文本、找不到的 request_id 都拒绝。"""
    body = str(text or "").strip()
    if not body:
        return {"ok": False, "error": "text is empty"}
    found = _find_pending(request_id)
    if found is None:
        return {"ok": False, "error": "request not found or already answered"}
    _req_path, reply_path, req = found
    _bridge.write_reply(reply_path, body, req)
    return {"ok": True, "request_id": str(req.get("request_id") or ""), "chars": len(body)}


# ── 看台：list_matches / read_match / vote / like ────────────────────────
# 全部复用 arena/room.py、arena/audience.py、arena/likes.py 现成的校验和投影，
# 这里只是把参数搬过去、把结果原样搬回来。

def _match_records() -> list[tuple[Path, dict]]:
    directory = _room.TRANSCRIPT_DIR
    rows: list[tuple[Path, dict]] = []
    if not directory.is_dir():
        return rows
    for path in sorted(directory.glob("debate-*.json")):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("run_id"):
            rows.append((path, data))
    return rows


def list_matches_impl() -> dict:
    """最近 20 场：只给 run_id / 状态 / 辩题 / 开赛时间——不碰 roster/jury，天然不会带出
    评委身份或服务器路径。"""
    rows = _match_records()
    rows.sort(key=lambda item: str(item[1].get("started_at") or item[0].name))
    recent = rows[-MAX_MATCHES:]
    recent.reverse()   # 新的在前
    matches = [
        {"run_id": data.get("run_id"), "status": data.get("status"),
         "topic": data.get("topic"), "started_at": data.get("started_at")}
        for _path, data in recent
    ]
    return {"matches": matches}


def read_match_impl(run_id: str, since: int = -1) -> dict:
    """直接复用 room._public_record 的公开视图——跟 GET /api/debate/{run_id}/record
    是同一份投影，评委模型身份、对调票、服务器路径都已经被挡在外面。"""
    state, err = _room._load_record(run_id)
    if err:
        status, body = err
        return {"error": str(body.get("error") or "error"), "status_code": status}
    return _room._public_record(state, since=int(since))


def vote_impl(run_id: str, voter_id: str, side: str, favorite: str = "", reason: str = "") -> dict:
    """观众投票，voter_kind 固定为 ai——复用 audience.py 的校验（validate_vote）和
    记票（record_vote），run_id/状态加载也是 audience.py 自己那条（跟真正打
    POST /api/debate/{run_id}/vote 时走的是同一段代码）。"""
    vote, err = _audience.validate_vote({
        "voter_id": voter_id, "voter_kind": "ai", "side": side,
        "favorite": favorite or None, "reason": reason or None,
    })
    if err:
        return {"ok": False, "error": err}
    state = _audience._load_state(_room.TRANSCRIPT_DIR, run_id)
    status, payload = _audience.record_vote(_room.TRANSCRIPT_DIR, run_id, state, vote)
    if status != 200:
        return {"ok": False, **payload}
    return {"ok": True, **payload}


def like_impl(run_id: str, voter_id: str, seq: int, liked: bool = True) -> dict:
    """给一段发言/质询/评委插问点赞或取消——复用 likes.py 的校验（record_like）和
    room._load_record/_public_events，跟 POST /api/debate/{run_id}/like 走的是同一段代码。"""
    state, err = _room._load_record(run_id)
    if err:
        _status, body = err
        return {"ok": False, "error": str(body.get("error") or "error")}
    events = _room._public_events(state)
    status, payload = _likes.record_like(
        _room.TRANSCRIPT_DIR, run_id, events,
        {"voter_id": voter_id, "seq": seq, "liked": liked},
    )
    if status != 200:
        return {"ok": False, **payload}
    return {"ok": True, **payload}


# ── FastMCP 组装：只有真的要起 stdio 服务才导入 `mcp` 包 ───────────────────

def build_server():
    """组一个 FastMCP 实例，六个工具全部只是把参数转手给上面的 `_impl` 函数。"""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(
        "ai-debate-arena",
        instructions=(
            "AI 华语辩论赛引擎的本机 MCP 接口，只能在这台机器上用：出题走本机投稿箱，"
            "next_turn/submit_turn 只有跟引擎同机才拿得到题、交得了稿，跨机器要自己搭桥"
            "（参考 tools/bridge.py 的 cmd handler）。没有开赛/停赛/排队这类管理工具——"
            "这套引擎本身没有鉴权，管理动作不适合经一个 MCP 服务器对谁连上都开放。"
        ),
    )

    @server.tool()
    async def next_turn(agent_id: str = "", run_id: str = "", wait_seconds: float = 30) -> dict:
        """取这个 Agent 最早一条没回的出题（按 agent_id 过滤，不给就不过滤；不给 run_id
        就盯整个投稿箱）。最多等 wait_seconds 秒（上限 50）；没有就回 {"pending": false}。
        返回出题的 id、run_id、kind、席位、system、prompt、截止时间（deadline_epoch）。"""
        return await asyncio.to_thread(next_turn_impl, agent_id, run_id, wait_seconds)

    @server.tool()
    def submit_turn(request_id: str, text: str) -> dict:
        """交稿：用 next_turn 拿到的 request_id 写回文本。已经回过的、空文本、找不到的
        request_id 都会被拒绝（返回 {"ok": false, "error": ...}）。"""
        return submit_turn_impl(request_id, text)

    @server.tool()
    def list_matches() -> dict:
        """最近 20 场比赛：run_id / 状态 / 辩题 / 开赛时间。"""
        return list_matches_impl()

    @server.tool()
    def read_match(run_id: str, since: int = -1) -> dict:
        """看一场的公开赛录：辩题/阵容/赛程 + 按顺序的发言/质询/评委插问/裁决票，跟
        GET /api/debate/{run_id}/record 同一份投影。since 给上次拿到的 next_seq 只拉增量，
        不给（默认 -1）就是整场。"""
        return read_match_impl(run_id, since)

    @server.tool()
    def vote(run_id: str, voter_id: str, side: str, favorite: str = "", reason: str = "") -> dict:
        """观众投票（voter_kind 固定为 ai）。side 必须是 pro/con；favorite 可选，必须是这场
        的席位名；reason 可选、≤100 字。"""
        return vote_impl(run_id, voter_id, side, favorite, reason)

    @server.tool()
    def like(run_id: str, voter_id: str, seq: int, liked: bool = True) -> dict:
        """给一段发言/质询/评委插问点赞；liked=false 取消。seq 必须是这场公开事件流里
        真实存在的发言/质询/插问（评审票不能点赞）。"""
        return like_impl(run_id, voter_id, seq, liked)

    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
