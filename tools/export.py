#!/usr/bin/env python3
"""把一场辩论赛的记录导成能直接搬运的 md / PDF。

赛录是拿出去给人看的，所以格式按「能直接转发」来做，不是内部调试格式。

PDF 走 reportlab + STSong-Light（无外部字体依赖也能出中文的路子）。

用法：
    python3 tools/export.py data/debates/<run_id>.json
    python3 tools/export.py <json> --md-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

# 辩手正文里的 [[quote:#短号]] 是给宿主前端渲染引用气泡的标记，
# 赛录 JSON 保留原样；导出成 md/PDF 时剥掉，读者看到的应该是台词不是标记。
_QUOTE_MARK = re.compile(r"\[\[quote:#[0-9a-fA-F]{4,12}\]\]\s*")


def speech_text(s: dict) -> str:
    return _QUOTE_MARK.sub("", str(s.get("text") or "")).strip()

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arena.prep import ordered_debate_events

ENGINE_LABEL = {
    ("codex", "gpt-5.5"): "GPT-5.5",
    ("codex", "gpt-5.6-sol"): "GPT-5.6-sol",
    ("claude", "claude-opus-5"): "Claude Opus 5",
    ("claude", "claude-fable-5"): "Claude Fable 5",
    ("claude", "claude-opus-4-6"): "Claude Opus 4.6",
    ("gemini", "gemini-3.1-pro-high"): "Gemini 3.1 Pro",
    ("gemini", "gemini-3.6-flash-high"): "Gemini 3.6 Flash",
}

MINI_STAGE_ORDER = (
    "正方一辩·立论", "反方一辩·立论",
    "交互质询·正方问", "交互质询·反方问",
    "正方二辩·驳论", "反方二辩·驳论",
    "反方一辩·结辩", "正方一辩·结辩",
)
FULL_STAGE_ORDER = (
    "正方一辩·立论", "反方一辩·立论",
    "正方二辩·驳论", "反方二辩·驳论",
    "交互质询·正方问", "交互质询·反方问",
    "自由辩·正方", "自由辩·反方", "自由辩·正方", "自由辩·反方",
    "反方四辩·结辩", "正方四辩·结辩",
)


def stage_order_of(data: dict) -> tuple[str, ...]:
    schedule = data.get("schedule") or []
    if schedule and all(isinstance(row, dict) and row.get("stage") for row in schedule):
        return tuple(
            str(row["stage"])
            for row in sorted(schedule, key=lambda row: int(row.get("index", 0)))
        )
    return MINI_STAGE_ORDER if data.get("format") == "mini" else FULL_STAGE_ORDER


def debate_events(data: dict) -> list[dict]:
    return ordered_debate_events(
        data.get("transcript") or [],
        data.get("crossfire") or [],
        stage_order=stage_order_of(data),
    )


def label_of(roster: list[dict], name: str) -> str:
    for d in roster:
        if d["name"] == name:
            key = (d["engine"], d["model"])
            model = ENGINE_LABEL.get(key, d["model"])
            return f"{model}·思考强度 {d['effort']}"
    return ""


def prep_receipts(prep: dict, side: str) -> str:
    """备赛收据一行一人：谁交了笔记、谁失败了。板子挂了时读者也能看到还有谁干活。

    实测踩过的坑：队长那一步挂了，PDF 只显示一句「无法解析」，看着像整队都没输出——
    其实队员的收集和队内评审都在 JSON 里。收据必须上纸。"""
    rows: list[str] = []
    for brief in (prep.get("scouts") or {}).get(side, []) or []:
        who = brief.get("scout") or "?"
        if brief.get("raw_status") == "parsed" and brief.get("main_case"):
            rows.append(f"{who} 收集：已交（主论点 {len(brief.get('main_case') or [])} 条）")
        else:
            rows.append(f"{who} 收集：失败/未解析")
    for review in (prep.get("discussion") or {}).get(side, []) or []:
        who = review.get("reviewer") or "?"
        ok = review.get("raw_status") == "parsed"
        rows.append(f"{who} 队内交流：{'已交' if ok else '失败/未解析'}")
    for pb in (prep.get("personal") or {}).get(side, []) or []:
        who = pb.get("label") or "?"
        state = {"parsed": "已交", "stitched_from_brief": "整理失败→带收集笔记",
                 "unparsed": "失败"}.get(pb.get("raw_status"), str(pb.get("raw_status")))
        rows.append(f"{who} 上场笔记：{state}")
    return "；".join(rows)


def build_md(data: dict) -> str:
    roster = data["roster"]
    lines: list[str] = []
    lines.append("# AI 辩论赛 · 完整赛录")
    lines.append("")
    lines.append(f"**辩题**：{data['topic']}")
    lines.append("")
    lines.append(f"- **正方**：{data['pro_side']}")
    lines.append(f"- **反方**：{data['con_side']}")
    if data.get("status"):
        lines.append(f"- **运行状态**：{data['status']}")
    if data.get("run_id"):
        lines.append(f"- **运行 ID**：`{data['run_id']}`")
    if data.get("rules_digest"):
        lines.append(f"- **规则摘要**：`{data['rules_digest']}`")
    if data.get("_source_name"):
        lines.append(f"- **原始记录**：`{data['_source_name']}`")
    if data.get("_source_sha256"):
        lines.append(f"- **JSON SHA-256**：`{data['_source_sha256']}`")
    lines.append("")
    lines.append("## 参赛阵容")
    lines.append("")
    lines.append("| 席位 | 模型 | 思考强度 |")
    lines.append("|---|---|---|")
    for d in roster:
        key = (d["engine"], d["model"])
        model = ENGINE_LABEL.get(key, d["model"])
        lines.append(f"| {d['name']} | {model} | {d['effort']} |")
    lines.append("")
    prep = data.get("prep") or {}
    if prep.get("status") not in (None, "disabled"):
        lines.append("## 赛前备赛")
        lines.append("")
        lines.append(
            f"状态：{prep.get('status')}；进入正赛的战术板上限 "
            f"{prep.get('board_char_limit', 800)} 字。原始资料和未核实项只留在赛录，不整包灌进正赛上下文。"
        )
        lines.append("")
        for side, plan in (prep.get("teams") or {}).items():
            label = "正方" if side == "pro" else "反方"
            lines.append(f"### {label}战术板")
            lines.append("")
            lines.append(
                f"一辩：{plan.get('opening_label', '?')}；二辩：{plan.get('rebuttal_label', '?')}"
            )
            lines.append("")
            board_head = "交流轮共同主线：" if prep.get("prep_model") == "personal_boards" else ""
            lines.append(board_head + str(plan.get("board") or "（无有效战术板）"))
            lines.append("")
            for pb in (prep.get("personal") or {}).get(side, []) or []:
                tag = {"parsed": "", "stitched_from_brief": "（整理失败，带的是自己的收集笔记）",
                       "unparsed": "（收集和整理都失败，裸打）"}.get(pb.get("raw_status"), "")
                lines.append(f"**{pb.get('label', '?')} 的上场笔记**{tag}")
                lines.append("")
                lines.append(str(pb.get("board") or "（空）"))
                lines.append("")
            receipts = prep_receipts(prep, side)
            if receipts:
                lines.append(f"备赛收据：{receipts}（状态：{plan.get('raw_status', '?')}）")
                lines.append("")
            urls = plan.get("source_urls") or []
            lines.append(
                "辩手报告已查阅链接："
                + ("、".join(urls) if urls else "无；这不等于论点已被外部核验")
            )
            unresolved = plan.get("unresolved") or []
            if unresolved:
                lines.append("")
                lines.append("仍未解决：" + "；".join(unresolved))
            lines.append("")

    lines.append("## 赛制")
    lines.append("")
    cps = data.get("chars_per_second", 6.5)
    lines.append(
        "立场由主持人在开赛时注入，全场锁死，不许倒戈；"
        f"发言时限换算成字数上限（本场配置 {cps} 字/秒），"
        "**超出部分由程序当场掐断**，掐在半句话上也照掐——跟真实赛场被计时器打断一样。"
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    for event in debate_events(data):
        if event["kind"] == "crossfire":
            block = event["row"]
            lines.append(f"## 交互质询逐字记录 · {block.get('stage') or '质询'}")
            lines.append("")
            exchanges = block.get("exchanges") or []
            if not exchanges:
                lines.append("（本段没有形成有效问答。）")
                lines.append("")
            for idx, exchange in enumerate(exchanges, 1):
                lines.append(f"**第 {idx} 问｜{exchange.get('asker', '?')}**：{exchange.get('q', '')}")
                lines.append("")
                lines.append(f"**回答｜{exchange.get('answerer', '?')}**：{exchange.get('a', '')}")
                lines.append("")
            continue
        s = event["row"]
        i = event["ledger_index"] + 1
        who = label_of(roster, s["speaker"])
        lines.append(f"## {i}. {s['stage']}")
        lines.append("")
        lines.append(f"*{s['speaker']}｜{who}*")
        lines.append("")
        lines.append(speech_text(s))
        lines.append("")
        mark = "，**发言被计时器掐断**" if s["truncated"] else "，在时限内说完"
        lines.append(f"> {s['chars']}/{s['limit']} 字{mark}。")
        quote_checks = s.get("quote_checks") or []
        if quote_checks:
            quotes = "、".join(f"「{row.get('quote', '')}」" for row in quote_checks)
            lines.append("")
            lines.append(f"> 引文核对：{quotes} 未在此前对方发言中逐字找到。")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 全场数据")
    lines.append("")
    lines.append("| 发言段 | 字数 | 上限 | 是否被掐 | 生成耗时 |")
    lines.append("|---|---|---|---|---|")
    for s in data["transcript"]:
        cut = "是" if s["truncated"] else "否"
        lines.append(
            f"| {s['stage']} | {s['chars']} | {s['limit']} | {cut} | {s['elapsed_sec']}s |"
        )
    lines.append("")
    total = sum(s["elapsed_sec"] for s in data["transcript"])
    cut_n = sum(1 for s in data["transcript"] if s["truncated"])
    lines.append(
        f"全场 {len(data['transcript'])} 段发言，{cut_n} 段被计时器掐断，"
        f"生成总耗时 {round(total, 1)} 秒。"
    )
    lines.append("")
    lines.extend(_scoring_section(data))

    jury = data.get("jury")
    verdict = data.get("verdict")
    if jury or verdict:
        lines.extend(["---", "", "## 评审结果", ""])
    if jury:
        status_label = {
            "decided": "形成稳定多数",
            "disputed": "评审分歧，暂不判",
            "position_unstable": "位置复判翻案，暂不判",
            "judge_failed": "有效票不足，评审失败",
        }.get(jury.get("status"), str(jury.get("status") or "未知"))
        winner = {"pro": "正方", "con": "反方"}.get(jury.get("winner"), "无")
        lines.append(f"**状态**：{status_label}　**胜方**：{winner}")
        lines.append("")
        if jury.get("review_note"):
            lines.append(f"> {jury['review_note']}")
            lines.append("")
        counts = jury.get("counts") or {}
        lines.append(
            f"票数：正方 {counts.get('pro', 0)} / 反方 {counts.get('con', 0)} / "
            f"平票 {counts.get('tie', 0)} / 不确定 {counts.get('uncertain', 0)}。"
        )
        mvp = jury.get("mvp")
        if isinstance(mvp, dict):
            if mvp.get("speaker"):
                who = mvp["speaker"] + (f"（{mvp['model']}）" if mvp.get("model") else "")
                lines.append(f"**最佳辩手**：{who}，{mvp.get('votes')}/{mvp.get('of')} 票。")
            else:
                lines.append(f"**最佳辩手**：空缺（{'、'.join(mvp.get('tie') or [])} 平票）。")
        lines.append("")
        for ballot in jury.get("ballots") or []:
            if ballot.get("role") == "recheck":
                continue   # 对调票只做位置自一致，不进赛报
            if not ballot.get("valid"):
                lines.append(f"- {ballot.get('ballot_id')}：无效（{ballot.get('error')}）")
                continue
            decision = {"pro": "正方", "con": "反方", "tie": "平票", "uncertain": "不确定"}.get(
                ballot.get("winner"), str(ballot.get("winner"))
            )
            lines.append(f"- {ballot.get('ballot_id')}：{decision}；{ballot.get('reason', '')}")
            for evidence in ballot.get("evidence") or []:
                lines.append(f"  - {evidence.get('speech_id')}：「{evidence.get('quote')}」")
        lines.append("")
        if jury.get("position_recheck_enabled") or jury.get("position_checked_judges"):
            unstable = jury.get("position_unstable_judges") or []
            checked = jury.get("position_checked_judges") or []
            lines.append(
                "各票互相不可见；每位评委另判一张 A/B 对调票，只用于检测自身位置偏好、不计票。"
                "主持播报不进入评审材料。"
                + (f"位置复判：{len(checked)} 位评委中 {len(unstable)} 位对调后翻案（{'、'.join(unstable)}）。"
                   if unstable else f"位置复判：{len(checked)} 位评委对调后判决不变。" if checked else "")
            )
        else:
            lines.append("三张票互相不可见；第二张交换 A/B 呈现顺序。主持播报不进入评审材料。")
        lines.append("")
    elif verdict:
        lines.append("本场为旧版单评委记录；以下意见没有位置复判，不能冒充稳定多数：")
        lines.append("")
        lines.append(str(verdict))
        lines.append("")
    return "\n".join(lines)


def _scoring_section(data: dict) -> list[str]:
    """按 judging-criteria.md 量能数的东西——skill 到底有没有效果，看这张表。

    改了 prompt 有没有效果，不能靠读一遍说「感觉好多了」。
    交锋率是头号指标——skill 之前实测两个结辩提对方原话 0 次，
    两篇作文各自漂亮，没有交锋。
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from score import QUOTE_PATTERNS, BPRIME_PATTERNS, CRITERION_PATTERNS, JARGON_PATTERNS, count, count_clash
    except Exception:  # noqa: BLE001 - 评分是加分项，缺了不该让导出失败
        return []

    lines = ["---", "", "## 自动诊断（不是质量分，也不决定胜负）", ""]
    lines.append("| 发言段 | 提到对方 | 引用原话 | 立秤 | B′ 信号 | 黑话泄漏 |")
    lines.append("|---|---|---|---|---|---|")
    zero = []
    for s in data["transcript"]:
        t = speech_text(s)
        clash = count_clash(t, s.get("side", ""))
        if clash == 0:
            zero.append(s["stage"])
        lines.append(
            f"| {s['stage']} | {clash} | {count(QUOTE_PATTERNS, t)} | "
            f"{count(CRITERION_PATTERNS, t)} | {count(BPRIME_PATTERNS, t)} | {count(JARGON_PATTERNS, t)} |"
        )
    lines.append("")
    n = len(data["transcript"])
    lines.append(f"**交锋率：{n - len(zero)}/{n} 席提到了对方。**")
    if zero:
        lines.append("")
        lines.append(f"零交锋席位（各说各话）：{'、'.join(zero)}")
    lines.append("")
    lines.append(
        "注：这些是形状不是质量。提到「对方」不等于真交锋，"
        "最终仍要人按八条尺子读一遍。"
    )
    lines.append("")
    return lines


def build_pdf(data: dict, out: Path) -> bool:
    """reportlab + STSong-Light：无外部字体依赖也能出中文。"""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
        )
        from reportlab.lib import colors
    except ImportError:
        return False

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    FONT = "STSong-Light"

    h1 = ParagraphStyle("h1", fontName=FONT, fontSize=20, leading=28,
                        spaceAfter=6 * mm)
    h2 = ParagraphStyle("h2", fontName=FONT, fontSize=14, leading=20,
                        spaceBefore=6 * mm, spaceAfter=2 * mm,
                        textColor=colors.HexColor("#1a3a5c"))
    meta = ParagraphStyle("meta", fontName=FONT, fontSize=9, leading=14,
                          textColor=colors.HexColor("#666666"),
                          spaceAfter=3 * mm)
    body = ParagraphStyle("body", fontName=FONT, fontSize=10.5, leading=18,
                          firstLineIndent=21, spaceAfter=2 * mm)
    note = ParagraphStyle("note", fontName=FONT, fontSize=9, leading=14,
                          textColor=colors.HexColor("#8a5a00"),
                          spaceBefore=1 * mm, spaceAfter=4 * mm)

    doc = SimpleDocTemplate(
        str(out), pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title="AI 辩论赛 · 完整赛录", author="AI Debate Arena",
    )
    story = []
    roster = data["roster"]

    story.append(Paragraph("AI 辩论赛 · 完整赛录", h1))
    story.append(Paragraph(f"辩题：{data['topic']}", h2))
    story.append(Paragraph(
        f"正方：{data['pro_side']}　｜　反方：{data['con_side']}", meta))
    provenance = []
    if data.get("status"):
        provenance.append(f"状态 {data['status']}")
    if data.get("run_id"):
        provenance.append(f"运行 ID {data['run_id']}")
    if data.get("rules_digest"):
        provenance.append(f"规则 {data['rules_digest']}")
    if data.get("_source_name"):
        provenance.append(f"原始记录 {data['_source_name']}")
    if provenance:
        story.append(Paragraph("　｜　".join(provenance), meta))
    if data.get("_source_sha256"):
        story.append(Paragraph(f"JSON SHA-256: {data['_source_sha256']}", meta))
    rows = [["席位", "模型", "思考强度"]]
    for d in roster:
        model = ENGINE_LABEL.get((d["engine"], d["model"]), d["model"])
        rows.append([d["name"], model, d["effort"]])
    tbl = Table(rows, colWidths=[30 * mm, 60 * mm, 30 * mm])
    tbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), FONT),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f7")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c8d0da")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 4 * mm))
    cps = data.get("chars_per_second", 6.5)
    story.append(Paragraph(
        "赛制：立场由主持人开赛时注入并全场锁死，不许倒戈；发言时限换算成字数上限"
        f"（本场配置 {cps} 字/秒），超出部分由程序当场掐断，掐在半句话上也照掐。", meta))

    prep = data.get("prep") or {}
    if prep.get("status") not in (None, "disabled"):
        story.append(Paragraph("赛前备赛", h2))
        story.append(Paragraph(
            f"状态：{prep.get('status')}；战术板上限 {prep.get('board_char_limit', 800)} 字。"
            "原始资料和未核实项留在 JSON，不整包进入正赛上下文。", meta))
        for side, plan in (prep.get("teams") or {}).items():
            label = "正方" if side == "pro" else "反方"
            story.append(Paragraph(f"{label}战术板", h2))
            story.append(Paragraph(
                f"一辩：{plan.get('opening_label', '?')}　｜　二辩：{plan.get('rebuttal_label', '?')}",
                meta,
            ))
            board_text = str(plan.get("board") or "（无有效战术板）")
            story.append(Paragraph(
                board_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), body
            ))
            for pb in (prep.get("personal") or {}).get(side, []) or []:
                tag = {"parsed": "", "stitched_from_brief": "（整理失败，带的是自己的收集笔记）",
                       "unparsed": "（收集和整理都失败，裸打）"}.get(pb.get("raw_status"), "")
                story.append(Paragraph(f"{pb.get('label', '?')} 的上场笔记{tag}", meta))
                for para in [x for x in str(pb.get("board") or "（空）").split("\n") if x.strip()]:
                    story.append(Paragraph(
                        para.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), body
                    ))
            receipts = prep_receipts(prep, side)
            if receipts:
                story.append(Paragraph(
                    f"备赛收据：{receipts}（状态：{plan.get('raw_status', '?')}）"
                    .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), meta
                ))
    story.append(HRFlowable(width="100%", color=colors.HexColor("#c8d0da")))

    for event in debate_events(data):
        if event["kind"] == "crossfire":
            block = event["row"]
            story.append(Paragraph(
                f"交互质询逐字记录 · {block.get('stage') or '质询'}", h2
            ))
            exchanges = block.get("exchanges") or []
            if not exchanges:
                story.append(Paragraph("（本段没有形成有效问答。）", note))
            for index, exchange in enumerate(exchanges, 1):
                q = str(exchange.get("q") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                a = str(exchange.get("a") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                story.append(Paragraph(f"第 {index} 问｜{exchange.get('asker', '?')}：{q}", body))
                story.append(Paragraph(f"回答｜{exchange.get('answerer', '?')}：{a}", body))
            continue
        s = event["row"]
        i = event["ledger_index"] + 1
        who = label_of(roster, s["speaker"])
        story.append(Paragraph(f"{i}. {s['stage']}", h2))
        story.append(Paragraph(f"{s['speaker']}　{who}", meta))
        for para in [p for p in speech_text(s).split("\n") if p.strip()]:
            clean = (para.replace("&", "&amp;").replace("<", "&lt;")
                         .replace(">", "&gt;").replace("**", ""))
            story.append(Paragraph(clean, body))
        mark = "发言被计时器掐断" if s["truncated"] else "在时限内说完"
        story.append(Paragraph(f"—— {s['chars']}/{s['limit']} 字，{mark}", note))

    story.append(HRFlowable(width="100%", color=colors.HexColor("#c8d0da")))
    story.append(Paragraph("全场数据", h2))
    rows = [["发言段", "字数", "上限", "被掐", "耗时"]]
    for s in data["transcript"]:
        rows.append([s["stage"], str(s["chars"]), str(s["limit"]),
                     "是" if s["truncated"] else "否", f"{s['elapsed_sec']}s"])
    tbl = Table(rows, colWidths=[45 * mm, 22 * mm, 22 * mm, 20 * mm, 22 * mm])
    tbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), FONT),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f7")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c8d0da")),
        ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(tbl)

    jury = data.get("jury")
    verdict = data.get("verdict")
    if jury or verdict:
        story.append(HRFlowable(width="100%", color=colors.HexColor("#c8d0da")))
        story.append(Paragraph("评审结果", h2))
    if jury:
        status_label = {
            "decided": "形成稳定多数",
            "disputed": "评审分歧，暂不判",
            "position_unstable": "位置复判翻案，暂不判",
            "judge_failed": "有效票不足，评审失败",
        }.get(jury.get("status"), str(jury.get("status") or "未知"))
        winner = {"pro": "正方", "con": "反方"}.get(jury.get("winner"), "无")
        story.append(Paragraph(f"状态：{status_label}　｜　胜方：{winner}", body))
        if jury.get("review_note"):
            review = str(jury["review_note"]).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            story.append(Paragraph(review, note))
        mvp = jury.get("mvp")
        if isinstance(mvp, dict):
            if mvp.get("speaker"):
                who = mvp["speaker"] + (f"（{mvp['model']}）" if mvp.get("model") else "")
                story.append(Paragraph(f"最佳辩手：{who}，{mvp.get('votes')}/{mvp.get('of')} 票", body))
            else:
                story.append(Paragraph(f"最佳辩手：空缺（{'、'.join(mvp.get('tie') or [])} 平票）", body))
        for ballot in jury.get("ballots") or []:
            if ballot.get("role") == "recheck":
                continue   # 对调票只做位置自一致，不进赛报
            if not ballot.get("valid"):
                story.append(Paragraph(
                    f"{ballot.get('ballot_id')}：无效（{ballot.get('error')}）", note
                ))
                continue
            decision = {"pro": "正方", "con": "反方", "tie": "平票", "uncertain": "不确定"}.get(
                ballot.get("winner"), str(ballot.get("winner"))
            )
            reason = str(ballot.get("reason") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            story.append(Paragraph(f"{ballot.get('ballot_id')}：{decision}；{reason}", body))
            for evidence in ballot.get("evidence") or []:
                quote = str(evidence.get("quote") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                story.append(Paragraph(f"{evidence.get('speech_id')}：「{quote}」", note))
        checked = jury.get("position_checked_judges") or []
        unstable = jury.get("position_unstable_judges") or []
        if jury.get("position_recheck_enabled") or checked:
            tail = (f"位置复判：{len(checked)} 位评委中 {len(unstable)} 位对调后翻案。" if unstable
                    else f"位置复判：{len(checked)} 位评委对调后判决不变。" if checked else "")
            story.append(Paragraph(
                "各票互相不可见；每位评委另判一张 A/B 对调票，只检测自身位置偏好、不计票。"
                "主持播报不进入评审材料。" + tail, meta
            ))
        else:
            story.append(Paragraph(
                "三张票互相不可见；第二张交换 A/B 呈现顺序。主持播报不进入评审材料。", meta
            ))
    elif verdict:
        story.append(Paragraph(
            "本场为旧版单评委记录；以下意见没有位置复判，不能冒充稳定多数。", note
        ))
        for para in [p for p in str(verdict).split("\n") if p.strip()]:
            clean = para.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("**", "")
            story.append(Paragraph(clean, body))

    doc.build(story)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path")
    ap.add_argument("--md-only", action="store_true")
    ap.add_argument("--outdir", default="")
    args = ap.parse_args()

    src = Path(args.json_path)
    raw = src.read_bytes()
    data = json.loads(raw)
    data["_source_name"] = src.name
    data["_source_sha256"] = hashlib.sha256(raw).hexdigest()
    outdir = Path(args.outdir) if args.outdir else src.parent
    outdir.mkdir(parents=True, exist_ok=True)

    md_path = outdir / (src.stem + ".md")
    md_path.write_text(build_md(data), encoding="utf-8")
    print(f"md  → {md_path}")

    if not args.md_only:
        pdf_path = outdir / (src.stem + ".pdf")
        if build_pdf(data, pdf_path):
            print(f"pdf → {pdf_path}")
        else:
            print("pdf → 跳过（reportlab 未安装）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
