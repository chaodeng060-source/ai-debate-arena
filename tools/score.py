#!/usr/bin/env python3
"""按 judging-criteria.md 量一场辩论——只数能数的，主观的留给人。

改了辩手 prompt 之后到底有没有用，
不能靠我读一遍说「感觉好多了」。这个脚本量三件事:

1. **交锋率**：每一席提到对方原话/对方论点的次数。写 skill 的直接动机就是
   上一版实测结辩提「对方」0 次——两篇作文各自漂亮，没有交锋。
2. **同义反复**：B 是不是从 A 里抄的。只能粗筛，抓「因为X所以X」的形状。
3. **judging-criteria 八条尺子**里可机检的部分（判准、底线、负担词）。

用法: python3 tools/score.py data/debates/debate-YYYYMMDD-HHMMSS.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# 交锋信号：提到对方、引对方原话、直接回应对方论证
# 教训：第一版只认「对方/您方/你方」，漏了「正方/反方」——
# 辩手常直接叫对面的方位名（正方二辩满篇「反方说」被记成零交锋），
# 差点把 skill 判死刑。称呼词表必须跟着场上真实用语走。
CLASH_PATTERNS = [
    r"对方(辩友|辩手|说|认为|提到|主张|告诉|一直|今天|刚才|方才)",
    r"您方",
    r"你方",
    r"正如对方",
    r"对方的(这个|那个|第[一二三四]|核心|最强)",
    r"[正反]方(辩友|说|认为|提到|主张|今天|刚才|把|将|所谓|最大)",
]
# 引用原话的更强信号
QUOTE_PATTERNS = [r"[「\"'']([^」\"'']{6,60})[」\"'']"]

# 立秤（比较标准）——只认自然语言形状。
# 教训：第一版把「判准」二字当正项，注入场正文念出 11 次「判准」
# 全被计成效果——那是指令术语泄漏进台词，
# 评分器等于在奖励八股。术语出现改计入 JARGON（泄漏负项）。
# 模式按 14:46 场真实语料校准（先看真句子再写正则，14:23 的教训第二次生效）：
# 「本场判准只有一把尺子：…」「我先把这把尺子换掉」「在这把尺子下」
# 「真诚的判断标准应是…而不是…」
CRITERION_PATTERNS = [
    r"[一这那]把(尺子|秤)",
    r"(判断|比较|评判|衡量|评分)的?标准",
    r"比的不是",
    r"真正(该|要)比",
    r"这场(比赛|辩论)?(比|争|问)的是",
    r"标准(应|只)(是|有)",
]
BOTTOMLINE_PATTERNS = [r"底线", r"哪一条被推翻", r"我方就输"]
BPRIME_PATTERNS = [r"更重要的(是|理由)", r"我不(反驳|否认)", r"我承认", r"恰恰(证明|说明)", r"换个?秤", r"真正该比的"]
# 指令黑话泄漏——出现即负项：教练黑板上的记号被念进了台词
JARGON_PATTERNS = [r"判准", r"A→B→C", r"A->B->C", r"理由\s*B", r"结论\s*C", r"B0", r"B[′']"]


def count(patterns: list[str], text: str) -> int:
    return sum(len(re.findall(p, text)) for p in patterns)


def count_clash(text: str, speaker_side: str = "") -> int:
    """数交锋信号；speaker_side 是发言者自己那方（'pro'/'con'），
    自方方位词（正方席说「正方」）是自指不是交锋，剔掉。"""
    total = count(CLASH_PATTERNS, text)
    own = {"pro": "正方", "con": "反方"}.get(speaker_side)
    if own:
        total -= len(re.findall(own + r"(辩友|说|认为|提到|主张|今天|刚才|把|将|所谓|最大)", text))
    return max(0, total)


def load_speeches(data: object) -> list[dict]:
    """比赛记录格式可能变，尽量宽容地找出发言列表。"""
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = (
            data.get("transcript")
            or data.get("speeches")
            or data.get("rounds")
            or data.get("log")
            or []
        )
        if not rows:
            for value in data.values():
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    rows = value
                    break
    else:
        rows = []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = ""
        for key in ("text", "content", "speech", "body", "output"):
            if isinstance(row.get(key), str) and row[key].strip():
                text = row[key]
                break
        if not text:
            continue
        label = " ".join(
            str(row.get(k))
            for k in ("side", "seat", "role", "stage", "phase", "name", "model")
            if row.get(k)
        )
        out.append({"label": label or "?", "text": text, "side": str(row.get("side") or "")})
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    path = Path(argv[1])
    data = json.loads(path.read_text(encoding="utf-8"))
    speeches = load_speeches(data)
    if not speeches:
        print(f"没找到发言内容，检查记录格式: {path}")
        print("顶层键:", list(data)[:20] if isinstance(data, dict) else type(data).__name__)
        return 1

    print(f"=== {path.name} · {len(speeches)} 段发言 ===\n")
    print(f"{'席位':<26} {'字数':>5} {'交锋':>4} {'引话':>4} {'立秤':>4} {'B′':>4} {'黑话':>4}")
    print("-" * 62)
    totals = {"clash": 0, "quote": 0, "criterion": 0, "bprime": 0, "jargon": 0}
    zero_clash = []
    for s in speeches:
        t = s["text"]
        row = {
            "clash": count_clash(t, s.get("side", "")),
            "quote": count(QUOTE_PATTERNS, t),
            "criterion": count(CRITERION_PATTERNS, t),
            "bprime": count(BPRIME_PATTERNS, t),
            "jargon": count(JARGON_PATTERNS, t),
        }
        for k, v in row.items():
            totals[k] += v
        if row["clash"] == 0:
            zero_clash.append(s["label"])
        print(
            f"{s['label'][:26]:<26} {len(t):>5} {row['clash']:>4} {row['quote']:>4} "
            f"{row['criterion']:>4} {row['bprime']:>4} {row['jargon']:>4}"
        )
    print("-" * 62)
    print(
        f"{'合计':<26} {sum(len(s['text']) for s in speeches):>5} "
        f"{totals['clash']:>4} {totals['quote']:>4} {totals['criterion']:>4} "
        f"{totals['bprime']:>4} {totals['jargon']:>4}"
    )
    print()
    print("【判读】")
    print(f"· 交锋率：{len(speeches) - len(zero_clash)}/{len(speeches)} 席提到了对方")
    if zero_clash:
        print(f"  零交锋的席位（各说各话，skill 没吃进去）：{', '.join(zero_clash)}")
    else:
        print("  没有零交锋席位——这是 skill 要治的头号病，看来治住了")
    print(f"· 立秤：{totals['criterion']} 处（自然语言的比较标准，不含术语宣布）")
    print(f"· B′ 信号：{totals['bprime']} 处（四辩结辩必须有，只否定不算交锋）")
    print(f"· 黑话泄漏：{totals['jargon']} 处（「判准/A→B→C/理由B」被念进台词——**负项**，>0 说明注入在漏）")
    print()
    print("注：这些是形状，不是质量。提到「对方」不等于真交锋——")
    print("最终还得人按 judging-criteria.md 八条尺子读一遍。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
