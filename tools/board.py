#!/usr/bin/env python3
"""选手统计榜：参赛场次、胜场、MVP 次数。

    python3 tools/board.py                 # markdown 表
    python3 tools/board.py --json          # 机读
    python3 tools/board.py --by seat       # 按席位（正方一辩…）而非按模型

口径：
- **参赛**：一场按 run_id 去重（同一场的 -jury / -rejudged 衍生文件只算一次，取最后判的那份）。
  一个模型在同一场占两个席位，仍只算这场参赛一次——选手是「谁」，不是「几张椅子」。
- **胜**：这场 jury.winner 有结果、且该选手在胜方有席位。评审分歧 / 位置不稳 / 判不出来的场
  照样计参赛，不计胜——那是没判出胜负，不是输了。
- **MVP**：jury.mvp.speaker 对回该场 roster 拿到的模型。早期赛录的评委票里没有这一栏，
  统计不出来，表下会注明有多少场没这项——不许拿别的信号（比如被引用次数）冒充 MVP。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable

DEBATE_DIR = Path(__file__).resolve().parent.parent / "data" / "debates"
DECIDED = {"pro", "con"}


def _load(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("roster"):
        return None
    return data


def load_records(directory: Path = DEBATE_DIR) -> tuple[list[dict], int]:
    """一场一条：按 run_id 去重，同一场多份判决取评审最完整的那份。

    返回（进榜的场次, 跳过的场次数）。**进榜 = 评委席出过票**——中断、取消、只有
    roster 没打完的记录不算参赛（早期 12 份就是这种）。跳过数要报出来，不许静默扔掉。
    """
    best: dict[str, tuple[int, str, dict]] = {}
    for path in sorted(directory.glob("debate-*.json")):
        data = _load(path)
        if data is None:
            continue
        key = str(data.get("run_id") or path.stem)
        jury = data.get("jury") or {}
        # 排序键：有胜负 > 有票 > 只有 roster；同级里新式票（带 mvp 栏）优先；再同级取文件名靠后的（重判版）。
        # 注意 "-jury.json" 的 '-' 排在 ".json" 的 '.' 前面，光比文件名会把重判版排到原件后头。
        rank = 2 if jury.get("winner") in DECIDED else (1 if jury.get("ballots") else 0)
        new_style = int(any(isinstance(b, dict) and "mvp" in b for b in (jury.get("ballots") or [])))
        prev = best.get(key)
        if prev is None or (rank, new_style, path.name) > (prev[0], prev[1], prev[2]):
            best[key] = (rank, new_style, path.name, {**data, "_file": path.name})
    rows = [row for row in sorted(best.values(), key=lambda r: r[2])]
    played = [row[3] for row in rows if row[0] >= 1]
    return played, len(rows) - len(played)


def tally(records: Iterable[dict], *, by: str = "model", skipped: int = 0) -> dict:
    rows: dict[str, dict] = {}
    seen_side: dict[str, set] = {}
    mvp_capable = 0
    decided = 0
    total = 0

    def row(key: str) -> dict:
        # 四个数：MVP、参赛次数、赢的次数、观众最喜爱次数
        return rows.setdefault(key, {"key": key, "mvp": 0, "played": 0, "won": 0, "audience_favorite": 0})

    audience_matches = 0
    for data in records:
        total += 1
        jury = data.get("jury") or {}
        winner = jury.get("winner") if jury.get("winner") in DECIDED else None
        if winner:
            decided += 1
        # 这场评委票里到底有没有 MVP 这一栏（之前没有）
        if any(isinstance(b, dict) and "mvp" in b for b in (jury.get("ballots") or [])):
            mvp_capable += 1
        mvp_speaker = (jury.get("mvp") or {}).get("speaker") if isinstance(jury.get("mvp"), dict) else None
        # 观众席（晚起）：这场「观众最喜爱」= 客观票里提名最多的席位（平票并列），按**次数**计
        audience = data.get("audience") or {}
        favorites = set(audience.get("audience_favorite") or [])
        if audience.get("voters"):
            audience_matches += 1

        seats: dict[str, set] = {}
        for seat in data.get("roster") or []:
            key = str(seat.get(by) or "")
            if not key:
                continue
            seats.setdefault(key, set()).add(str(seat.get("side") or ""))
            seat_name = str(seat.get("name") or "")
            if mvp_speaker and seat_name == mvp_speaker:
                row(key)["mvp"] += 1
            if seat_name in favorites:
                row(key)["audience_favorite"] += 1
        for key, sides in seats.items():
            entry = row(key)
            entry["played"] += 1
            if winner and winner in sides:
                entry["won"] += 1
        seen_side.update(seats)

    table = sorted(
        rows.values(),
        key=lambda r: (-r["mvp"], -r["won"], -r["audience_favorite"],
                       -(r["won"] / r["played"] if r["played"] else 0), r["key"]),
    )
    for entry in table:
        entry["win_rate"] = round(entry["won"] / entry["played"], 3) if entry["played"] else 0.0
    return {
        "by": by,
        "matches": total,
        "skipped_unplayed": skipped,
        "decided": decided,
        "mvp_capable_matches": mvp_capable,
        "audience_matches": audience_matches,
        "table": table,
    }


def to_markdown(board: dict) -> str:
    head = "模型" if board["by"] == "model" else "席位"
    lines = [
        f"## 选手榜（{board['matches']} 场，其中 {board['decided']} 场判出胜负）",
        "",
        f"| {head} | MVP | 参赛 | 胜 | 观众最喜爱 | 胜率 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in board["table"]:
        lines.append(
            f"| {row['key']} | {row['mvp']} | {row['played']} | {row['won']} | "
            f"{row.get('audience_favorite', 0)} | {row['win_rate']:.0%} |"
        )
    notes = []
    missing = board["matches"] - board["mvp_capable_matches"]
    if missing:
        notes.append(f"MVP 随评委票统计；此前 {missing} 场的票里没有这一栏，不计入。")
    if board.get("audience_matches") is not None:
        notes.append(f"观众最喜爱按次数：每场客观票（去掉自家 AI 在场的票）里提名最多的席位计 1 次，平票并列；"
                     f"不影响胜负。{board['audience_matches']} 场有观众投票。")
    if board.get("skipped_unplayed"):
        notes.append(f"另有 {board['skipped_unplayed']} 份记录没走到评审（中断/取消/半成品），不算参赛。")
    if notes:
        lines += [""] + [f"*{n}*" for n in notes]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="辩论选手统计榜")
    ap.add_argument("--dir", default=str(DEBATE_DIR))
    ap.add_argument("--by", choices=("model", "name"), default="model", help="model=按模型（默认）, name=按席位")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    records, skipped = load_records(Path(args.dir))
    board = tally(records, by=args.by, skipped=skipped)
    if args.json:
        print(json.dumps(board, ensure_ascii=False, indent=2))
    else:
        print(to_markdown(board))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
