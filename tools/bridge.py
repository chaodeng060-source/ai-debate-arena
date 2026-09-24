#!/usr/bin/env python3
"""辩论场「桥」：把引擎写进投稿箱的出题送到外部 AI 手里、把回稿送回来。

    .venv/bin/python tools/bridge.py --run-id <run_id> --handler stub              # 验收：本地代填（零额度）
    .venv/bin/python tools/bridge.py --all --handler stub                           # 盯整个投稿箱
    .venv/bin/python tools/bridge.py --all --handler cmd --cmd "<可执行文件 参数...>"    # 接命令行 AI
    .venv/bin/python tools/bridge.py --run-id <run_id> --handler aisay             # 尚未接入，见下

引擎侧只认一个文件协议（arena/prep.py「外部 AI 席位」一节）：
    data/debates/inbox/<run_id>/<seq:04d>-<seat>.request.json   引擎写：{kind, seat, system, prompt, deadline_epoch, …}
    data/debates/inbox/<run_id>/<seq:04d>-<seat>.reply.txt      桥写：外部 AI 的回复正文
到 deadline 没 reply = 白卷，引擎不重试不代写。桥是独立进程，引擎一行不改就能换桥。
投稿箱根目录跟引擎共用同一个 DEBATE_DATA_DIR（没设就都落到 data/debates/），不会互相找不到人。

handler 就是「把一条 request 变成回复正文」的那一段，按 request.kind 分发（EXTERNAL_KINDS）：
- stub ：本地代填。只为验引擎流程（开赛→发言→质询→插问→评委票→观众票→榜）没坏，
          稿是模板、票是按格式填的——**这不是辩论质量验收，不许拿它的胜负/分数当成绩**。
          零额度验收：全 external 席位 + 桥代填，不起任何真模型。
- cmd  ：接任何读 stdin、吐 stdout 的命令行程序——claude、codex、ollama、自己写的脚本都行。
          题面（system+prompt 拼在一起）从 stdin 喂进去，stdout 当回稿；命令按参数列表执行，
          不经过 shell，出题内容不会被当成 shell 语法解释。超时、非零退出、空输出都当白卷
          处理（不重试、不代写），只打日志、不让桥退出。
          用法：--handler cmd --cmd "python3 my_ai.py" [--cmd-timeout 120]
          （更完整的例子和防注入说明见 README「命令行 handler」一节）。
- aisay：还没接入。对接形态（内建辩论桌 / 唤醒面板一行）由 aisay 侧定，方向是走 aisay、
          可多房间、7/11 席没问题，蛋壳在修理铺给的意见一并采纳；口子到了在 aisay_handler 里
          落地，文件协议不动。——致谢蛋宝、蛋壳。
          落地前选它会在启动时直接报错退出，不会等到比赛打到一半才发现外部席位全白卷；
          现在能用的外部桥是 stub 或 cmd。

回稿写法：先写 .reply.tmp 再原子 rename 成 .reply.txt，引擎见到文件就读，不会读到半截。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from arena.prep import EXTERNAL_KINDS  # noqa: E402

# 跟引擎（arena/room.py 的 TRANSCRIPT_DIR）认同一个数据目录，默认值也完全一致——
# 桥不读 DEBATE_DATA_DIR 就会去找一个引擎从来不用的旧默认路径，外部席位永远等不到桥。
DATA_DIR = Path(os.environ.get("DEBATE_DATA_DIR") or (ROOT / "data" / "debates"))
INBOX_ROOT = DATA_DIR / "inbox"

Handler = Callable[[dict], Optional[str]]


# ── 投稿箱扫描 ────────────────────────────────────────────────────────────────

def pending_requests(inbox_root: Path, run_id: str | None = None) -> list[tuple[Path, Path, dict]]:
    """还没回、也还没过期的 request：[(request_path, reply_path, request_dict)]，按 run_id/seq 排。"""
    folders = [inbox_root / run_id] if run_id else sorted(p for p in inbox_root.glob("*") if p.is_dir())
    out: list[tuple[Path, Path, dict]] = []
    now = time.time()
    for folder in folders:
        if not folder.is_dir():
            continue
        for req_path in sorted(folder.glob("*.request.json")):
            reply_path = req_path.with_name(req_path.name[: -len(".request.json")] + ".reply.txt")
            if reply_path.exists():
                continue
            try:
                req = json.loads(req_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if float(req.get("deadline_epoch") or 0) <= now:
                continue   # 引擎已经按白卷处理了，回了也没人读
            out.append((req_path, reply_path, req))
    return out


def write_reply(reply_path: Path, text: str) -> None:
    tmp = reply_path.with_name(reply_path.name[: -len(".txt")] + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(reply_path)


# ── stub：本地代填（验收用，零额度）────────────────────────────────────────────

_SPEECH_RE = re.compile(r"^\[(S\d{2})\] 队([AB]) · 席(P\d{2}) · (.*)$", re.M)
_LIMIT_RE = re.compile(r"(\d{2,4})\s*字")

# 备赛三步的 system 前缀（跟 arena/room.py 里 _run_prep 三个阶段写的 system 逐字对应）：
# 靠这三个前缀分辨这条 prep 出题是「独立收集」「队内讨论」还是「整理上场笔记」，
# 好回一份有真实内容、能通过 arena/prep.py 里对应 parse_* 的 JSON——不是空对象 "{}"。
_PREP_SCOUT_SYS = "你在做一场辩论的独立赛前研究"
_PREP_REVIEW_SYS = "你在和队友做赛前讨论"
_PREP_BOARD_SYS = "你在整理自己上场要带的笔记"
_WHO_RE = re.compile(r"你是 ?(\S+?)，队友是 ?(\S+?)。")


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


def _prep_stub(req: dict) -> str:
    """备赛三步的本地代填：回合法 JSON、带可辨认的真实内容——不是空对象。

    验的是「引擎能不能把这份内容解析成真笔记、笔记能不能跟着这一席一路带到发言出题里」，
    不是辩论质量。系统提示词认不出属于哪一步（比如测试里手写了个 system="sys"）就退回
    空对象："{}"，跟旧行为一致，交给 arena/prep.py 的解析器走「未解析/内容为空」的路。
    """
    system = str(req.get("system") or "")
    prompt = str(req.get("prompt") or "")
    seat = str(req.get("seat") or "外部席位")
    tag = f"（本地桥代填·验收用·{seat}）"

    if system.startswith(_PREP_SCOUT_SYS):
        return json.dumps({
            "preferred_role": "opening",
            "main_case": [f"{tag}主线一：题面负担在我方定义下成立",
                          f"{tag}主线二：对方最强论点没有得到正面回应"],
            "opponent_best_case": [f"{tag}对方最可能主打的一条"],
            "evidence": [],
            "source_urls": [],
            "uncertainties": [f"{tag}这条数据没有核实来源，上场不能当事实说"],
        }, ensure_ascii=False)

    who = _WHO_RE.search(prompt)
    me, mate = (who.group(1), who.group(2)) if who else (seat, "队友")
    if system.startswith(_PREP_REVIEW_SYS):
        return json.dumps({
            "strongest_shared": f"{tag}双方笔记里能共用的一条主线",
            "challenge_to_partner": f"{tag}给队友笔记提的一处修改",
            "preferred_role": "opening",
            "division": {me: f"{tag}[{me}] 负责的主线", mate: f"{tag}[{mate}] 负责的主线"},
            "unresolved": [],
        }, ensure_ascii=False)
    if system.startswith(_PREP_BOARD_SYS):
        return json.dumps({
            "preferred_role": "opening",
            "board": f"{tag}[{me}] 我上场主打：题面负担成立 + 怎么接对方最强点，"
                     f"分工上我负责主线、{mate} 负责另一条。",
            "unresolved": [],
        }, ensure_ascii=False)
    return "{}"   # 认不出属于哪一步：保留旧的空对象退路，走「解析失败/内容为空」的降级


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
        return _prep_stub(req)
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


# ── cmd：接任何读 stdin / 吐 stdout 的命令行程序 ─────────────────────────────────

def make_cmd_handler(argv: list[str], *, timeout: float = 60.0) -> Handler:
    """把 request 交给一个命令行程序：题面（system+prompt 拼在一起）走 stdin，stdout 当回稿。

    - 防注入：argv 是完整参数列表（第一项是可执行文件），subprocess 不经过 shell，
      出题内容只会作为 stdin 的字节流交给对方，不会被当成 shell 语法解释、也不会拼进命令行。
    - 超时、非零退出、空输出都当白卷处理：返回 None（不重试、不代写，交给引擎的
      deadline 机制去记「未在时限内作答」），只打一行诊断日志，不让桥的轮询循环退出。
    - 能接：`claude -p`、`codex exec -`、`ollama run <model>`、自己写的一行脚本——
      只要肯读 stdin、把回答吐到 stdout 就行。
    """
    argv = list(argv)
    if not argv:
        raise ValueError("make_cmd_handler 需要至少一项可执行文件")

    def handler(req: dict) -> Optional[str]:
        seat = str(req.get("seat") or "外部席位")
        kind = str(req.get("kind") or "speech")
        stdin_text = f"{req.get('system') or ''}\n\n{req.get('prompt') or ''}"
        try:
            proc = subprocess.run(
                argv, input=stdin_text, capture_output=True, text=True,
                timeout=timeout, shell=False,
            )
        except subprocess.TimeoutExpired:
            print(f"[bridge] cmd 超时（>{timeout}s）：{seat} ({kind})，按白卷处理", flush=True)
            return None
        except OSError as exc:
            print(f"[bridge] cmd 启动失败：{seat} ({kind})：{exc}，按白卷处理", flush=True)
            return None
        if proc.returncode != 0:
            err = (proc.stderr or "").strip().replace("\n", " ")[:200]
            print(f"[bridge] cmd 非零退出 {proc.returncode}：{seat} ({kind})：{err}，按白卷处理", flush=True)
            return None
        out = (proc.stdout or "").strip()
        if not out:
            print(f"[bridge] cmd 空输出：{seat} ({kind})，按白卷处理", flush=True)
            return None
        return out

    return handler


# ── aisay：正式桥（等口子）─────────────────────────────────────────────────────

def aisay_handler(req: dict) -> Optional[str]:
    """把 request 送到 aisay 城里的 AI、把回稿拿回来。对接形态待 aisay 开发者给出接口后落地；
    落地前不代填、不猜——调用即失败，见 main() 里对 --handler aisay 的启动期拦截（选它会在
    进入轮询循环之前就报错退出，不会等到比赛打到一半才发现外部席位全白卷）。
    现在能用的外部桥是 --handler stub（零额度验流程）或 --handler cmd（接命令行 AI）。"""
    raise NotImplementedError("aisay 桥尚未接入：等对方开放接口。现在可用 --handler cmd 接命令行 AI。")


HANDLERS: dict[str, Handler] = {"stub": stub_handler, "aisay": aisay_handler}


# ── 主循环 ────────────────────────────────────────────────────────────────────

def run(inbox_root: Path, run_id: str | None, handler: Handler, *, poll: float = 1.0,
        idle_exit: float | None = None, once: bool = False) -> int:
    """轮询投稿箱，见到没回的 request 就交给 handler、写回 reply。返回回了几条。
    idle_exit：连续这么多秒没新 request 就退出（验收脚本用）；None = 一直盯着。"""
    answered = 0
    last_seen = time.time()
    while True:
        todo = pending_requests(inbox_root, run_id)
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
            write_reply(reply_path, str(text))
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
    ap.add_argument("--handler", choices=sorted(set(HANDLERS) | {"cmd"}), default="stub")
    ap.add_argument("--cmd", help="handler=cmd 时必填：完整命令，比如 \"python3 my_ai.py\"")
    ap.add_argument("--cmd-timeout", type=float, default=60.0, help="handler=cmd 时单条出题的超时秒数")
    ap.add_argument("--inbox", type=Path, default=INBOX_ROOT)
    ap.add_argument("--poll", type=float, default=1.0)
    ap.add_argument("--idle-exit", type=float, default=None, help="连续多少秒没新 request 就退出")
    ap.add_argument("--once", action="store_true", help="扫一遍就退")
    args = ap.parse_args(argv)
    if not args.run_id and not args.all:
        ap.error("give --run-id <id> or --all")

    if args.handler == "aisay":
        print("[bridge] aisay 桥尚未接入：等对方开放接口后再选这个 handler。"
              "现在能用的是 --handler stub（本地代填验收）或 --handler cmd（接命令行 AI）。", flush=True)
        return 2
    if args.handler == "cmd":
        if not args.cmd:
            ap.error("--handler cmd 需要配 --cmd \"<可执行文件> [参数...]\"")
        handler: Handler = make_cmd_handler(shlex.split(args.cmd), timeout=args.cmd_timeout)
    else:
        handler = HANDLERS[args.handler]

    n = run(args.inbox, args.run_id, handler, poll=args.poll,
            idle_exit=args.idle_exit, once=args.once)
    print(f"[bridge] done, answered {n}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
