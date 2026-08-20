"""观众席：人和 AI 都能看、都能投票，另出一份榜。

人和 AI 都能看、都能投。「尽量客观」落成五条规矩，写死在代码里不是写在说明里：

1. **盲投**：开赛即开票，评委票公布（phase=done）即关票。窗口开着时谁也看不到分布——
   只能看到自己投了什么和一共几个人投了。公示前不跟风、不被评委带节奏。
2. **一人一票**：按 voter_id 去重；窗口内可以改票，最后一次算数（记 revisions 次数）。
3. **利益回避**：voter 是场上某席位的主人（外部席位的 owner）或席位本身（aisay:<id>），
   这张票标 conflict=True——照收、照公示，但**不进「客观票」**、不进观众准确率榜。
4. **票不进裁决**：观众票是独立的「观众选择」，评委盲审定胜负，两边互不影响。
5. **结构化**：side 必填（pro|con）；favorite（最喜爱辩手，席位名）可选；reason ≤ 100 字可选。

榜两个面（都在 tools/board.py 出）。选手榜四个数：**MVP、参赛次数、赢的次数、观众最喜爱次数**。
- 「观众最喜爱」按**次数**不按票数：每场客观票里 favorite 提名最多的席位 = 该场观众最喜爱，那位选手 +1；
  平票并列都算（观众喜爱可以并列）。按次数是为了场与场之间观众多少不同也公平。
- 观众榜（?by=audience）：每个 voter 投了几场、跟评委裁决一致几次、一致率、回避票几张。
  一致率只算客观票、只算判出胜负的场——这就是「尽量客观」的激励：投得准才上榜。

存储：data/debates/votes/<run_id>.json（窗口期间读写）；局终把汇总挂进赛录 state["audience"]，
榜只读赛录。票不写进 transcript——transcript 是盲审物料，不能被观众污染。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

SIDES = ("pro", "con")
VOTER_KINDS = ("human", "ai")
REASON_MAX = 100
TERMINAL = {"completed", "judge_failed", "cancelled", "failed"}


def votes_dir(transcript_dir: Path) -> Path:
    return transcript_dir / "votes"


def votes_path(transcript_dir: Path, run_id: str) -> Path:
    return votes_dir(transcript_dir) / f"{run_id}.json"


def _norm_id(value: object) -> str:
    s = str(value or "").strip()
    return s[len("aisay:"):] if s.startswith("aisay:") else s


def is_window_open(state: Optional[dict]) -> bool:
    """开赛即开票，评委公示（phase=done）或终态即关票。没这场 = 关。"""
    if not state:
        return False
    if state.get("phase") == "done":
        return False
    return state.get("status") not in TERMINAL


def conflict_of(voter_id: str, roster: list[dict]) -> Optional[str]:
    """voter 是某席位的主人或席位本身 → 返回冲突的席位名；否则 None。"""
    vid = _norm_id(voter_id)
    if not vid:
        return None
    for seat in roster or []:
        if _norm_id(seat.get("owner")) == vid or _norm_id(seat.get("model")) == vid:
            return str(seat.get("name") or seat.get("label") or "?")
    return None


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
        if isinstance(data, dict) and isinstance(data.get("votes"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"votes": {}}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def validate_vote(body: dict) -> tuple[Optional[dict], Optional[str]]:
    voter_id = str(body.get("voter_id") or "").strip()
    if not voter_id or len(voter_id) > 120:
        return None, "voter_id required (≤120 chars)"
    kind = str(body.get("voter_kind") or "ai").strip().lower()
    if kind not in VOTER_KINDS:
        return None, "voter_kind must be human|ai"
    side = str(body.get("side") or "").strip().lower()
    if side not in SIDES:
        return None, "side must be pro|con"
    # favorite = 最喜爱辩手（席位名）；mvp 作别名兼容
    favorite = str(body.get("favorite") or body.get("mvp") or "").strip() or None
    reason = str(body.get("reason") or "").strip()
    if len(reason) > REASON_MAX:
        return None, f"reason ≤ {REASON_MAX} chars"
    return {"voter_id": voter_id, "voter_kind": kind, "side": side, "favorite": favorite, "reason": reason or None}, None


def record_vote(transcript_dir: Path, run_id: str, state: Optional[dict], vote: dict) -> tuple[int, dict]:
    """落一张票。返回 (http 状态码, 响应体)。"""
    if not state:
        return 404, {"error": "no such match"}
    if not is_window_open(state):
        return 409, {"error": "voting_closed", "phase": state.get("phase"), "status": state.get("status")}
    roster = state.get("roster") or []
    if vote.get("favorite") and vote["favorite"] not in {str(s.get("name") or "") for s in roster}:
        return 400, {"error": "favorite must be a seat name of this match",
                     "seats": [str(s.get("name") or "") for s in roster]}
    path = votes_path(transcript_dir, run_id)
    data = _read(path)
    data.setdefault("run_id", run_id)
    data.setdefault("opened_at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    prev = data["votes"].get(vote["voter_id"])
    conflict = conflict_of(vote["voter_id"], roster)
    entry = {
        **vote,
        "conflict": bool(conflict),
        "conflict_seat": conflict,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "revisions": (int(prev.get("revisions", 0)) + 1) if prev else 0,
    }
    data["votes"][vote["voter_id"]] = entry
    _write(path, data)
    return 200, {"ok": True, "run_id": run_id, "revised": prev is not None,
                 "conflict": bool(conflict), "conflict_seat": conflict,
                 "total_voters": len(data["votes"])}


def summarize(votes: dict, roster: list[dict]) -> dict:
    """票箱 → 公示用汇总。all = 全部票；unaffiliated = 去掉回避票的客观票。
    audience_favorite = 客观票里 favorite 提名最多的席位（平票并列），这场的「观众最喜爱」。"""
    tally = {"pro": 0, "con": 0}
    clean = {"pro": 0, "con": 0}
    fav: dict[str, int] = {}
    kinds = {"human": 0, "ai": 0}
    conflicts = 0
    for v in votes.values():
        side = v.get("side")
        if side not in SIDES:
            continue
        tally[side] += 1
        kinds[v.get("voter_kind") or "ai"] = kinds.get(v.get("voter_kind") or "ai", 0) + 1
        if v.get("conflict"):
            conflicts += 1
        else:
            clean[side] += 1
            f = v.get("favorite") or v.get("mvp")   # 旧票箱兼容
            if f:
                fav[f] = fav.get(f, 0) + 1
    total = tally["pro"] + tally["con"]
    clean_total = clean["pro"] + clean["con"]
    pick = None
    if clean["pro"] != clean["con"]:
        pick = "pro" if clean["pro"] > clean["con"] else "con"
    top = max(fav.values()) if fav else 0
    favorites = sorted(k for k, n in fav.items() if n == top) if top else []
    return {
        "voters": total,
        "by_kind": kinds,
        "all": tally,
        "unaffiliated": clean,
        "unaffiliated_voters": clean_total,
        "conflict_votes": conflicts,
        "audience_pick": pick,                  # 客观票多数方；平票 None
        "favorite_nominations": dict(sorted(fav.items(), key=lambda kv: (-kv[1], kv[0]))),
        "audience_favorite": favorites,         # 这场观众最喜爱（并列都在）
        "ballots": [
            {"voter_id": vid, "voter_kind": v.get("voter_kind"), "side": v.get("side"),
             "favorite": v.get("favorite") or v.get("mvp"), "conflict": bool(v.get("conflict")),
             "reason": v.get("reason")}
            for vid, v in sorted(votes.items())
        ],
    }


def public_view(transcript_dir: Path, run_id: str, state: Optional[dict], voter_id: str = "") -> tuple[int, dict]:
    """窗口开着：只给自己的票 + 总人数。关了：给全部分布。"""
    if not state:
        return 404, {"error": "no such match"}
    data = _read(votes_path(transcript_dir, run_id))
    if is_window_open(state):
        mine = data["votes"].get(voter_id) if voter_id else None
        return 200, {"run_id": run_id, "open": True, "total_voters": len(data["votes"]),
                     "mine": ({"side": mine["side"], "favorite": mine.get("favorite") or mine.get("mvp"),
                               "conflict": bool(mine.get("conflict"))} if mine else None),
                     "note": "盲投中：评委公示前看不到分布。"}
    summary = state.get("audience") or summarize(data["votes"], state.get("roster") or [])
    return 200, {"run_id": run_id, "open": False, **summary}


def close_and_summarize(transcript_dir: Path, run_id: str, state: dict) -> dict:
    """局终调：把票箱汇总挂进 state["audience"]（调用方负责落盘 state）。"""
    path = votes_path(transcript_dir, run_id)
    data = _read(path)
    summary = summarize(data["votes"], state.get("roster") or [])
    summary["closed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    state["audience"] = summary
    if data["votes"] or path.exists():
        data["closed_at"] = summary["closed_at"]
        _write(path, data)
    return summary


def summary_markdown(summary: dict, pro: str, con: str) -> str:
    if not summary.get("voters"):
        return "观众席没有人投票。"
    a, u = summary["all"], summary["unaffiliated"]
    lines = [
        f"观众 {summary['voters']} 人投票（人 {summary['by_kind'].get('human', 0)} · AI {summary['by_kind'].get('ai', 0)}）。",
        f"- 全部票：正方「{pro}」{a['pro']} · 反方「{con}」{a['con']}",
        f"- 客观票（去掉 {summary['conflict_votes']} 张自家票）：正方 {u['pro']} · 反方 {u['con']}",
    ]
    pick = summary.get("audience_pick")
    lines.append(f"- 观众选择：{'正方' if pick == 'pro' else '反方' if pick == 'con' else '平票'}（不影响评委裁决）")
    if summary.get("audience_favorite"):
        noms = summary.get("favorite_nominations") or {}
        lines.append("- 观众最喜爱：" + "、".join(f"{k}（{noms.get(k, 0)} 票）" for k in summary["audience_favorite"]))
    return "\n".join(lines)


def audience_board(records) -> dict:
    """观众榜：每个 voter 投了几场、跟评委裁决一致几次、一致率、回避票几张。
    一致率只算客观票、只算判出胜负的场。"""
    rows: dict[str, dict] = {}
    matches_with_votes = 0
    for data in records:
        aud = data.get("audience") or {}
        ballots = aud.get("ballots") or []
        if not ballots:
            continue
        matches_with_votes += 1
        jury = data.get("jury") or {}
        winner = jury.get("winner") if jury.get("winner") in SIDES else None
        for b in ballots:
            vid = str(b.get("voter_id") or "")
            if not vid:
                continue
            row = rows.setdefault(vid, {"key": vid, "voter_kind": b.get("voter_kind"),
                                        "voted": 0, "judged": 0, "agreed": 0, "conflict_votes": 0})
            row["voted"] += 1
            if b.get("conflict"):
                row["conflict_votes"] += 1
                continue
            if winner:
                row["judged"] += 1
                if b.get("side") == winner:
                    row["agreed"] += 1
    table = sorted(rows.values(),
                   key=lambda r: (-(r["agreed"] / r["judged"] if r["judged"] else 0), -r["judged"], r["key"]))
    for r in table:
        r["accuracy"] = round(r["agreed"] / r["judged"], 3) if r["judged"] else 0.0
    return {"by": "audience", "matches_with_votes": matches_with_votes, "table": table}


def audience_board_markdown(board: dict) -> str:
    lines = [f"## 观众榜（{board['matches_with_votes']} 场有观众投票）", "",
             "| 观众 | 类型 | 投票场次 | 可对账 | 与评委一致 | 一致率 | 自家票 |",
             "|---|---|---:|---:|---:|---:|---:|"]
    for r in board["table"]:
        lines.append(f"| {r['key']} | {r.get('voter_kind') or '-'} | {r['voted']} | {r['judged']} | "
                     f"{r['agreed']} | {r['accuracy']:.0%} | {r['conflict_votes']} |")
    lines += ["", "*一致率只算客观票（非自家 AI 参赛的场）、只算评委判出胜负的场。*"]
    return "\n".join(lines)


# ── 路由：挂在 room.router 下 ──────────────────────────────────────────
router = APIRouter()


def _deps():
    from arena import room as dr
    return dr


def _load_state(transcript_dir: Path, run_id: str) -> Optional[dict]:
    if not run_id or "/" in run_id or ".." in run_id:
        return None
    path = transcript_dir / f"{run_id}.json"
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


@router.post("/api/debate/{run_id}/vote")
async def debate_vote(run_id: str, req: Request):
    """观众投票。body: {voter_id, voter_kind?: human|ai, side: pro|con, mvp?: 席位名, reason?: ≤100 字}
    开赛即可投，评委公示即关；窗口内可改票。自家 AI 在场的票照收但标 conflict。"""
    dr = _deps()
    body = await req.json()
    vote, err = validate_vote(body if isinstance(body, dict) else {})
    if err:
        return JSONResponse({"error": err}, status_code=400)
    state = _load_state(dr.TRANSCRIPT_DIR, run_id)
    status, payload = record_vote(dr.TRANSCRIPT_DIR, run_id, state, vote)
    return JSONResponse(payload, status_code=status)


@router.get("/api/debate/{run_id}/votes")
async def debate_votes(run_id: str, req: Request):
    """盲投中只回自己的票和总人数（?voter_id=）；公示后回全部分布。"""
    dr = _deps()
    state = _load_state(dr.TRANSCRIPT_DIR, run_id)
    status, payload = public_view(dr.TRANSCRIPT_DIR, run_id, state, (req.query_params.get("voter_id") or "").strip())
    return JSONResponse(payload, status_code=status)
