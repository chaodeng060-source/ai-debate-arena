#!/usr/bin/env python3
"""前置实验：三席评委的插问落在同几处漏洞上吗？（Elliot 与 Laurie 评阅第 4.2 节）

    python3 tools/bench_overlap.py data/debates/debate-xxx.json --runs 3

同一份盲审转录，交给三席评委各出 N 次插问，看问题落在哪些发言段（S 号）、哪一队，
席与席之间集合重合度多少（Jaccard）。重合度高 → 三席分别提问不带来额外覆盖，可合并成统一环节；
重合度低 → 三席有独立视角，保持各自提问。同时是评委独立性的早期观测：连提问都趋同，判决大概率也趋同。

「落在哪处漏洞」按两层算：粗 = 问哪一队（target）；细 = 问题文本里能对上的发言段 S 号 / 关键词重合。
评委出题时不带 speech_id，所以细层用问题文本与各段转录的字重合（bigram Jaccard）定位最像的那段。
只读赛录、只调评委 CLI，不改赛录。结果写 notes/corner/ 下一篇 md。

致谢：这个实验是 Elliot 和 Laurie 在评阅第 4.2 节里提的，连读法都是她们给的。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arena import room  # noqa: E402
from arena.prep import blind_transcript, build_bench_question_prompt, parse_bench_question  # noqa: E402
from tools.export import stage_order_of  # noqa: E402


def _bigrams(text: str) -> set[str]:
    t = re.sub(r"[\s，。、；：？！「」『』（）()\"'“”‘’—\-·,.!?;:]", "", text)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _locate(question: str, transcript: list[dict]) -> tuple[str, float]:
    """问题最像哪一段发言：bigram Jaccard 最大的 S 号。"""
    qb = _bigrams(question)
    best, score = "", 0.0
    for i, row in enumerate(transcript, 1):
        sb = _bigrams(str(row.get("text") or ""))
        if not qb or not sb:
            continue
        j = len(qb & sb) / len(qb | sb)
        if j > score:
            best, score = f"S{i:02d}", j
    return best, round(score, 3)


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a | b) else 0.0


async def run(path: Path, runs: int, timeout: int) -> dict:
    data = json.loads(path.read_text("utf-8"))
    transcript = data["transcript"]
    topic = str(data.get("topic") or "")
    stage_order = stage_order_of(data)
    blinded, mapping = blind_transcript(transcript, data.get("crossfire") or [], swap=False, stage_order=stage_order)
    prompt = build_bench_question_prompt(topic=topic, blinded=blinded)
    panel = room._draw_panel(seed=int(data.get("draw_seed") or 0) or None)

    async def one(judge: dict, k: int) -> dict:
        raw, err = await room._ask_judge(judge, prompt, timeout=timeout, max_tokens=200)
        parsed = parse_bench_question(raw, side_to_label=mapping) if raw else None
        row = {"judge": judge["name"], "label": judge["label"], "run": k, "raw": (raw or "")[:300], "err": err}
        if parsed:
            sid, sim = _locate(parsed["question"], transcript)
            row.update({"target": parsed["target"], "question": parsed["question"], "speech": sid, "sim": sim})
        return row

    jobs = [one(j, k) for j in panel for k in range(1, runs + 1)]
    rows = await asyncio.gather(*jobs)
    ok = [r for r in rows if r.get("question")]

    by_judge: dict[str, list[dict]] = defaultdict(list)
    for r in ok:
        by_judge[r["judge"]].append(r)
    seat_targets = {j: Counter(r["target"] for r in rs) for j, rs in by_judge.items()}
    seat_speeches = {j: {r["speech"] for r in rs if r["speech"]} for j, rs in by_judge.items()}
    seat_bigrams = {j: set().union(*(_bigrams(r["question"]) for r in rs)) for j, rs in by_judge.items()}
    pair_overlap = {}
    for a, b in combinations(sorted(by_judge), 2):
        pair_overlap[f"{a}×{b}"] = {
            "speech_jaccard": round(_jaccard(seat_speeches[a], seat_speeches[b]), 3),
            "text_bigram_jaccard": round(_jaccard(seat_bigrams[a], seat_bigrams[b]), 3),
            "same_target_rate": round(
                sum(1 for x in by_judge[a] for y in by_judge[b] if x["target"] == y["target"])
                / max(1, len(by_judge[a]) * len(by_judge[b])), 3),
        }
    # 同一席自己 N 次有多稳（自一致）：问同一段的比例
    self_stability = {j: round(max(Counter(r["speech"] for r in rs).values()) / len(rs), 3) if rs else None
                      for j, rs in by_judge.items()}
    all_speeches = Counter(r["speech"] for r in ok if r["speech"])
    return {
        "file": path.name, "topic": topic, "runs": runs, "panel": [{"name": j["name"], "label": j["label"]} for j in panel],
        "asked": len(ok), "failed": len(rows) - len(ok),
        "questions": [{k: r[k] for k in ("judge", "label", "run", "target", "speech", "sim", "question")} for r in ok],
        "seat_targets": {j: dict(c) for j, c in seat_targets.items()},
        "seat_speeches": {j: sorted(s) for j, s in seat_speeches.items()},
        "pair_overlap": pair_overlap,
        "self_stability": self_stability,
        "hot_speeches": all_speeches.most_common(5),
    }


def to_markdown(r: dict) -> str:
    lines = [
        f"# 插问重合度实验 · {r['file']}",
        "",
        f"辩题：{r['topic']}　评委席：{' / '.join(p['name'] + '=' + p['label'] for p in r['panel'])}　每席 {r['runs']} 次，"
        f"成功 {r['asked']} 问、失败 {r['failed']}",
        "",
        "## 每席问了什么",
    ]
    for q in r["questions"]:
        lines.append(f"- {q['judge']}#{q['run']} → 队{q['target']} · 最像 {q['speech']}（相似 {q['sim']}）：{q['question']}")
    lines += ["", "## 重合度（席×席）"]
    for pair, v in r["pair_overlap"].items():
        lines.append(f"- {pair}：问同一队 {v['same_target_rate']:.0%} · 落点段 Jaccard {v['speech_jaccard']} · 问题字面 Jaccard {v['text_bigram_jaccard']}")
    lines += ["", "## 每席自己稳不稳（N 次里问同一段的最高比例）"]
    for j, s in r["self_stability"].items():
        lines.append(f"- {j}：{s}")
    lines += ["", f"热点段：{', '.join(f'{s}×{n}' for s, n in r['hot_speeches'])}", "",
              "Elliot 4.2 的读法：落点重合高 → 三席分别问不带来额外覆盖，可合并成统一环节（三席同时提交、主持去重、一次作答）；"
              "重合低 → 保持各自提问。这一份只是一场，结论要多场。"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)
    result = asyncio.run(run(Path(args.json_path), args.runs, args.timeout))
    md = to_markdown(result)
    out = Path(args.out) if args.out else (
        Path(__file__).resolve().parent.parent / "notes" / "corner"
        / f"{time.strftime('%Y-%m-%d')}-bench-overlap-{Path(args.json_path).stem[:24]}.md")
    out.write_text(md + "\n\n```json\n" + json.dumps(result, ensure_ascii=False, indent=1) + "\n```\n", "utf-8")
    print(md)
    print(f"\n→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
