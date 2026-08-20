#!/usr/bin/env python3
"""跨场评委一致性统计（Elliot 与 Laurie 评阅第 6 节口径）。

    python3 tools/consistency.py            # markdown 报告
    python3 tools/consistency.py --json

只做赛内测不了、必须跨场累计的那部分：三席之间一致不一致。按第 6 节的五条算：
1. **随机校正**：胜负是二分类，两位评委瞎投也有 50% 重合。报 Cohen's kappa（两两）+ Fleiss' kappa（三席），
   不报裸重合率当结论——但裸重合率和胜负分布也一起给（第 5 条：偏斜时 kappa 会失真，得一起看）。
2. **以分项为主、票为辅**：票每场 1 个观测；四项尺子每场每队各一份，每场 8 个观测。分项用 ICC(2,1)
   （绝对一致、双向随机、单测量）。
3. **先做评委内标准化**：三席底座不同、打分习惯不同，分项先按评委自己的均值/标准差 z 化再算 ICC，
   裸分 ICC 也给，两个并列——差值就是打分习惯吃掉的部分。
4. **自留分算两遍**：含与不含各算一次（尺子四项 vs 尺子四项+自留），差值即自留分对一致性的实际影响。
5. **偏斜提示**：胜负一边倒时在报告里明写。

没有依赖 numpy/scipy，纯 Python。场次少时 kappa/ICC 都不稳，报告头上明写 N，结论由人下。
新式赛录只计原序票（role=primary），对调票是位置自一致用的、不是独立观测；旧式三票（无 role）三张都算独立观测。
评委按席位名（评委甲/乙/丙）对齐，席位背后的模型按场轮换——量的是三席之间的一致，不是某个固定模型。

致谢：第 6 节的五条口径出自 Elliot 和 Laurie 的评阅，这个脚本只是把它们接到赛录上。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from itertools import combinations
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.board import load_records  # noqa: E402  按场去重、只要走到评审的

RUBRIC_N = 4


SEAT_NAMES = ("评委甲", "评委乙", "评委丙")


def _independent(ballot: dict) -> bool:
    """算不算一个独立观测。新式（有 role）：recheck 是同一评委换标签重判，不独立；
    旧式（无 role）：三张票是三位评委各判一次，换位那张也是独立观测（只是呈现顺序不同），都算。"""
    role = ballot.get("role")
    if role in {"primary", "recheck"}:
        return role == "primary"
    return True


def _judge_key(ballot: dict) -> str:
    """统一成席位名：新式有 judge=评委甲/乙/丙；旧式只有 ballot-N，N 对应席位顺序。
    席位名背后的模型按场轮换，这里量的是「三席」之间的一致，不是某个固定模型。"""
    key = str(ballot.get("judge") or "")
    if key:
        return key
    bid = str(ballot.get("ballot_id") or "")
    for i, name in enumerate(SEAT_NAMES, 1):
        if bid.startswith(f"ballot-{i}"):
            return name
    return bid


def collect(records: Iterable[dict]) -> dict:
    """从赛录抽观测：每场 → {judge: {"winner": pro|con, "pro": [4 分], "con": [4 分], "disc": {pro, con}}}"""
    matches: list[dict] = []
    for data in records:
        jury = data.get("jury") or {}
        per_judge: dict[str, dict] = {}
        for b in jury.get("ballots") or []:
            if not b.get("valid") or not _independent(b):
                continue
            key = _judge_key(b)
            if not key or b.get("winner") not in {"pro", "con"}:
                continue
            row: dict = {"winner": b["winner"]}
            scores = b.get("scores")
            if isinstance(scores, dict):
                for side in ("pro", "con"):
                    rub = (scores.get(side) or {}).get("rubric")
                    if isinstance(rub, list) and len(rub) == RUBRIC_N:
                        row[side] = [float(x) for x in rub]
                        row.setdefault("disc", {})[side] = float((scores.get(side) or {}).get("discretion") or 0)
            per_judge[key] = row
        if len(per_judge) >= 2:
            matches.append({"run_id": data.get("run_id") or data.get("_file"), "judges": per_judge,
                            "winner": jury.get("winner")})
    return {"matches": matches}


# ---------- kappa ----------

def cohen_kappa(pairs: list[tuple[str, str]]) -> float | None:
    n = len(pairs)
    if n == 0:
        return None
    cats = sorted({a for a, _ in pairs} | {b for _, b in pairs})
    po = sum(1 for a, b in pairs if a == b) / n
    pe = sum((sum(1 for a, _ in pairs if a == c) / n) * (sum(1 for _, b in pairs if b == c) / n) for c in cats)
    if pe >= 1.0:
        return 1.0 if po >= 1.0 else 0.0
    return (po - pe) / (1 - pe)


def fleiss_kappa(rows: list[list[str]]) -> float | None:
    """每行一场、行内是各席的投票（席数可不等，按行内席数算）。"""
    rows = [r for r in rows if len(r) >= 2]
    if not rows:
        return None
    cats = sorted({c for r in rows for c in r})
    total = sum(len(r) for r in rows)
    p_j = {c: sum(r.count(c) for r in rows) / total for c in cats}
    p_i = []
    for r in rows:
        n = len(r)
        p_i.append(sum(r.count(c) * (r.count(c) - 1) for c in cats) / (n * (n - 1)))
    p_bar = sum(p_i) / len(p_i)
    p_e = sum(v * v for v in p_j.values())
    if p_e >= 1.0:
        return 1.0 if p_bar >= 1.0 else 0.0
    return (p_bar - p_e) / (1 - p_e)


# ---------- ICC(2,1) ----------

def icc_2_1(table: list[list[float]]) -> float | None:
    """table[subject][rater]，每行评委数相同、无缺失。双向随机、绝对一致、单测量。"""
    table = [r for r in table if r and all(x is not None for x in r)]
    if len(table) < 2:
        return None
    k = len(table[0])
    if k < 2 or any(len(r) != k for r in table):
        return None
    n = len(table)
    grand = sum(sum(r) for r in table) / (n * k)
    row_means = [sum(r) / k for r in table]
    col_means = [sum(table[i][j] for i in range(n)) / n for j in range(k)]
    ss_rows = k * sum((m - grand) ** 2 for m in row_means)
    ss_cols = n * sum((m - grand) ** 2 for m in col_means)
    ss_total = sum((table[i][j] - grand) ** 2 for i in range(n) for j in range(k))
    ss_err = ss_total - ss_rows - ss_cols
    ms_r = ss_rows / (n - 1)
    ms_c = ss_cols / (k - 1)
    ms_e = ss_err / ((n - 1) * (k - 1)) if (n - 1) * (k - 1) > 0 else 0.0
    denom = ms_r + (k - 1) * ms_e + k * (ms_c - ms_e) / n
    if denom == 0:
        return None
    return (ms_r - ms_e) / denom


def _zscore_by_judge(obs: dict[str, list[float]]) -> dict[str, tuple[float, float]]:
    out = {}
    for judge, xs in obs.items():
        if len(xs) < 2:
            out[judge] = (0.0, 1.0)
            continue
        mu = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1)) or 1.0
        out[judge] = (mu, sd)
    return out


def analyse(collected: dict) -> dict:
    matches = collected["matches"]
    n_matches = len(matches)
    judges = sorted({j for m in matches for j in m["judges"]})

    # --- 票 ---
    vote_rows = [[m["judges"][j]["winner"] for j in judges if j in m["judges"]] for m in matches]
    pairwise = {}
    for a, b in combinations(judges, 2):
        pairs = [(m["judges"][a]["winner"], m["judges"][b]["winner"]) for m in matches
                 if a in m["judges"] and b in m["judges"]]
        if pairs:
            pairwise[f"{a}×{b}"] = {
                "n": len(pairs),
                "raw_agreement": round(sum(1 for x, y in pairs if x == y) / len(pairs), 3),
                "cohen_kappa": None if (k := cohen_kappa(pairs)) is None else round(k, 3),
            }
    winners = [m["winner"] for m in matches if m["winner"] in {"pro", "con"}]
    skew = {"pro": winners.count("pro"), "con": winners.count("con"), "undecided": n_matches - len(winners)}
    fk = fleiss_kappa(vote_rows)

    # --- 分项 ---
    # subject = (场, 队, 尺子项)；rater = 评委。只取三席都带分的场，保证表无缺失。
    full = [m for m in matches if len(m["judges"]) == len(judges) and
            all(("pro" in m["judges"][j] and "con" in m["judges"][j]) for j in judges)]
    raw_obs: dict[str, list[float]] = {j: [] for j in judges}
    for m in full:
        for j in judges:
            for side in ("pro", "con"):
                raw_obs[j].extend(m["judges"][j][side])
    z = _zscore_by_judge(raw_obs)

    def table(with_disc: bool, standardize: bool) -> list[list[float]]:
        rows = []
        for m in full:
            for side in ("pro", "con"):
                for i in range(RUBRIC_N):
                    rows.append([_norm(m["judges"][j][side][i], z[j], standardize) for j in judges])
                if with_disc:
                    # 自留分 0–30 换算到 0–10 一个尺子项的量纲再并入
                    rows.append([_norm(m["judges"][j]["disc"][side] / 3.0, z[j], standardize) for j in judges])
        return rows

    icc = {
        "rubric_raw": icc_2_1(table(False, False)),
        "rubric_z": icc_2_1(table(False, True)),
        "rubric_plus_disc_raw": icc_2_1(table(True, False)),
        "rubric_plus_disc_z": icc_2_1(table(True, True)),
    }
    icc = {k: (None if v is None else round(v, 3)) for k, v in icc.items()}

    # 自留分跟尺子分反向的评委（第 5 节）：同一场同一队，自留分高的队尺子分却低
    reverse = {j: 0 for j in judges}
    for m in full:
        for j in judges:
            r = m["judges"][j]
            d_rub = sum(r["pro"]) - sum(r["con"])
            d_disc = r["disc"]["pro"] - r["disc"]["con"]
            if d_rub * d_disc < 0:
                reverse[j] += 1

    return {
        "n_matches": n_matches,
        "n_fully_scored": len(full),
        "judges": judges,
        "votes": {"fleiss_kappa": None if fk is None else round(fk, 3), "pairwise": pairwise,
                  "winner_distribution": skew},
        "rubric_icc": icc,
        "discretion_reverse_count": reverse,
    }


def _norm(x: float, mu_sd: tuple[float, float], standardize: bool) -> float:
    if not standardize:
        return x
    mu, sd = mu_sd
    return (x - mu) / sd


def to_markdown(r: dict) -> str:
    lines = [
        f"## 评委一致性（{r['n_matches']} 场有 ≥2 张有效原序票，{r['n_fully_scored']} 场三席都带分）",
        "",
        "**决胜票**",
        f"- Fleiss' κ（三席）：{r['votes']['fleiss_kappa']}",
    ]
    for pair, v in r["votes"]["pairwise"].items():
        lines.append(f"- {pair}：裸重合 {v['raw_agreement']:.0%}（n={v['n']}），Cohen's κ {v['cohen_kappa']}")
    w = r["votes"]["winner_distribution"]
    lines.append(f"- 胜负分布：正方 {w['pro']} / 反方 {w['con']} / 未判 {w['undecided']}"
                 + ("　⚠️ 一边倒，κ 会被压低，别单看它" if (w["pro"] == 0 or w["con"] == 0) and (w["pro"] + w["con"]) >= 2 else ""))
    icc = r["rubric_icc"]
    lines += [
        "",
        "**分项 ICC(2,1)**（绝对一致；z = 先按评委自己的均值/标准差标准化）",
        f"- 四项尺子：裸分 {icc['rubric_raw']} · z {icc['rubric_z']}",
        f"- 四项尺子 + 自留分：裸分 {icc['rubric_plus_disc_raw']} · z {icc['rubric_plus_disc_z']}",
        f"- 自留分与尺子分反向的场次：" + "、".join(f"{j} {n}" for j, n in r["discretion_reverse_count"].items()),
        "",
        f"*样本 {r['n_matches']} 场。Elliot 的口径：κ/ICC 要多场才有基数；这里只是把算法接上、把数算出来，结论等场次。*",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="跨场评委一致性")
    ap.add_argument("--dir", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    records, _skipped = load_records(Path(args.dir)) if args.dir else load_records()
    report = analyse(collect(records))
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else to_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
