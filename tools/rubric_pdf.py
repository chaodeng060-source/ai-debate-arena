#!/usr/bin/env python3
"""「评分细则」一页纸 PDF——发给评委或外部合作方看的那一份。

内容全部取自真源：
- app/prep.py 的 RUBRIC_ITEMS / RUBRIC_ITEM_MAX / RUBRIC_TOTAL_MAX / DISCRETION_MAX /
  BENCH_Q_CHARS / BENCH_A_CHARS 与 build_ballot_prompt 的裁判纪律；
- app/room.py 的 _RUBRIC_FOOTER（长评输出要求）；
- data/debates/reference/judging-criteria.md 「给辩论场评审 prompt 用的二十二条尺子」小节
  （评委长评 prompt 动态读的就是这一节）。
PDF 走 reportlab + STSong-Light（同 tools/export.py）。
用法：.venv/bin/python tools/rubric_pdf.py [--out data/uploads/debate-rubric-20260819.pdf]
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arena import prep as dp  # noqa: E402
from arena import room as dr  # noqa: E402

CRITERIA = ROOT / "data" / "debates" / "reference" / "judging-criteria.md"


def load_22_rules() -> tuple[list[str], str]:
    text = CRITERIA.read_text("utf-8")
    m = re.search(r"^# 给辩论场评审 prompt 用的.*?尺子\s*$", text, re.M)
    assert m, "judging-criteria.md 缺二十二条小节"
    section = text[m.end():]
    rules = []
    tail = []
    for line in section.splitlines():
        s = line.strip()
        if re.match(r"^\d+\.\s", s):
            rules.append(re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s))
        elif s and rules:
            tail.append(re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s))
    assert len(rules) >= 20, len(rules)
    return rules, " ".join(tail)


def build(out: Path) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle, KeepTogether)

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    F = "STSong-Light"
    H1 = ParagraphStyle("h1", fontName=F, fontSize=16, leading=22, spaceAfter=4)
    H2 = ParagraphStyle("h2", fontName=F, fontSize=12.5, leading=17, spaceBefore=9, spaceAfter=3,
                        textColor=colors.HexColor("#1f3a5f"))
    P = ParagraphStyle("p", fontName=F, fontSize=9.8, leading=14.5)
    SM = ParagraphStyle("sm", fontName=F, fontSize=8.6, leading=12.5, textColor=colors.HexColor("#555555"))
    LI = ParagraphStyle("li", fontName=F, fontSize=9.6, leading=14, leftIndent=10)

    rules, rules_tail = load_22_rules()
    rubric_rows = [[Paragraph("<b>尺子</b>", P), Paragraph("<b>A 队</b>", P), Paragraph("<b>B 队</b>", P)]]
    for i, item in enumerate(dp.RUBRIC_ITEMS, 1):
        rubric_rows.append([Paragraph(f"{i}. {item}", P),
                            Paragraph(f"0–{dp.RUBRIC_ITEM_MAX}", P), Paragraph(f"0–{dp.RUBRIC_ITEM_MAX}", P)])
    rubric_rows.append([Paragraph(f"四项小计 ×{dp.RUBRIC_TOTAL_MAX / (dp.RUBRIC_ITEM_MAX * len(dp.RUBRIC_ITEMS)):.2f} 折算", P),
                        Paragraph(f"/ {dp.RUBRIC_TOTAL_MAX}", P), Paragraph(f"/ {dp.RUBRIC_TOTAL_MAX}", P)])
    rubric_rows.append([Paragraph("自留分（评委按自己的辩论观给的整体印象分，可奖励尺子没覆盖的洞见、勇气、语言的准确与美感）", P),
                        Paragraph(f"0–{dp.DISCRETION_MAX}", P), Paragraph(f"0–{dp.DISCRETION_MAX}", P)])
    rubric_rows.append([Paragraph("<b>决胜票</b>（不论分数如何必须投 A 或 B，不许弃权、不许平票）", P),
                        Paragraph("A / B", P), Paragraph("—", P)])
    tbl = Table(rubric_rows, colWidths=[118 * mm, 26 * mm, 26 * mm])
    tbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), F),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#9aa5b1")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef5")),
        ("BACKGROUND", (0, len(dp.RUBRIC_ITEMS) + 1), (-1, len(dp.RUBRIC_ITEMS) + 1), colors.HexColor("#f4f6f8")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))

    story = [
        Paragraph("AI 辩论赛 · 评分细则", H1),
        Paragraph(f"现行版 {date.today().isoformat()} · 取自 app/prep.py / app/room.py / "
                  "data/debates/reference/judging-criteria.md", SM),
        Spacer(1, 4),

        Paragraph("一、评审怎么组成", H2),
        Paragraph("· 三位 AI 评委<b>盲审</b>：只看脱敏转录（队伍写成 A/B、席位写成席号），看不到模型名、看不到主持播报；"
                  "三位来自不同家（当前：GPT-5.5 / GPT-5.6 / Claude Opus 5）。", LI),
        Paragraph("· <b>位置复判</b>：其中一票把 A/B 呈现顺序交换后再判，防先后位置偏好。", LI),
        Paragraph(f"· <b>评委席插问</b>：闭幕后每位评委向一方提一个问题（≤{dp.BENCH_Q_CHARS} 字，必须针对转录里真实出现过的论证漏洞），"
                  f"被问方当场作答（≤{dp.BENCH_A_CHARS} 字），答得正面与否计入评分。AI 最会念稿，插问逼即时应答才见真水平。", LI),
        Paragraph("· <b>同题往届真人评委发言</b>：评审可看、辩手不可看，只做尺度校准；不是本场证据，不许因为某队打法像往届胜方而加减分。", LI),

        Paragraph("二、明德杯式记分（每位评委一张票）", H2),
        tbl,
        Spacer(1, 3),
        Paragraph("<b>票归票、分归分</b>：分数只公示不定胜负，胜负以决胜票为准（像真人评委席那样）。"
                  "分与票不一致时标出来给观众看（score_vote_consistent=false）。", P),
        Paragraph("每张票还必须带：决胜点（≤240 字）、最大不确定性（≤160 字）、至少两条<b>逐字短引</b>（≤25 字，标 speech_id）作证据。", P),
        Paragraph("<b>裁判纪律</b>：不能因篇幅更长、先后位置或文风偏好投票；只评论证质量与交锋水平，不评立场本身的道德倾向——"
                  "立场是抽签抽来的，「更安全」「更符合主流价值」不构成投票理由；一方论证得更狠、更完整，即使结论让你不舒服，票也应该给它。", P),

        Paragraph("三、长评用的二十二条尺子（从真人评委发言里提炼）", H2),
        Paragraph("来源：2024 新国辩总决赛、2025 新国辩决赛、2026 新国辩复赛 EF 组、「原生家庭批判」一场共十余位评委的口头判词。"
                  "评委长评 prompt 动态读的就是这二十二条。", SM),
    ]
    for r in rules:
        story.append(Paragraph(r, LI))
    story.append(Spacer(1, 3))
    story.append(Paragraph("长评输出要求：" + dr._RUBRIC_FOOTER.split("\n", 1)[1].replace("\n- ", "；").replace("- ", "").replace("**", ""), P))
    if rules_tail:
        story.append(Paragraph(rules_tail, SM))

    story.append(Paragraph("四、场上硬规矩（与评分直接相关）", H2))
    story.append(Paragraph("· 立场由主持人开赛时注入、全场锁死，不许倒戈（倒戈词表检测）。", LI))
    story.append(Paragraph("· 发言时限换算成字数上限（默认 6.5 字/秒），超出部分由程序当场掐断，掐在半句话上也照掐——跟真实赛场被计时器打断一样。", LI))
    story.append(Paragraph("· 引用对方原句要逐字，主持核对（quote_checks）；引不准不算交锋。", LI))

    doc = SimpleDocTemplate(str(out), pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm,
                            title="AI 辩论赛 · 评分细则", author="AI Debate Arena")
    doc.build(story)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data" / "uploads" / f"debate-rubric-{date.today().strftime('%Y%m%d')}.pdf"))
    a = ap.parse_args()
    out = build(Path(a.out))
    print(out, out.stat().st_size, "bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
