#!/usr/bin/env python3
"""辩论场「桥」：把引擎写进投稿箱的出题送到外部 AI 手里、把回稿写回来。

    .venv/bin/python tools/bridge.py --run-id <run_id> --handler stub     # 验收：本地代填（零额度）
    .venv/bin/python tools/bridge.py --all --handler stub                  # 盯整个投稿箱
    .venv/bin/python tools/bridge.py --run-id <run_id> --handler aisay    # 正式：aisay 城里的 AI（等口子）

引擎侧只认一个文件协议（arena/prep.py「外部 AI 席位」一节）：
    data/debates/inbox/<run_id>/<seq:04d>-<seat>.request.json   引擎写：{kind, seat, system, prompt, deadline_epoch, …}
    data/debates/inbox/<run_id>/<seq:04d>-<seat>.reply.json     桥写：v2 身份绑定回执
    data/debates/inbox/<run_id>/<seq:04d>-<seat>.reply.txt      桥写：v1 兼容正文投影
到 deadline 没 reply = 白卷，引擎不重试不代写。桥是独立进程，引擎一行不改就能换桥。

handler 就是「把一条 request 变成回复正文」的那一段，按 request.kind 分发（EXTERNAL_KINDS）：
- stub  ：本地代填。只为验引擎流程（开赛→发言→质询→插问→评委票→观众票→榜）没坏，
          稿是模板、票是按格式填的——**这不是辩论质量验收，不许拿它的胜负/分数当成绩**。
          零额度验收：全 external 席位 + 桥代填，不起任何真模型。
- aisay：正式桥。对接形态（内建辩论桌 / 唤醒面板一行）由 aisay 侧定
          方向决定：走 aisay、可多房间、7/11 席没问题；蛋壳在修理铺给的意见也一并采纳。
          口子清单到了在 AisayHandler 里落地，文件协议不动。——致谢蛋宝、蛋壳。

v2 request 带 participant.agent_id/session_id 与 turn.stage；主人桥用 session_id 恢复自己的
持久 Agent，其 MCP、记忆和凭据仍留在主人环境。可用 --agent-id 只领取指定 Agent 的请求。
回稿先写临时文件再原子 rename，引擎不会读到半截。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from arena.prep import (  # noqa: E402
    EXTERNAL_KINDS, external_reply_envelope_path, external_reply_output,
)

INBOX_ROOT = ROOT / "data" / "debates" / "inbox"

Handler = Callable[[dict], Optional[str]]


# ── 投稿箱扫描 ────────────────────────────────────────────────────────────────

def pending_requests(inbox_root: Path, run_id: str | None = None,
                     agent_id: str | None = None) -> list[tuple[Path, Path, dict]]:
    """还没回、也还没过期的 request：[(request_path, reply_path, request_dict)]，按 run_id/seq 排。"""
    folders = [inbox_root / run_id] if run_id else sorted(p for p in inbox_root.glob("*") if p.is_dir())
    out: list[tuple[Path, Path, dict]] = []
    now = time.time()
    for folder in folders:
        if not folder.is_dir():
            continue
        for req_path in sorted(folder.glob("*.request.json")):
            reply_path = req_path.with_name(req_path.name[: -len(".request.json")] + ".reply.txt")
            try:
                req = json.loads(req_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            envelope_path = external_reply_envelope_path(reply_path)
            if envelope_path.exists():
                try:
                    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
                    external_reply_output(req, envelope)
                    continue
                except (OSError, TypeError, ValueError):
                    pass   # 身份串线/半截回执仍是 pending；正确主人可以原子覆盖修复
            elif reply_path.exists():
                continue
            participant = req.get("participant") if isinstance(req.get("participant"), dict) else {}
            request_agent_id = str(participant.get("agent_id") or req.get("model") or "")
            if agent_id and request_agent_id != agent_id:
                continue
            if float(req.get("deadline_epoch") or 0) <= now:
                continue   # 引擎已经按白卷处理了，回了也没人读
            out.append((req_path, reply_path, req))
    return out


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_reply(reply_path: Path, text: str, request: Optional[dict] = None) -> None:
    """Write a v2 identity-bound receipt plus a v1-compatible text projection."""
    if request and int(request.get("protocol_version") or 1) >= 2:
        participant = request.get("participant") if isinstance(request.get("participant"), dict) else {}
        receipt = {
            "protocol_version": 2,
            "request_id": str(request.get("request_id") or ""),
            "agent_id": str(participant.get("agent_id") or request.get("model") or ""),
            "status": "completed",
            "output": text,
            "completed_epoch": time.time(),
        }
        _atomic_write(
            external_reply_envelope_path(reply_path),
            json.dumps(receipt, ensure_ascii=False, indent=1),
        )
    tmp = reply_path.with_name(reply_path.name[: -len(".txt")] + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(reply_path)


# ── stub：本地代填（验收用，零额度）────────────────────────────────────────────

_SPEECH_RE = re.compile(r"^\[(S\d{2})\] 队([AB]) · 席(P\d{2}) · (.*)$", re.M)
_LIMIT_RE = re.compile(r"(\d{2,4})\s*字")


def _quote_from(text: str, n: int = 24) -> str:
    """从一段发言里切一句能在原文里找回来的短引（parse_ballot 比的是去标点后的子串）。"""
    body = text.strip().splitlines()
    first = next((ln.strip() for ln in body if ln.strip()), "")
    return first[:n]


def _parse_blind_transcript(prompt: str) -> list[dict]:
    """从评委 prompt 里抠出盲审转录：[{speech_id, team, pid, stage, text}]。"""
    rows: list[dict] = []
    matches = list(_SPEECH_RE.finditer(prompt))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(prompt)
        text = prompt[start:end]
        # 转录后面还跟着评分说明/插问实录，截到下一个空行块之前的正文足够做引文
        text = text.strip().split("\n\n")[0]
        rows.append({"speech_id": m.group(1), "team": m.group(2), "pid": m.group(3),
                     "stage": m.group(4).strip(), "text": text})
    return rows


def stub_handler(req: dict) -> Optional[str]:
    kind = str(req.get("kind") or "speech")
    seat = str(req.get("seat") or "外部席位")
    prompt = str(req.get("prompt") or "")
    tag = f"（本地桥代填·验收用·{seat}）"

    if kind == "speech":
        m = _LIMIT_RE.search(prompt)
        limit = int(m.group(1)) if m else 300
        core = (
            f"{tag}我方的论证链只有一条：先定义题面里的关键词，再证明我方立场在这个定义下成立，"
            f"最后说明对方的最强反驳为什么不构成推翻。第一步，题面说的不是绝对命题，是比较命题。"
            f"第二步，在比较的尺度上，我方举的例子能直接支撑结论，对方举的例子只说明例外存在。"
            f"第三步，对方最强的论点是把例外当常态，这一点我方一辩已经正面回应过。"
            f"所以本场的判准应当落在：谁的论证链在题面负担下真的闭合。我方闭合了，对方没有。"
        )
        return core[: max(60, limit)]
    if kind == "crossfire_q":
        return f"{tag}请正面回答：你方的核心定义如果换成对方的定义，你方结论还成立吗？"
    if kind == "crossfire_a":
        return f"{tag}成立。我方结论不依赖定义之争，依赖的是比较尺度上的证据。"
    if kind == "bench_answer":
        return f"{tag}我方最核心的一条：题面是比较命题，我方在比较尺度上举证闭合，对方只举了例外。"
    if kind == "prep":
        return "{}"   # 备赛 JSON 给空对象：引擎有解析失败的退路（退回收集笔记 / 未解析）
    if kind == "bench_question":
        return json.dumps({"target": "A", "question": "请用一句话说明你方论证链里最关键、也最脆弱的一环是什么？"},
                          ensure_ascii=False)
    if kind == "ballot":
        rows = _parse_blind_transcript(prompt)
        evidence = [{"speech_id": r["speech_id"], "quote": _quote_from(r["text"])} for r in rows[:3]
                    if _quote_from(r["text"])]
        by_team = {"A": 0, "B": 0}
        for r in rows:
            by_team[r["team"]] = by_team.get(r["team"], 0) + len(r["text"])
        winner = "A" if by_team["A"] >= by_team["B"] else "B"
        loser = "B" if winner == "A" else "A"
        mvp = next((r["pid"] for r in rows if r["team"] == winner), rows[0]["pid"] if rows else "P01")
        ballot = {
            "winner": winner,
            "margin": "narrow",
            "reason": f"{tag}按格式代填的票：只验引擎能收票、解析、计票，不代表任何评审判断。",
            "uncertainty": "本票为流程验收代填，无评审含义。",
            "evidence": evidence,
            "rubric_scores": {winner: [7, 7, 7, 7], loser: [6, 6, 6, 6]},
            "discretion": {winner: 20, loser: 18},
            "mvp": mvp,
        }
        return json.dumps(ballot, ensure_ascii=False)
    return None


# ── aisay：正式桥（等蛋的口子清单）─────────────────────────────────────────────

def aisay_handler(req: dict) -> Optional[str]:
    """把 request 送到 aisay 城里的 AI、把回稿拿回来。对接形态待 aisay 开发者给口子（辩论桌 /
    唤醒面板）后落地；落地前不代填、不猜——返回 None 让引擎按白卷处理，别让假稿混进赛录。"""
    raise NotImplementedError("aisay 桥等口子清单：见 notes/debate_aisay_bridge_20260819.md §五")


HANDLERS: dict[str, Handler] = {"stub": stub_handler, "aisay": aisay_handler}


# ── 主循环 ────────────────────────────────────────────────────────────────────

def run(inbox_root: Path, run_id: str | None, handler: Handler, *, poll: float = 1.0,
        idle_exit: float | None = None, once: bool = False,
        agent_id: str | None = None) -> int:
    """轮询投稿箱，见到没回的 request 就交给 handler、写回 reply。返回回了几条。
    idle_exit：连续这么多秒没新 request 就退出（验收脚本用）；None = 一直盯着。"""
    answered = 0
    last_seen = time.time()
    while True:
        todo = pending_requests(inbox_root, run_id, agent_id=agent_id)
        for req_path, reply_path, req in todo:
            kind = str(req.get("kind") or "speech")
            if kind not in EXTERNAL_KINDS:
                print(f"[bridge] skip unknown kind={kind!r}: {req_path.name}", flush=True)
                continue
            try:
                text = handler(req)
            except NotImplementedError as exc:
                print(f"[bridge] handler not ready: {exc}", flush=True)
                return answered
            except Exception as exc:  # noqa: BLE001
                print(f"[bridge] handler error on {req_path.name}: {exc}", flush=True)
                continue
            if text is None or not str(text).strip():
                continue
            write_reply(reply_path, str(text), req)
            answered += 1
            last_seen = time.time()
            print(f"[bridge] {req.get('run_id')} #{req.get('seq')} {kind} ← {req.get('seat')} ({len(str(text))} chars)",
                  flush=True)
        if once:
            return answered
        if idle_exit is not None and time.time() - last_seen > idle_exit:
            return answered
        time.sleep(poll)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-id", help="只盯这一场；不给就 --all")
    ap.add_argument("--all", action="store_true", help="盯整个投稿箱")
    ap.add_argument("--handler", choices=sorted(HANDLERS), default="stub")
    ap.add_argument("--agent-id", help="只处理这个 owner-managed agent_id 的请求")
    ap.add_argument("--inbox", type=Path, default=INBOX_ROOT)
    ap.add_argument("--poll", type=float, default=1.0)
    ap.add_argument("--idle-exit", type=float, default=None, help="连续多少秒没新 request 就退出")
    ap.add_argument("--once", action="store_true", help="扫一遍就退")
    args = ap.parse_args(argv)
    if not args.run_id and not args.all:
        ap.error("give --run-id <id> or --all")
    n = run(args.inbox, args.run_id, HANDLERS[args.handler], poll=args.poll,
            idle_exit=args.idle_exit, once=args.once, agent_id=args.agent_id)
    print(f"[bridge] done, answered {n}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
