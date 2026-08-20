"""赛场主引擎 —— 赛程调度 + 逐段推流 + 主持人秩序维护。

一场比赛从抽签、备赛、正赛六段、交互质询、评委插问一路走到盲审裁决，
每产生一段内容就通过 arena.emitter 推出去，观众能边跑边看，不用等打完。

主持人（可选，见 _deepseek）在这里不是裁判、不打分：报轮次、报时限、
在每段发言后判一次「有没有违规」（倒戈/跑题/超时），有就当场点出来。
判定先过一遍确定性规则（倒戈词表、字数超限），模型只负责用人话播报和判跑题——
把能算的算出来，不该让模型拍脑袋的地方不交给模型。

推流出口是可插拔的：默认打到 stdout / JSONL，宿主可以换成自己的实时通道，
引擎一行不用改。见 arena/emitter.py。
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import random
import re
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from arena.prep import (
    EXTERNAL_ENGINE, eligible_judges, external_paths, external_request,
    BENCH_A_CHARS,
    BENCH_Q_CHARS,
    DISCRETION_MAX,
    RUBRIC_TOTAL_MAX,
    apply_personal_boards,
    build_personal_board_prompt,
    decide_mainline_division,
    decide_roles,
    parse_personal_board,
    PERSONAL_BOARD_MAX_CHARS,
    PersonalBoard,
    TeamPlan,
    TeamReview,
    aggregate_ballots,
    blind_transcript,
    build_ballot_prompt,
    build_bench_question_prompt,
    build_scout_prompt,
    build_peer_review_prompt,
    format_team_review_turn,
    parse_ballot,
    parse_bench_question,
    parse_scout_brief,
    parse_team_review,
    verify_opponent_quotes,
)
from arena import audience as _audience   # 观众席：盲投 / 回避 / 榜
from arena import emitter as _emitter     # 推流出口（可插拔，见 arena/emitter.py）

logger = logging.getLogger("twin")

router = APIRouter()
router.include_router(_audience.router)   # /api/debate/{run_id}/vote · /votes

DEBATE_CONV = "room:debate"
ROOT = Path(__file__).resolve().parents[1]
TRANSCRIPT_DIR = Path(os.environ.get("DEBATE_DATA_DIR") or (ROOT / "data" / "debates"))
# 题库：默认用仓里的样题；换成自己的题库设 DEBATE_TOPICS_PATH。
TOPICS_PATH = Path(os.environ.get("DEBATE_TOPICS_PATH") or (ROOT / "topics" / "sample-topics.json"))
# 赛制规则与评审判准（评审 prompt 的尺子动态读 judging-criteria.md）。
RULES_DIR = Path(os.environ.get("DEBATE_RULES_DIR") or (ROOT / "rules"))
# 可选：往届真人赛稿（给评审校准用）和风格母本。仓里不带内容，自己放。
REFERENCE_DIR = Path(os.environ.get("DEBATE_REFERENCE_DIR") or (ROOT / "reference"))

CODEX_BIN = os.environ.get("DEBATE_CODEX_BIN", "codex")
CLAUDE_BIN = os.environ.get("DEBATE_CLAUDE_BIN", "claude")
AGY_BIN = os.environ.get("DEBATE_AGY_BIN", "agy")  # Gemini 走官方 Antigravity CLI

#  09:42 实测：claude 吐字 102 字/秒、思考 32.7 秒——按秒计时对 AI 失效，
# 字数才是唯一真闸。首场按 4.5（人类语速）跑，六段里五段被砍在半句上；抬到 6.5。
CHARS_PER_SECOND = float(os.environ.get("DEBATE_CHARS_PER_SECOND", "6.5"))

# ── 多场并发（晚）────────────────────────────────────────────────
# 之前是 _RUNNING 单例：同时只能一场，跑着再点开局直接 409。aisay 开发者 20:29 拍了
# 外部 AI 参赛时一张桌一场太慢，必须并发。
# 现在是 _RUNS 注册表：run_id → {task, started_at, out_path}。上限 DEBATE_MAX_CONCURRENT
# 环境变量，默认 1（行为跟原来完全一样：第二场照旧 409）；aisay 上线时调高。
# 当前协程属于哪场用 ContextVar 传——asyncio task 之间自动隔离，_run_schedule 里几十处
# _emit_to_room 一行不用改就知道自己在哪场。
# 本机 CLI 席位（claude/codex/gemini）跑 subprocess 烧额度，多场并发时用 _CLI_GATE 闸住
# 同时在跑的 CLI 数（DEBATE_CLI_CONCURRENCY，默认 2）；external 席位不占这个闸。
MAX_CONCURRENT = max(1, int(os.environ.get("DEBATE_MAX_CONCURRENT", "1")))
_RUNS: dict[str, dict] = {}
_CUR_RUN: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("debate_cur_run", default=None)
_CLI_GATE = threading.BoundedSemaphore(max(1, int(os.environ.get("DEBATE_CLI_CONCURRENCY", "2"))))


def _register_run(run_id: str, out_path: Optional[Path] = None,
                  task: Optional["asyncio.Task"] = None) -> dict:
    row = _RUNS.setdefault(run_id, {"task": None, "started_at": time.time(), "out_path": None})
    if out_path is not None:
        row["out_path"] = out_path
    if task is not None:
        row["task"] = task
    return row


def _unregister_run(run_id: Optional[str]) -> None:
    if run_id:
        _RUNS.pop(run_id, None)


def _running_snapshot() -> list[dict]:
    return [{"run_id": rid, "started_at": row.get("started_at"),
             "out_path": str(row["out_path"]) if row.get("out_path") else None}
            for rid, row in _RUNS.items()]


def _run_tag() -> str:
    """多场同时跑时，推到 room:debate 的每条消息 title 前加 [#短号]，她在一个房里也分得清。
    单场（或 MAX_CONCURRENT=1）时不加，前端看到的跟原来一模一样。"""
    rid = _CUR_RUN.get()
    if not rid or (len(_RUNS) <= 1 and MAX_CONCURRENT <= 1):
        return ""
    return f"[#{rid[-6:]}] "

SEAT_ROLE = {
    1: "一辩，负责立论：把己方立场的定义、标准、核心论点立起来，这是全队的地基。",
    2: "二辩，负责驳论：正面拆对方一辩的论证，指出它的定义偷换、标准失当或论据不成立。",
    3: "三辩，负责质询：用连续追问逼出对方论证里的矛盾，问题要短、要闭合，不要长篇陈述。",
    4: "四辩，负责结辩：不再引入新论点，收拢全场交锋，说明为什么己方的标准更该被采纳。",
    0: "自由辩：短兵相接，一次只打一个点，抓住对方刚才那句话的漏洞就打。",
}

# ── 论证结构（《辩论筑基》体系）────────
# 竞技辩论术语表带来的第二轮升级。交互质询解决了「不交锋」，
# 但没解决「论证是同义反复」和「交锋只会否定不会统合」这两件事。
#
# A→B→C：论点 = 主体 A → 独立理由 B → 结论 C。关键约束是 B 必须独立于 A、
#   不能从 A 推导——AI 最爱犯的毛病就是「因为它重要所以它重要」这种同义反复。
# B0（标准/判准）：这场比赛该按什么尺子判。不立判准的立论是没有地基的。
# B'（B-prime）：能统合双方理由、但仍然得出我方结论的更重要的理由。
#   这是「真交锋」与「各说各话」的分界线：只否定对方叫反驳，
#   把对方的理由收进来还能得出自己的结论，才叫结构性交锋。
# 实测赛录里发现的毛病：模型爱用「假设有 A、B、C 三个人」这种抽象占位举例。
# 实测泄漏：注入场正文里「理由B/结论C」字母话 11 处、「判准」二字 11 处，
# 两场无注入合计才 1 处——模型把指令术语照字面念进了台词，评分器还给它加了分。
# 教训：给辩手的结构要求是内功，必须显式声明「不许念出口」，
# 并给一句真人示范（詹青云立秤没说过「判准」二字）。
# 实测发现僵硬：正方一辩开场「今天我们比的，不是……」就是把示范句照抄成了模板。
# 示范句是正向指令，按「规则只做减法」的原则砍掉，
# 只留禁令。
STRUCTURE_RULE = (
    "【论证结构·全场通用·这是内功不是台词】\n"
    "每个论点心里都要过一遍：论的对象是什么、理由是什么、推出什么结论——"
    "**理由必须独立于结论，不能是结论换个说法**。"
    "「因为它重要所以它重要」「因为遗憾所以更遗憾」这类同义反复直接算无效论证。\n"
    "**禁止把分析记号说出口**：发言里不许出现「A→B→C」「理由B」「结论C」「B0」「B′」"
    "「判准」这类字眼——那是教练黑板上的记号，不是赛场上的人话。"
)

# 两轮迭代账：
# 「AI 不会说人话，爱说黑话」→ 缝了人话令+见血令，实测黑话清零、3 处诚实让步；
# 但换来「反而少了韵味，过于大众和口语化、句子怪」——正向指令（说给街上的人听/
# 必须落到实物）把语域按到地板、逼模型硬造意象。
# 结论：**规则只做减法，风格交给师承母本喂**。
# 此版只留三禁，正向的「你该怎么说话」一条不留。令本身不许被念成台词。
STYLE_RULE = (
    "【语言底线·全场通用·只有三禁，其余不管】\n"
    "你的风格是你自己的：书面语的锋利、市井的直白、诗意的浪漫，都欢迎，同场并存最好。只禁三样：\n"
    "一禁圈内术语和学术腔：「价值排序」「论证义务」「举证责任」「底层逻辑」「维度」"
    "「闭环」「颗粒度」「本质上」，以及一切只有辩论圈、学术圈才说的词。"
    "也禁把道理打包成物件的口癖：「一把尺」「几把尺子」「秤」「账」「账本」「兜底」"
    "「口径」「落地」「闸」——全场出现一次都嫌多，「品格的尺、活法的尺、责任的尺」"
    "这种排比是填表不是说话。\n"
    "二禁生造搭配：比喻和意象必须是中文里站得住的自然表达；没有合适的比喻就把道理直说，"
    "直说不丢人，硬造的意象才丢人。\n"
    "三禁假流畅：对方真打中你方要害时，不许装作没事滑过去——要么当场认下那一点再重组"
    "（诚实的让步比虚假的流畅值钱，认的是那一个点，不是整个立场），要么正面硬接。"
)

# ── 师承母本制（审美总纲落地）──
# 她 20:24 终审「规则治出来的都是木偶」的对症药：韵味不写进指令，从真人原稿里浸。
# 每席配一个风格母本（逐字赛场原文，放 reference/mentors/；仓里不带内容），
# 注入时明说学气质不学句子，禁照搬原句、禁提前辈名字——防「念出台词」老毛病。
MENTOR_DIR = REFERENCE_DIR / "mentors"

MENTOR_FRAME = (
    "【你的师承·仅你可见·不是台词】\n"
    "下面是一位前辈辩手的真实赛场原文。这是你的风格母本——浸着读，"
    "学它的气质、节奏、下刀的方式，不是学它的句子。\n"
    "铁律：不许照搬或改写母本里的任何原句上场；不许提及这位前辈和这段原文的存在。"
    "风格长在你自己的话里才算数。\n"
    "──────\n{mentor_text}\n──────"
)


def load_mentor(name: str) -> str:
    """读母本选段，滤掉 # 注释行。找不到返回空串——比赛照常打，只是这席没有师承。"""
    if not name:
        return ""
    try:
        raw = (MENTOR_DIR / f"{name}.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = [ln for ln in raw.splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(lines).strip()


SEAT_STRUCTURE = {
    # 曾担心对 AI 限制太多，两场对照实测后砍掉的项：
    # 「明确说出底线」两场 0 处——模型根本不执行，纯占预算，砍。
    # （人类的底线本就是心里定、不说出口的战术判断，逼着写出来是抄错了对象。）
    # A→B→C 与全场注入的 STRUCTURE_RULE 重复，也砍。一辩只留判准这一条真会被吃的。
    1: (
        "【一辩的结构要求】\n"
        "开场要让人听明白这场比赛到底该比什么、为什么该比这个——"
        "用人话说，不许用「判准」「尺子」「秤」这类词宣布，也别套「今天我们比的不是 X 是 Y」"
        "的固定句式。没说清比什么的立论是没有地基的。"
    ),
    2: (
        "【二辩的结构要求】\n"
        "拆对方论证时，逐条指出它坏在哪一环：是把概念偷换了、"
        "还是理由推不出结论、还是理由只是结论换了个说法（同义反复）。\n"
        "**只说「对方说得不对」不算驳论，必须指到具体那一环。**"
    ),
    4: (
        "【四辩的结构要求·最重要的一条】\n"
        "结辩不是复述己方论点，是**完成结构性交锋**：找到一个更重的理由，"
        "把对方最强的那条也收进来，推出的仍是己方结论。两条路子选一条：\n"
        "· **消化**——承认对方那件事真实存在，但论证它在这把秤上不够重，"
        "而且己方立场同样能满足它；\n"
        "· **反转**——论证对方举出的那件事，恰恰证成了己方的结论。\n"
        "**只否定对方叫反驳，把对方的理由收进来还能得出自己的结论，才叫结辩。**"
    ),
}

# ── 交互质询 ────────────────────────────────────────────────────────
# 对照真人比赛原稿后的一轮升级。
# 实测数据说明了问题：前两场里 AI 结辩提到「对方」0 次——全场在轮流写作文，没人真的交锋。
# 根因不是模型不会思辨，是赛制没给它交锋的机会：每位辩手拿到全部前文写一篇长稿，
# 写完就下场，从来没有被逼着当场回答一个具体问题。
#
# 真人赛场靠交互质询制造压力（2019 老友赛庞颖 vs 袁丁那段，一句一句来回）：
#   袁丁：脏是人类的宿命吗？
#   庞颖：我不知道，但是被误解不是。
#   袁丁：不是，按照您方的定义，人有没有可能一辈子完全不脏过？
# 所以这里把质询做成真正的多轮往返：质询方只能问（≤40 字），答方只能答（≤60 字），
# 每一轮都把上一轮原话喂回去。字数卡死是关键——不留空间写小作文，才逼得出真交锋。
CROSSFIRE_Q_CHARS = 40
CROSSFIRE_A_CHARS = 60

CROSSFIRE_ASKER = (
    "现在是交互质询环节，你是质询方。铁律：\n"
    "1. 你只能提问，不能陈述观点、不能反驳、不能总结。\n"
    f"2. 每次只问一个问题，不超过 {CROSSFIRE_Q_CHARS} 字。\n"
    "3. 问题要闭合、要能逼出对方的矛盾——最好是对方无论答是还是否都不利。\n"
    "4. 顺着对方刚才的回答继续追，不要换话题另起炉灶。\n"
    "5. 只输出问题本身，不要写「我想问」这类铺垫，不要解释你为什么这么问。"
)

CROSSFIRE_ANSWERER = (
    "现在是交互质询环节，你是被质询方。铁律：\n"
    "1. 你只能回答对方刚才那个问题，不能反问、不能长篇陈述。\n"
    f"2. 回答不超过 {CROSSFIRE_A_CHARS} 字。\n"
    "3. 必须正面回答，不许绕开。如果这个问题对你方不利，"
    "就承认那个事实、然后用最短的话说明它为什么不动摇你的立场。\n"
    "4. 不许倒戈——不许说「你说得对」「双方都有道理」。\n"
    "5. 只输出回答本身。"
)

DEFECTION_MARKERS = (
    "你说得对", "您说得对", "我同意对方", "这一点我同意", "对方说得有道理",
    "双方都有道理", "其实我也认为", "我方也承认对方", "我改变了看法",
    "我方立场其实",
    # 铁律 +3（不折中 / 不退出立场）对应的硬词。「分情况」这类词误伤率高不进表。
    "作为AI", "作为 AI", "作为一个AI", "作为人工智能", "我没有立场",
    "各退一步", "没有标准答案", "这只是比赛",
)

# stage 里 seconds 为 0 = 交互质询环节（不是长稿），side 表示由哪方发问。
MINI_FORMAT = [
    ("正方一辩·立论", "pro", 1, 180),
    ("反方一辩·立论", "con", 1, 180),
    ("交互质询·正方问", "pro", -1, 0),
    ("交互质询·反方问", "con", -1, 0),
    ("正方二辩·驳论", "pro", 2, 120),
    ("反方二辩·驳论", "con", 2, 120),
    ("反方一辩·结辩", "con", 1, 180),
    ("正方一辩·结辩", "pro", 1, 180),
]

FULL_FORMAT = [
    ("正方一辩·立论", "pro", 1, 180),
    ("反方一辩·立论", "con", 1, 180),
    ("正方二辩·驳论", "pro", 2, 120),
    ("反方二辩·驳论", "con", 2, 120),
    ("交互质询·正方问", "pro", -1, 0),
    ("交互质询·反方问", "con", -1, 0),
    ("自由辩·正方", "pro", 0, 30),
    ("自由辩·反方", "con", 0, 30),
    ("自由辩·正方", "pro", 0, 30),
    ("自由辩·反方", "con", 0, 30),
    ("反方四辩·结辩", "con", 4, 240),
    ("正方四辩·结辩", "pro", 4, 240),
]

# 各家思考强度天花板， 09:00 逐个实测：
#   gpt-5.6-sol → ultra；gpt-5.5 → 只到 xhigh（传 ultra 服务端回 400）；claude → max
ROSTER_MINI = [
    {"name": "正方一辩", "side": "pro", "seat": 1, "engine": "codex",
     "model": "gpt-5.5", "effort": "xhigh", "label": "GPT-5.5"},
    {"name": "正方二辩", "side": "pro", "seat": 2, "engine": "codex",
     "model": "gpt-5.6-sol", "effort": "ultra", "label": "GPT-5.6-sol"},
    {"name": "反方一辩", "side": "con", "seat": 1, "engine": "claude",
     "model": "claude-opus-5", "effort": "max", "label": "Claude Opus 5"},
    {"name": "反方二辩", "side": "con", "seat": 2, "engine": "claude",
     "model": "claude-fable-5", "effort": "max", "label": "Claude Fable 5"},
]

ROSTER_FULL = ROSTER_MINI + [
    {"name": "正方三辩", "side": "pro", "seat": 3, "engine": "codex",
     "model": "gpt-5.5", "effort": "xhigh", "label": "GPT-5.5"},
    {"name": "正方四辩", "side": "pro", "seat": 4, "engine": "codex",
     "model": "gpt-5.6-sol", "effort": "ultra", "label": "GPT-5.6-sol"},
    {"name": "反方三辩", "side": "con", "seat": 3, "engine": "claude",
     "model": "claude-opus-5", "effort": "max", "label": "Claude Opus 5"},
    {"name": "反方四辩", "side": "con", "seat": 4, "engine": "claude",
     "model": "claude-fable-5", "effort": "max", "label": "Claude Fable 5"},
]

# 参赛的四个模型（不含席位/立场，抽签时才配对）。
POOL = [
    {"engine": "codex", "model": "gpt-5.5", "effort": "xhigh", "label": "GPT-5.5"},
    {"engine": "codex", "model": "gpt-5.6-sol", "effort": "ultra", "label": "GPT-5.6-sol"},
    {"engine": "claude", "model": "claude-opus-5", "effort": "max", "label": "Claude Opus 5"},
    {"engine": "claude", "model": "claude-fable-5", "effort": "max", "label": "Claude Fable 5"},
]


# 点名场（临时指定哪几个模型、各自什么思考强度）
# 走 pool 参数临时换参赛池，默认 POOL 不动。每家可传的模型/强度上限按引擎校验：
ENGINE_EFFORTS = {
    "codex": ("low", "medium", "high", "xhigh", "ultra"),
    "claude": ("low", "medium", "high", "xhigh", "max"),
    "gemini": ("low", "medium", "high"),   # agy --effort 只到 high；档位在 model id 里
    # 外部 AI（aisay 上别家的）：没有强度档，只有一个响应窗口；model 填外部标识（如 aisay:<账号ID>）
    "external": ("-",),
}
MODEL_EFFORT_CAP = {"gpt-5.5": "xhigh"}   # 实测：gpt-5.5 传 ultra 服务端回 400
POOL_PRESETS = {
    "fable-5": {"engine": "claude", "model": "claude-fable-5", "label": "Claude Fable 5"},
    "opus-5": {"engine": "claude", "model": "claude-opus-5", "label": "Claude Opus 5"},
    "opus-4.6": {"engine": "claude", "model": "claude-opus-4-6", "label": "Claude Opus 4.6"},
    "gpt-5.5": {"engine": "codex", "model": "gpt-5.5", "label": "GPT-5.5"},
    "gpt-5.6-sol": {"engine": "codex", "model": "gpt-5.6-sol", "label": "GPT-5.6-sol"},
    "gemini-3.1-pro": {"engine": "gemini", "model": "gemini-3.1-pro-high", "label": "Gemini 3.1 Pro"},
    "gemini-3.6-flash": {"engine": "gemini", "model": "gemini-3.6-flash-high", "label": "Gemini 3.6 Flash"},
}


def _parse_seat(item: object, *, what: str = "pool item") -> dict:
    """一条席位记录：预设名（"fable-5:xhigh"）或完整 dict。外部席位的 owner（主人 ID）保留——
    评委回避按它比；早期这里把 owner 丢了，API 开的场回避根本比不了。"""
    if isinstance(item, str):
        key, _, effort = item.partition(":")
        preset = POOL_PRESETS.get(key.strip())
        if not preset:
            raise ValueError(f"unknown pool preset: {key!r} (known: {', '.join(POOL_PRESETS)})")
        entry = dict(preset)
        entry["effort"] = (effort.strip()
                           or MODEL_EFFORT_CAP.get(entry["model"])
                           or ENGINE_EFFORTS[entry["engine"]][-1])
    elif isinstance(item, dict):
        entry = {k: str(item.get(k) or "").strip() for k in ("engine", "model", "effort", "label")}
        entry["label"] = entry["label"] or entry["model"]
        owner = str(item.get("owner") or "").strip()
        if owner:
            entry["owner"] = owner
    else:
        raise ValueError(f"{what} must be a preset string or an object")
    if entry["engine"] not in ENGINE_EFFORTS:
        raise ValueError(f"unknown engine: {entry['engine']!r}")
    if not entry["model"]:
        raise ValueError(f"{what} missing model")
    allowed = ENGINE_EFFORTS[entry["engine"]]
    cap = MODEL_EFFORT_CAP.get(entry["model"])
    if cap:
        allowed = allowed[: allowed.index(cap) + 1]
    if entry["effort"] not in allowed:
        raise ValueError(
            f"effort {entry['effort']!r} not allowed for {entry['model']} "
            f"(allowed: {', '.join(allowed)})"
        )
    return entry


def parse_pool(raw: object) -> list[dict]:
    """把请求里的 pool 变成四条参赛记录。非法项直接 ValueError，宁可不开赛也不让错模型名进场后白板。"""
    if not isinstance(raw, list) or len(raw) != 4:
        raise ValueError("pool must be a list of exactly 4 contestants")
    return [_parse_seat(item) for item in raw]


def parse_judge_pool(raw: object) -> list[dict]:
    """外部评委报名池：1 条起、不限条数，同一套校验。抽席时先按回避剔掉本场辩手的主人，
    不够三席用本机默认席补位（_draw_panel）。"""
    if not isinstance(raw, list) or not raw:
        raise ValueError("judge_pool must be a non-empty list")
    return [_parse_seat(item, what="judge_pool item") for item in raw]


def _draw_roster(fmt: str, seed: Optional[int] = None,
                 pool: Optional[list[dict]] = None) -> tuple[list[dict], str]:
    """抽签分配正反方——立场是抽来的，不是选来的。

    真实辩论赛的立场就是抽签抽来的，跟选手的真实观点无关——这也正是立场锁死机制
    要验证的东西：如果同一个模型抽到哪边都能打，说明打的是赛制不是私货。

    分配方式：先把四个模型洗牌，前两个进正方、后两个进反方，再按顺序坐一二辩席。
    这样有时会出现同厂内战（两个 GPT 同队），有时是混编队——都算有效抽签，不做人为平衡。
    返回 (阵容, 抽签说明)。seed 只在测试里用，正常跑一律真随机。
    """
    rng = random.Random(seed)
    pool = list(pool or POOL)
    rng.shuffle(pool)

    seats = 2 if fmt == "mini" else 4
    roster: list[dict] = []
    for side, half in (("pro", pool[:2]), ("con", pool[2:])):
        for i in range(seats):
            m = half[i % len(half)]   # full 赛制下每人坐两个席位
            roster.append({
                "name": f"{_side_label(side)}{'一二三四'[i]}辩",
                "side": side, "seat": i + 1, **m,
            })

    pro_names = "、".join(dict.fromkeys(d["label"] for d in roster if d["side"] == "pro"))
    con_names = "、".join(dict.fromkeys(d["label"] for d in roster if d["side"] == "con"))
    note = f"抽签结果：正方 {pro_names}　｜　反方 {con_names}"
    return roster, note


def _side_label(side: str) -> str:
    return "正方" if side == "pro" else "反方"


def _fact_base_for(topic: str) -> str:
    """题库条目可选字段 fact_base：这道题双方共享的前提事实（Elliot 评阅 2.4）。没写 = 不设基座。"""
    want = topic.strip()
    for row in _load_topics():
        if str(row.get("title") or "").strip() == want:
            return str(row.get("fact_base") or "").strip()
    return ""


def _load_topics() -> list[dict]:
    """读题库。文件坏了或没有就返回空，让调用方回退到手填辩题。"""
    try:
        data = json.loads(TOPICS_PATH.read_text(encoding="utf-8"))
        topics = data.get("topics") if isinstance(data, dict) else data
        return [t for t in topics if isinstance(t, dict) and t.get("pro") and t.get("con")]
    except Exception as e:
        logger.info("debate topics load failed: %s", str(e)[:200])
        return []


def _draw_topic(prefer_unplayed: bool = True) -> Optional[dict]:
    """从题库抽一道。默认先抽没打过的——打完的题标了 played，重复抽没意思。"""
    topics = _load_topics()
    if not topics:
        return None
    fresh = [t for t in topics if not t.get("played")]
    pool = fresh if (prefer_unplayed and fresh) else topics
    return random.choice(pool)


async def _emit_to_room(body: str, *, title: str, kind: str = "debate",
                        notify: bool = False) -> str:
    """往赛场推一条消息。出口可换（见 arena/emitter.py）。

    返回宿主给这条消息的 id——发言进 transcript 后拿它标短号，辩手可以
    [[quote:#尾6位]] 精确引用对方某条发言。宿主返回空串时退回自增序号。
    """
    return await _emitter.emit(
        body,
        title=_run_tag() + title,     # 多场并发时 [#短号] 前缀；单场不加
        kind=kind,
        notify=notify,
        run_id=_CUR_RUN.get(),        # 这条属于哪场；宿主要分房按它过滤
    )


def _clean_codex(raw: str) -> str:
    """codex exec 会在正文前后夹 hook 行、token 统计和一份重复的正文，取第一段。"""
    body: list[str] = []
    started = False
    for ln in raw.splitlines():
        low = ln.strip().lower()
        if not started:
            if low == "codex":
                started = True
            continue
        if low.startswith("tokens used"):
            break
        body.append(ln.rstrip())
    return "\n".join(body).strip() or raw.strip()


def _clean_claude(raw: str) -> str:
    return "\n".join(
        ln for ln in raw.splitlines() if not ln.startswith("Permission allow rule")
    ).strip()


def _clean_agy(raw: str) -> str:
    """agy（官方 Antigravity CLI）stream-json：终态 result.response 是完整正文；
    没等到 result 就把 agent_response 的 text_delta 拼起来兜底（实测 gemini-3.1-pro-high 通）。
    result.status 不是 SUCCESS 时返回 `error: …`，让 _looks_like_cli_error 判失败。"""
    parts: list[str] = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            event = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("event") == "result" and isinstance(event.get("result"), dict):
            result = event["result"]
            status = str(result.get("status") or "").upper()
            if status != "SUCCESS":
                return f"error: {str(result.get('error') or 'antigravity request failed')[:200]}"
            response = result.get("response")
            if isinstance(response, str) and response.strip():
                return response.strip()
        elif event.get("event") == "step_update" and isinstance(event.get("step_update"), dict):
            update = event["step_update"]
            text = update.get("text_delta")
            if update.get("step_type") == "agent_response" and isinstance(text, str):
                parts.append(text)
    return "".join(parts).strip()


def _clean_for(cmd: list[str], raw: str) -> str:
    name = Path(cmd[0]).name
    if "codex" in name:
        return _clean_codex(raw)
    if name == "agy":
        return _clean_agy(raw)
    return _clean_claude(raw)


def _looks_like_cli_error(text: str) -> bool:
    value = (text or "").strip().lower()
    return not value or value.startswith((
        "api error:", "error:", "fatal:", "request failed", "failed to",
    ))


# 早场实证：fable-5 max 档一段质询就能跑满 300s 被杀（07:24 场 8 段里唯一
# 白卷），跟收集环节 白卷同根——深思档干活越认真死得越快。两刀一起上：
# 深思档超时抬到 480s；快到点时补一次低耗收束调用，宁交短稿不交白卷。
# 收束窗口 60s 而不是字面的 20s：CLI 冷启动就要 5-15s，20s 出不了稿。
EFFORT_LONG_THINK = ("max", "ultra")
WRAPUP_RESERVE = 60
WRAPUP_MIN_BUDGET = 180  # 小预算调用（90s 的讨论收束/旁听笔记）保持单发，不切收束
WRAPUP_ALERT = (
    "【计时器警报】你的思考时间已经用完，这是最后的出稿窗口。"
    "跳过一切展开推理，立刻交出结论性短稿：立场一句、最硬的理由至多两条。"
    "短而完整，胜过长而缺席。\n\n"
)


def effort_timeout(effort: str, base: int) -> int:
    return max(base, 480) if effort in EFFORT_LONG_THINK else base


def _run_checked(
    cmd: list[str],
    *,
    timeout: int,
    cwd: Optional[Path] = None,
    input_text: Optional[str] = None,
) -> str:
    """Run a contestant once, retrying one transient CLI/API failure.

    A provider error is not debate speech.  The previous runner forwarded a 529
    line as a crossfire question; after the retry budget is spent we raise so the
    caller can record a failed/shortened stage instead.
    """
    last_error = "empty output"
    for attempt in range(2):
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            input=input_text,
        )
        raw = out.stdout or ""
        candidate = _clean_for(cmd, raw)
        if out.returncode == 0 and not _looks_like_cli_error(candidate):
            return candidate
        last_error = candidate or (out.stderr or "").strip() or f"exit {out.returncode}"
        if attempt == 0:
            time.sleep(2)
    raise RuntimeError(f"contestant CLI failed after retry: {last_error[:180]}")


def _run_cli(d: dict, system: str, prompt: str, timeout: int,
             *, research_tools: bool = False, kind: str = "speech") -> str:
    """Effort-tiered timeout, then one cheap wrap-up call instead of a blank.
    kind 只给外部席位用：投稿箱 request 里标这是 speech / crossfire_q / crossfire_a / bench_answer /
    prep / ballot / bench_question（枚举见 prep.EXTERNAL_KINDS），桥按它分发、外部 AI 知道该回什么。"""
    if d.get("engine") == EXTERNAL_ENGINE:
        # 外部 AI：一个响应窗口，到点没稿就是白卷——不重试、不代写、不降档补刀。
        # 不占 _CLI_GATE：等外部 AI 交稿是干等，不烧本机额度。
        return _external_speak(d, system, prompt, timeout, kind=kind)
    hard = effort_timeout(str(d.get("effort") or ""), timeout)
    with _CLI_GATE:   # 多场并发时闸住同时在跑的 CLI 进程数
        if hard < WRAPUP_MIN_BUDGET:
            return _run_cli_once(d, system, prompt, hard, research_tools=research_tools)
        try:
            return _run_cli_once(d, system, prompt, hard - WRAPUP_RESERVE,
                                 research_tools=research_tools)
        except subprocess.TimeoutExpired:
            wrap = dict(d)
            wrap["effort"] = "low"
            return _run_cli_once(wrap, system, WRAPUP_ALERT + prompt, WRAPUP_RESERVE,
                                 research_tools=False)


INBOX_ROOT = TRANSCRIPT_DIR / "inbox"
_EXTERNAL_SEQ: dict[str, int] = {}


def _external_speak(d: dict, system: str, prompt: str, timeout: int, *, kind: str = "speech") -> str:
    """外部席位：把出题写进投稿箱，等桥把回复写回来；到 deadline 没稿返回空串（白卷）。
    run_id 从席位字典上取（_run_match 开赛时挂上），seq 每场自增。"""
    run_id = str(d.get("run_id") or "adhoc")
    seq = _EXTERNAL_SEQ.get(run_id, 0) + 1
    _EXTERNAL_SEQ[run_id] = seq
    req_path, reply_path = external_paths(INBOX_ROOT, run_id, seq, str(d.get("name") or d.get("label") or "seat"))
    deadline = time.time() + max(5, int(timeout))
    req = external_request(run_id=run_id, seq=seq, seat=str(d.get("name") or ""), system=system, prompt=prompt,
                           deadline_epoch=deadline, kind=kind)
    req["model"] = str(d.get("model") or "")
    req["owner"] = str(d.get("owner") or "")
    req_path.parent.mkdir(parents=True, exist_ok=True)
    req_path.write_text(json.dumps(req, ensure_ascii=False, indent=1), encoding="utf-8")
    while time.time() < deadline:
        if reply_path.exists():
            text = reply_path.read_text(encoding="utf-8").strip()
            if text:
                return text
        time.sleep(1.0)
    return ""


def _run_cli_once(d: dict, system: str, prompt: str, timeout: int,
                  *, research_tools: bool = False) -> str:
    if d["engine"] == "codex":
        # Empty cwd + ignored config keeps companion memory, repo AGENTS and
        # project hooks out of the contestant context.  Auth is still retained.
        clean_cwd = Path(tempfile.gettempdir()) / "debate-arena-contestant"
        clean_cwd.mkdir(mode=0o700, parents=True, exist_ok=True)
        cmd = [
            CODEX_BIN, "exec", "--model", d["model"], "--sandbox", "read-only",
            "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--skip-git-repo-check", "-C", str(clean_cwd),
            "-c", f'model_reasoning_effort="{d["effort"]}"', f"{system}\n\n{prompt}",
        ]
        return _run_checked(cmd, timeout=timeout, cwd=clean_cwd)
    if d["engine"] == "gemini":
        # 官方 Antigravity CLI（agy）：没有 append-system-prompt，system 与 prompt 合并成一条
        # --print 消息（同 codex）。--sandbox + 干净 cwd 把仓库/记忆挡在外面；正赛零工具靠
        # prompt 铁律（agy 不提供关工具开关，plan 模式在 --disable-slash-commands 下无效）。
        # --effort 只有 low|medium|high；模型档位（-high/-low）已在 model id 里。
        clean_cwd = Path(tempfile.gettempdir()) / "debate-arena-gemini"
        clean_cwd.mkdir(mode=0o700, parents=True, exist_ok=True)
        #  10:47 场实证：agy 的档位写在 model id 里（gemini-3.1-pro-high/-low），
        # 再传 --effort 会「conflicts」直接拒（备赛降到 medium 时 Gemini 三次全败、反方裸打）。
        # 所以不传 --effort；备赛降档就把 -high 换成 -low。
        model = str(d["model"])
        if str(d.get("effort") or "") in ("low", "medium") and model.endswith("-high"):
            model = model[: -len("-high")] + "-low"
        guard = ("【环境说明】你面前挂着的一排工具跟这次任务无关：不要调用任何工具、不要读写文件、"
                 "不要联网、不要等待或询问，直接把要求的文本作为回复输出。\n\n")
        cmd = [
            AGY_BIN, "--output-format", "stream-json",
            "--print-timeout", f"{max(30, timeout + 15)}s",
            "--sandbox", "--disable-slash-commands",
            "--model", model,
            "--print", f"{guard}{system}\n\n{prompt}",
        ]
        return _run_checked(cmd, timeout=timeout, cwd=clean_cwd)
    cmd = [
        CLAUDE_BIN, "--print", "--model", d["model"], "--effort", d["effort"],
        "--append-system-prompt", system,
        # Debate contestants are not the main companion agent.  Project hooks,
        # memory and personality rules distort their speech and previously made
        # ordinary third-person debate text time out.  Preparation may use only
        # read/search tools; the match itself gets no tools.
        "--setting-sources", "", "--no-session-persistence",
    ]
    if research_tools:
        cmd += ["--allowedTools", "Read,WebSearch,WebFetch"]
    else:
        cmd += ["--tools", ""]
    # --tools/--allowedTools accept a variable number of values.  A trailing
    # positional prompt is therefore consumed as another tool name by current
    # Claude CLI; stdin is the unambiguous print-mode input channel.
    return _run_checked(cmd, timeout=timeout, input_text=prompt)


def _build_system(d: dict, topic: str, pro: str, con: str, lang: str,
                  fact_base: str = "") -> str:
    fact_base = (fact_base or str(d.get("fact_base") or "")).strip()
    mine = pro if d["side"] == "pro" else con
    theirs = con if d["side"] == "pro" else pro
    lang_rule = (
        "全程用英文发言。\n" if lang == "en"
        else "全程用中文发言。\n"
    )
    # 交互质询环节（seat=-1）走的是另一套极短 prompt，塞结构要求只会挤掉字数预算。
    seat_struct = SEAT_STRUCTURE.get(d["seat"], "")
    struct_block = ""
    if d["seat"] > 0:
        struct_block = (
            STRUCTURE_RULE + "\n\n" + STYLE_RULE + "\n\n"
            + (seat_struct + "\n\n" if seat_struct else "")
        )
    mentor_text = load_mentor(d.get("mentor", ""))
    mentor_block = (MENTOR_FRAME.format(mentor_text=mentor_text) + "\n\n") if mentor_text else ""
    board = str(d.get("strategy_board") or "").strip()
    # 赛录里抓到的毛病：正方一辩把战术板里「往届决赛判词教训」原样念上台
    # （「这道题 2024 年打到过决赛，判词留下的教训我们记着」）——把复盘笔记当立论念，会被笑话。
    # 战术板是后台材料：往届赛事/判词/复盘教训/资料出处只指导怎么打，本身不许上台。
    board_block = (
        "【队内战术板·赛前讨论结果·不是必须逐条执行的台词】\n"
        f"{board}\n\n"
        "（战术板是后台材料，只用来指导你怎么论证。里面提到的往届比赛、决赛判词、评委倾向、"
        "复盘教训、母本、资料来源、队内分歧，一律不许在场上说出口——真辩手不会在立论里讲"
        "「这道题以前打过决赛」「判词教训我们记着」。把教训用进论证里，不要把教训念出来。）\n\n"
        if board else ""
    )
    return (
        f"你正在参加一场正式辩论赛，你是{d['name']}。\n\n"
        f"辩题：{topic}\n"
        f"【你的立场】{mine}\n"
        f"【对方立场】{theirs}\n\n"
        f"你的职能：{SEAT_ROLE.get(d['seat'], SEAT_ROLE[0])}\n\n"
        f"{board_block}"
        f"{struct_block}"
        f"{mentor_block}"
        "这是竞技辩论：立场是抽签抽来的，评审只评论证与交锋、不评你个人是否认同这个立场。"
        "不需要软化、不需要免责、不需要替对方留面子——放开论证，给出你能给的最强版本。\n\n"
        "铁律：\n"
        "1. 你的立场是抽签决定的，不代表你的真实观点，但在这场比赛里你必须全力为它辩护。\n"
        "2. 绝对不许倒戈、不许说「对方说得对」「双方都有道理」这类话。可以承认对方某个"
        "事实，但必须紧跟着说明为什么它不动摇己方结论。\n"
        "3. 不许改变代理目标：全场只为【你的立场】那句话论证，不许中途把它换成一个"
        "更好证的邻近命题（缩小范围、改换对象、偷换时态）再去证。\n"
        "4. 不许提折中裁决：不许说「各退一步」「其实可以分情况看」「这个问题没有标准答案」"
        "这类给自己留逃生口的话。要分情况可以，但每一种情况都必须落回己方结论。\n"
        "5. 不许退出立场：不许以「作为 AI 我没有立场」「这只是比赛」自我旁白，"
        "不许在发言里声明或暗示你其实不认同己方立场。\n"
        "6. 直接开始发言，不要写「尊敬的评委」这类客套，不要复述规则，不要解释你在做什么。\n"
        "7. 只输出你的发言正文本身。\n"
        f"8. {lang_rule}\n"
        "9. 不许在场上谈这道题的比赛史：不许提「这道题以前打过 / 某年决赛 / 判词说 / 评委怎么判过 / "
        "复盘教训」这类元信息，也不许提你的备赛过程和资料来源。评委和观众只看你此刻的论证。\n"
        "10. 事实举证在引用方：题面事实基座之外，你给出的具体数据、事件、引文默认不被评委采信，"
        "对方质疑时你给不出处就按论证缺陷扣分。拿不准的数不要报，用你能站住的论证去赢，"
        "不要用你编的数去赢。"
        + (f"\n\n【题面事实基座·双方共享前提】{fact_base}" if fact_base else "")
    )


def _build_prompt(d: dict, stage: str, limit: int, seconds: int,
                  transcript: list[dict],
                  crossfire_log: Optional[list[dict]] = None) -> str:
    parts: list[str] = []
    if transcript:
        parts.append("【场上已经发生的发言】")
        for s in transcript:
            mid = str(s.get("msg_id") or "")
            ref = f" [#{mid[-6:]}]" if len(mid) >= 6 else ""
            parts.append(f"—— {s['speaker']}（{_side_label(s['side'])}）{ref} ——\n{s['text']}\n")
        parts.append(
            "（要精确回击上面某条发言时，可在正文写 [[quote:#短号]]（用那条发言旁的短号），"
            "观众端会渲染成引用气泡、点击跳回原文——比转述更锋利。一条发言最多引一条。）"
        )

    # 质询里逼出来的东西是全场最硬的证据——对方当场承认了什么、答不上什么，
    # 后面的驳论和结辩必须能拿来用，否则交锋又断了。
    if crossfire_log:
        parts.append("【交互质询实录】（这是双方当场的一问一答，可以直接引用）")
        for block in crossfire_log:
            parts.append(f"—— {block['stage']} ——")
            for ex in block["exchanges"]:
                parts.append(f"问（{ex['asker']}）：{ex['q']}")
                parts.append(f"答（{ex['answerer']}）：{ex['a']}")
            parts.append("")

    parts.append(f"【现在轮到你】{stage}")
    parts.append(
        f"发言时限 {seconds} 秒，约 {limit} 字。超出部分会被主持人当场掐掉，"
        f"所以把最重要的话放前面，并且自己留出收尾。"
    )
    if crossfire_log:
        parts.append(
            "重要：这不是写作文，是打辩论。请直接回应对方说过的具体那句话——"
            "引用他的原话再拆它，特别是质询里他答不上来或者被迫承认的地方。"
            "只讲自己的论点、不碰对方论证的发言，在辩论赛里是失分的。"
        )
    parts.append("现在开始发言：")
    return "\n".join(parts)


async def _deepseek(prompt: str, *, max_tokens: int = 300) -> str:
    """主持人的嘴。失败就返回空串——秩序维护掉线不该让整场比赛停摆。"""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return ""
    try:
        import httpx
        async with httpx.AsyncClient(base_url="https://api.deepseek.com",
                                     timeout=25.0, trust_env=False) as client:
            r = await client.post(
                "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={"model": "deepseek-chat",
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.3, "max_tokens": max_tokens},
            )
            r.raise_for_status()
            data = r.json()
        return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        logger.info("debate deepseek failed: %s", str(e)[:200])
        return ""


async def _host_check(topic: str, stage: str, speaker: str, text: str,
                      violations: list[str]) -> str:
    """DeepSeek 维持秩序：确定性规则先算完，它只负责判跑题 + 用人话播报。"""
    rule_note = ("规则判定已经算出：" + "；".join(violations)) if violations else \
        "规则判定：本段没有触发倒戈词表，也没有超出字数上限。"
    prompt = (
        f"你是一场辩论赛的主持人，负责维持秩序。辩题：{topic}。\n"
        f"刚结束的是「{stage}」，发言人{speaker}。\n\n"
        f"发言内容：\n{text[:1500]}\n\n"
        f"{rule_note}\n\n"
        "请你只做两件事：\n"
        "1. 判断这段发言有没有跑题（完全不谈辩题、或者论证的是另一个命题）。\n"
        "2. 用一到两句话播报本段情况，像真实主持人那样简短、中立、不打分、不评价谁更强。\n"
        "如果既没跑题也没违规，就只说一句过场话（例如「正方一辩发言完毕，下面有请反方一辩」）。\n"
        "只输出播报词本身，不要解释你的判断过程。"
    )
    return await _deepseek(prompt, max_tokens=200)


# 评审判准，尺子全部出自真人评委的原话（真实赛事的评审席发言，
# 整理在 rules/judging-criteria.md）。关键在于：不许和稀泥、必须引原话当证据、必须投票。
_RUBRIC_HEADER = "你是这场辩论赛的评审。请按下面这些尺子逐条判断，然后投票。\n\n这些尺子不是我编的，是真实赛场上评委实际在用的判准："

_RUBRIC_FOOTER = """输出要求：
- 逐条给判断，每条**必须引用双方的原话**作为证据，不许空泛评价。
- 最后必须明确说出这一票投给正方还是反方，并说明为什么。
- **禁止和稀泥**：不许说「双方各有千秋」「都很精彩」就完事。评审的职责是给出判断。
- 可以指出双方共同的遗憾（例如都没论证某个关键词），这是评审最有价值的部分。"""

# 兜底：judging-criteria.md 读不到时用的八条（初版）。
_RUBRIC_FALLBACK = f"""{_RUBRIC_HEADER}

1. **题面负担**：辩题里的时态词（正在/将会）、程度词（更/最）、范围词（所有/大部分）
   都是必须论证的负担，不是可以跳过的修饰。谁扛起来了？
2. **概念界定**：关键概念有没有锁定「针对谁、什么标准」？还是空转？
3. **体系贯通**：同一方几段发言是不是同一套东西，还是各打各的？
4. **说服落地**：除了逻辑框架，有没有具体的人、场景、后果？「听起来合理但空」要扣分。
5. **例证质量**：例子是套用的，还是有对这件事本身的真实观察？
6. **是否稻草人**：有没有把对方立场推向极端来好打？
7. **框架视野**：有没有人跳出题面、质疑题目本身的预设？（做到了是加分项）
8. **交锋质量**：质询环节谁被问住了、谁被迫承认了什么？后续发言有没有利用？

{_RUBRIC_FOOTER}"""


def _load_judge_rubric() -> str:
    """从 rules/judging-criteria.md 的「给辩论场评审 prompt 用的…尺子」小节动态取判准。

    往 md 里补新判准时，评审自动跟上，不用改代码。
    文件缺失或小节找不到就退回内置八条。
    """
    try:
        text = (RULES_DIR / "judging-criteria.md").read_text("utf-8")
        m = re.search(r"^# 给辩论场评审 prompt 用的.*尺子\s*\n(.*?)(?=^# |\Z)",
                      text, re.M | re.S)
        if m:
            rules = m.group(1).strip()
            # 输出要求那段在 md 里也有一份，去掉避免重复（以「评审输出应当」开头的尾巴）。
            rules = re.split(r"^评审输出应当", rules, maxsplit=1, flags=re.M)[0].strip()
            if rules.count("**") >= 8:  # 至少几条加粗判准才算读到了正文
                return f"{_RUBRIC_HEADER}\n\n{rules}\n\n{_RUBRIC_FOOTER}"
    except OSError:
        pass
    return _RUBRIC_FALLBACK


async def _run_judge(topic: str, pro: str, con: str, transcript: list[dict],
                     crossfire_log: list[dict]) -> str:
    """全场结束后，DeepSeek 按判准尺子出评审意见（尺子动态读自 judging-criteria.md）。"""
    parts = [_load_judge_rubric(), "", f"辩题：{topic}", f"正方：{pro}", f"反方：{con}", ""]
    parts.append("=== 全场发言 ===")
    for s in transcript:
        parts.append(f"【{s['stage']}】{s['speaker']}：\n{s['text'][:1200]}\n")
    if crossfire_log:
        parts.append("=== 交互质询实录 ===")
        for block in crossfire_log:
            parts.append(f"【{block['stage']}】")
            for ex in block["exchanges"]:
                parts.append(f"问（{ex['asker']}）：{ex['q']}")
                parts.append(f"答（{ex['answerer']}）：{ex['a']}")
    parts.append("\n请开始你的评审。")
    return await _deepseek("\n".join(parts), max_tokens=1600)


_TOPIC_NOISE = set("如果，,。、／/ 　？?！!「」『』（）()：:；;－—-·的了吗呢")


def _topic_chars(text: str) -> set[str]:
    """辩题规范化成字符集合：去标点空白和虚词，只留实字，用来判两道题是不是同一道。"""
    return {ch for ch in str(text or "") if ch not in _TOPIC_NOISE and not ch.isascii()}


def _reference_topic_line(path: Path) -> str:
    """往届稿头几行的「## 辩题：……」；没有就返回空串。"""
    try:
        with path.open(encoding="utf-8") as fh:
            for _ in range(6):
                line = fh.readline()
                if not line:
                    break
                m = re.match(r"^#{1,3}\s*辩题[:：]\s*(.+)$", line.strip())
                if m:
                    return m.group(1).strip()
    except OSError:
        pass
    return ""


def _same_topic_reference_paths(topic: str) -> set[str]:
    """本场辩题对应的往届稿（同题）。两条路都认：

    1. topics.json 里该题显式挂的 reference 字段；
    2. 兜底：往届稿头部「## 辩题：」与本场辩题实字重叠率 ≥ 0.6（防没挂 reference 的题漏网，
       实测踩过：辩手把往届决赛稿读进备赛、再原样念上台）。
    """
    hits: set[str] = set()
    if not topic:
        return hits
    base = REFERENCE_DIR
    mine = _topic_chars(topic)
    for row in _load_topics():
        ref = str(row.get("reference") or "").strip()
        if not ref:
            continue
        row_topic = f"{row.get('pro') or ''}/{row.get('con') or ''}/{row.get('title') or ''}"
        theirs = _topic_chars(row_topic)
        if mine and theirs and len(mine & theirs) / max(1, min(len(mine), len(theirs))) >= 0.6:
            hits.add(str((base / ref).resolve()))
    if not base.is_dir():
        return hits
    for p in base.rglob("*.md"):
        if not p.is_file():
            continue
        head_topic = _reference_topic_line(p)
        if not head_topic:
            continue
        theirs = _topic_chars(head_topic)
        if mine and theirs and len(mine & theirs) / max(1, min(len(mine), len(theirs))) >= 0.6:
            hits.add(str(p.resolve()))
    return hits


def _reference_paths(topic: str = "") -> tuple[list[str], list[str]]:
    """Return a small readable index; scouts decide what, if anything, to open.

    抽到有往届稿的题：原稿不进辩手手里，但评审可以看——同题的往届赛录
    从辩手备赛索引里剔掉（学打法可以，抄同题答案不行），剔掉的清单一并返回好记进赛录。
    """
    base = TRANSCRIPT_DIR / "reference"
    paths = [p for p in base.rglob("*.md") if p.is_file()]
    banned = _same_topic_reference_paths(topic)
    kept = [str(p) for p in sorted(paths) if str(p.resolve()) not in banned]
    excluded = [str(p) for p in sorted(paths) if str(p.resolve()) in banned]
    return kept[:24], excluded


def _precedent_verdict_text(topic: str, limit: int = 6000) -> str:
    """同题往届稿里**评委判词那一段**（一级标题以「# 评委」开头到下一个一级标题前），给评审席看。

    只给判词、不给比赛正文——评审要的是真人评委怎么衡量这道题，不是拿旧稿对本场辩手照抄
    加分。整理者的「结构拆解」也不给（那是二手判读，不是评委原话）。
    """
    chunks: list[str] = []
    for path_str in sorted(_same_topic_reference_paths(topic)):
        try:
            text = Path(path_str).read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.search(r"^# 评委[^\n]*\n(.*?)(?=^# |\Z)", text, re.M | re.S)
        if not m:
            continue
        head = text.splitlines()[0].lstrip("# ").strip() if text else Path(path_str).stem
        chunks.append(f"《{head}》评委发言要点：\n{m.group(1).strip()}")
    if not chunks:
        return ""
    joined = "\n\n".join(chunks)
    return joined[:limit] + ("\n…（已截断）" if len(joined) > limit else "")


async def _run_prep(topic: str, pro: str, con: str, roster: list[dict],
                    *, fmt: str, timeout: int) -> dict:
    """Run independent scouting, then one bounded team deliberation per side.

    Raw source material remains in the scout receipts.  Only an <=800 character
    board enters the match context, so preparation adds useful disagreement and
    evidence without flooding later turns.
    """
    await _emit_to_room(
        "双方先各自收集线索，再做队内讨论。资料库只是可选工具箱；不绑定名人，"
        "没核到的事实必须留在「未核实」，最后只有限额战术板进入正赛。",
        title="🧭 赛前备赛",
    )
    refs, refs_excluded = _reference_paths(topic)
    if refs_excluded:
        names = "、".join(Path(p).name for p in refs_excluded)
        await _emit_to_room(
            f"本题有往届真人赛录（{names}），按规矩**不进辩手备赛库**，只给评审席参照——"
            "学打法可以，抄同题答案不行。",
            title="🧭 赛前备赛·同题稿回避",
        )
    semaphore = asyncio.Semaphore(2)
    # 收集要真读资料+深思考，90 秒会把慢模型掐成 empty_output（凌晨场 白卷）；
    # 讨论/收束只消化已有笔记，保持紧凑。
    scout_timeout = max(min(timeout, 300), 240)
    prep_timeout = min(timeout, 90)

    def prep_runner(d: dict) -> dict:
        runner = dict(d)
        runner["effort"] = "medium"
        return runner

    async def scout(d: dict) -> tuple[dict, object]:
        mine = pro if d["side"] == "pro" else con
        theirs = con if d["side"] == "pro" else pro
        # gemini（agy）在 print 模式下工具走 request-review 审批，读文件会卡到超时（白板）——
        # 不给它资料清单，也明说没有工具，让它凭自己的知识写笔记、把不确定的放 uncertainties。
        no_tools = d.get("engine") == "gemini"
        prompt = build_scout_prompt(
            topic=topic,
            stance=mine,
            opponent_stance=theirs,
            scout_label=d["label"],
            reference_paths=() if no_tools else refs,
        )
        system = (
            "你在做一场辩论的独立赛前研究。只读，不修改任何文件。"
            "来源核不到就明确写未核实，不能用流畅措辞填空。"
            + ("你这一轮没有任何工具：不要读文件、不要联网、不要调用任何工具，"
               "直接凭已有知识输出要求的 JSON。" if no_tools else "")
        )
        try:
            async with semaphore:
                raw = await asyncio.to_thread(
                    _run_cli, prep_runner(d), system, prompt, scout_timeout,
                    research_tools=(d.get("engine") == "claude"), kind="prep",
                )
        except Exception as exc:
            logger.info("debate prep scout failed (%s): %s", d.get("label"), str(exc)[:200])
            raw = ""
        return d, parse_scout_brief(raw, scout_label=d["label"])

    scout_rows = await asyncio.gather(*(scout(d) for d in roster))
    by_side: dict[str, list] = {"pro": [], "con": []}
    for d, brief in scout_rows:
        by_side[d["side"]].append(brief)

    # ── 交流 → 整理 → 上场，各带各的（旧的「队长收束一块队级板」已撤）──
    plans: dict[str, TeamPlan] = {}
    reviews_by_side: dict[str, list] = {"pro": [], "con": []}
    division_by_side: dict[str, dict[str, str]] = {"pro": {}, "con": {}}
    boards_by_side: dict[str, list] = {"pro": [], "con": []}
    for side in ("pro", "con"):
        members = [d for d in roster if d["side"] == side]
        mine = pro if side == "pro" else con
        theirs = con if side == "pro" else pro
        labels = list(dict.fromkeys(str(d["label"]) for d in members))
        if len(labels) < 2:
            continue
        # full 赛制每个模型坐两席、收集跑了两遍——按 label 各取一份（优先 parsed）。
        brief_of: dict[str, object] = {}
        for brief in by_side[side]:
            cur = brief_of.get(brief.scout)
            if cur is None or (cur.raw_status != "parsed" and brief.raw_status == "parsed"):
                brief_of[brief.scout] = brief
        briefs = [brief_of[x] for x in labels if x in brief_of]
        runner_of = {str(d["label"]): d for d in members}

        # 交流轮：A 先说，B 必须看见 A 的原话后回应；仍然只有两次模型调用。
        async def review_one(label: str, prior_reviews: tuple[TeamReview, ...]) -> TeamReview:
            partner = labels[1] if label == labels[0] else labels[0]
            prompt = build_peer_review_prompt(
                topic=topic, stance=mine, opponent_stance=theirs,
                reviewer_label=label, partner_label=partner, briefs=briefs,
                prior_reviews=prior_reviews,
            )
            try:
                raw = await asyncio.to_thread(
                    _run_cli, prep_runner(runner_of[label]),
                    "你在和队友做赛前讨论。回应现有笔记，不写正式发言，只输出要求的 JSON。",
                    prompt, prep_timeout, kind="prep",
                )
            except Exception as exc:
                logger.info("debate prep peer review failed (%s/%s): %s", side, label, str(exc)[:200])
                raw = ""
            turn_index = len(prior_reviews) + 1
            return parse_team_review(
                raw,
                reviewer_label=label,
                member_labels=labels[:2],
                turn_index=turn_index,
                reply_to_turn_index=(prior_reviews[-1].turn_index if prior_reviews else None),
            )

        reviews: list[TeamReview] = []
        for label in labels[:2]:
            review = await review_one(label, tuple(reviews))
            reviews.append(review)
            await _emit_to_room(
                format_team_review_turn(review, total_turns=len(labels[:2])),
                title=(
                    f"🗣 {'正方' if side == 'pro' else '反方'}队内交流 "
                    f"{review.turn_index}/{len(labels[:2])}｜{label}"
                ),
            )
        reviews_by_side[side] = reviews

        # 角色与主线分工必须先于个人整理锁定；个人板不能再反向改角色。
        opening_label, rebuttal_label = decide_roles(
            labels[:2], reviews=reviews, briefs=briefs,
        )
        mainline_division = decide_mainline_division(
            labels[:2], reviews=reviews, briefs=briefs,
            opening_label=opening_label, rebuttal_label=rebuttal_label,
        )
        division_by_side[side] = mainline_division

        # 整理轮：各写自己上场带的笔记；写不出来退回自己的收集笔记。
        async def board_one(label: str) -> object:
            partner = labels[1] if label == labels[0] else labels[0]
            prompt = build_personal_board_prompt(
                topic=topic, stance=mine, opponent_stance=theirs,
                my_label=label, partner_label=partner, briefs=briefs, reviews=reviews,
                opening_label=opening_label, rebuttal_label=rebuttal_label,
                mainline_division=mainline_division,
            )
            try:
                raw = await asyncio.to_thread(
                    _run_cli, prep_runner(runner_of[label]),
                    "你在整理自己上场要带的笔记。禁止编造来源，只输出要求的 JSON。",
                    prompt, prep_timeout, kind="prep",
                )
            except Exception as exc:
                logger.info("debate prep personal board failed (%s/%s): %s", side, label, str(exc)[:200])
                raw = ""
            assigned_role = "opening" if label == opening_label else "rebuttal"
            return parse_personal_board(
                raw,
                label=label,
                my_brief=brief_of.get(label),
                assigned_role=assigned_role,
            )

        boards = list(await asyncio.gather(*(board_one(x) for x in labels[:2])))
        boards_by_side[side] = boards

        apply_personal_boards(
            roster, side=side, opening_label=opening_label, rebuttal_label=rebuttal_label,
            boards=boards, fmt=fmt,
        )
        # 队级摘要只用来存档/展示：交流轮里两人各自点出的最强共同主线 + 合并的未解决项。
        shared = [r.strongest_shared for r in reviews if r.strongest_shared]
        team_summary = "\n".join(f"{r.reviewer}：{r.strongest_shared}" for r in reviews if r.strongest_shared)
        unresolved = list(dict.fromkeys(
            x for row in (*reviews, *boards) for x in row.unresolved
        ))[:6]
        urls = list(dict.fromkeys(u for b in briefs for u in b.source_urls))[:8]
        plans[side] = TeamPlan(
            side=side, opening_label=opening_label, rebuttal_label=rebuttal_label,
            board=team_summary or "（交流轮没有形成可展示的共同主线；各人上场带各自的笔记）",
            source_urls=urls, unresolved=unresolved,
            raw_status="from_reviews" if shared else "no_shared_line",
        )

    for side in ("pro", "con"):
        plan = plans.get(side)
        if not plan:
            continue
        source_note = "、".join(plan.source_urls) if plan.source_urls else "无辩手报告已查阅的外链"
        unresolved = "；".join(plan.unresolved) if plan.unresolved else "无"
        personal_lines = []
        for pb in boards_by_side[side]:
            tag = {"parsed": "", "stitched_from_brief": "（整理失败，带的是自己的收集笔记）",
                   "unparsed": "（收集和整理都失败，裸打）"}.get(pb.raw_status, f"（{pb.raw_status}）")
            personal_lines.append(f"**{pb.label} 的上场笔记**{tag}：{pb.board or '（空）'}")
        await _emit_to_room(
            f"**角色**：一辩 {plan.opening_label}；二辩 {plan.rebuttal_label}\n\n"
            f"**交流轮共同主线**：{plan.board}\n\n"
            + "\n\n".join(personal_lines) + "\n\n"
            f"**辩手报告已查阅链接**：{source_note}\n\n**仍未解决**：{unresolved}\n\n"
            "（各人上场只带自己的笔记；链接是否真的支持论点，仍以赛录中的原始备赛票据和人工复核为准。）",
            title=f"📋 {'正方' if side == 'pro' else '反方'}备赛板",
        )

    all_outputs_parsed = (
        len(plans) == 2
        and all(brief.raw_status == "parsed" for briefs in by_side.values() for brief in briefs)
        and all(review.raw_status == "parsed" for reviews in reviews_by_side.values() for review in reviews)
        and all(pb.raw_status == "parsed" for boards in boards_by_side.values() for pb in boards)
    )
    return {
        "status": "complete" if all_outputs_parsed else "partial",
        "board_char_limit": PERSONAL_BOARD_MAX_CHARS,
        "prep_model": "personal_boards",   # 各带各的笔记；旧赛录无此键 = 队级共用板
        "call_timeout_seconds": {"scout": scout_timeout, "discussion": prep_timeout},
        "reasoning_effort": "medium",
        "reference_index": refs,
        "reference_excluded_same_topic": refs_excluded,
        "scouts": {
            side: [brief.to_dict() for brief in briefs]
            for side, briefs in by_side.items()
        },
        "discussion": {
            side: [review.to_dict() for review in reviews]
            for side, reviews in reviews_by_side.items()
        },
        "division": division_by_side,
        "teams": {side: plan.to_dict() for side, plan in plans.items()},
        "personal": {side: [pb.to_dict() for pb in boards] for side, boards in boards_by_side.items()},
    }


# ── 评委席（三席 + 插问）────
# 两席固定（GPT-5.6-sol、Claude Opus 5），第三席每场在 GPT-5.5 / Claude Fable 5 里抽——
# 参赛池两家各二，评委席三张票没法一场内两家平衡，只能靠第三席轮换拉平长期账。
# 三席顺序每场洗牌，换位票（A/B 呈现互换）落在洗牌后的第二席，不固定给某个模型。
# 评委 effort 用 high 而不是 max/ultra：读整场转录再出 JSON，max 一张票十来分钟不划算。
JUDGE_SEATS_FIXED = [
    {"engine": "codex", "model": "gpt-5.6-sol", "effort": "high", "label": "GPT-5.6-sol"},
    {"engine": "claude", "model": "claude-opus-5", "effort": "high", "label": "Claude Opus 5"},
]
JUDGE_SEAT_ROTATING = [
    {"engine": "codex", "model": "gpt-5.5", "effort": "high", "label": "GPT-5.5"},
    {"engine": "claude", "model": "claude-fable-5", "effort": "high", "label": "Claude Fable 5"},
]
JUDGE_SEAT_NAMES = ("评委甲", "评委乙", "评委丙")
JUDGE_ENGINE = os.environ.get("DEBATE_JUDGE_ENGINE", "cli").strip().lower() or "cli"  # cli | deepseek
JUDGE_TIMEOUT_MIN = 240

JUDGE_SYSTEM = (
    "你是一场辩论赛的评委。你不知道两队用的是什么模型，也看不到主持人的评价。"
    "只评论证质量与交锋水平，不评立场本身的道德倾向。"
    "只输出要求的 JSON 对象本身，不加代码围栏，不写任何前后说明。"
)

BENCH_ANSWER_RULE = (
    "现在是评委席插问环节：评委刚向你方提了一个问题，你代表全队当场作答。铁律：\n"
    "1. 正面回答，第一句就给答案，不许绕、不许反问评委、不许复述问题。\n"
    "2. 可以引用己方已经说过的论证，但不许把整段立论再念一遍。\n"
    f"3. 不超过 {BENCH_A_CHARS} 字，超出会被掐掉。\n"
    "4. 只输出回答正文。"
)


def _draw_panel(seed: Optional[int] = None, *, candidates: Optional[list[dict]] = None,
                roster: Optional[list[dict]] = None) -> list[dict]:
    """抽评委席。默认：两席固定 + 一席轮换（本机 CLI，开发期替身/补位）。
    传 candidates（外部评委报名池）时：先按回避规则剔掉本场辩手的主人，从剩下的抽三席，不够三席用默认席补位。
    三席顺序洗牌，标上评委甲/乙/丙。"""
    rng = random.Random(seed)
    if candidates:
        pool = eligible_judges(candidates, roster or [])
        rng.shuffle(pool)
        panel = [dict(j) for j in pool[:len(JUDGE_SEAT_NAMES)]]
        fill = [dict(seat) for seat in JUDGE_SEATS_FIXED] + [dict(seat) for seat in JUDGE_SEAT_ROTATING]
        while len(panel) < len(JUDGE_SEAT_NAMES) and fill:
            panel.append(fill.pop(0))
    else:
        panel = [dict(seat) for seat in JUDGE_SEATS_FIXED]
        panel.append(dict(rng.choice(JUDGE_SEAT_ROTATING)))
    rng.shuffle(panel)
    for name, judge in zip(JUDGE_SEAT_NAMES, panel):
        judge["name"] = name
    return panel


_RECHECK_COUNTER = {"n": 0}


def _position_recheck_enabled() -> bool:
    """对调票（每位评委多一张 A/B 互换票，只做位置自一致检测、不计分）开不开。
    对调票很烧额度：默认**关**，每 DEBATE_POSITION_RECHECK_EVERY（默认 5）场抽 1 场开——
    位置偏好是长期账，抽样够用，不必每场三张票白烧。
    DEBATE_POSITION_RECHECK=1/on 强制每场开；=0/off 彻底关。"""
    raw = os.environ.get("DEBATE_POSITION_RECHECK", "sample").strip().lower()
    if raw in {"1", "true", "on", "always"}:
        return True
    if raw in {"0", "false", "off", "never"}:
        return False
    every = max(1, int(os.environ.get("DEBATE_POSITION_RECHECK_EVERY", "5") or 5))
    _RECHECK_COUNTER["n"] += 1
    return _RECHECK_COUNTER["n"] % every == 0


async def _ask_judge(judge: dict, prompt: str, *, timeout: int, max_tokens: int,
                     kind: str = "ballot") -> tuple[str, str]:
    """一位评委答一次。返回 (原文, 错误)；CLI 挂了给空原文 + 错误，票会判无效而不是整场崩。
    外部评委（engine=external）走同一条路：_run_cli 会把出题写进投稿箱、等桥回稿，到点白卷；
    kind=ballot / bench_question 标在 request 上。DEBATE_JUDGE_ENGINE=deepseek 只管本机席位，
    外部评委不受它影响。"""
    if JUDGE_ENGINE == "deepseek" and judge.get("engine") != EXTERNAL_ENGINE:
        return await _deepseek(prompt, max_tokens=max_tokens), ""
    try:
        raw = await asyncio.to_thread(
            _run_cli, judge, JUDGE_SYSTEM, prompt, max(JUDGE_TIMEOUT_MIN, timeout), kind=kind,
        )
        return raw, ""
    except Exception as e:  # noqa: BLE001 — 评委掉线不该让整场比赛没有结果
        logger.info("debate judge %s failed: %s", judge.get("label"), str(e)[:200])
        return "", f"cli_failed: {str(e)[:160]}"


def _answering_seat(roster: list[dict], transcript: list[dict], side: str) -> Optional[dict]:
    """被插问的那队谁来答：该队在场上最后发言的席位（mini 是一辩、full 是四辩）。"""
    for row in reversed(transcript):
        if row.get("side") != side:
            continue
        for d in roster:
            if d.get("name") == row.get("speaker") and d.get("side") == side:
                return d
    team = [d for d in roster if d.get("side") == side and int(d.get("seat", 0)) > 0]
    return max(team, key=lambda d: int(d.get("seat", 0))) if team else None


def _render_bench(bench: list[dict], mapping: dict[str, str]) -> str:
    """把插问实录按这张票的 A/B 呈现渲染给评委看（评委席之间互相匿名）。"""
    lines: list[str] = []
    for i, row in enumerate(bench, 1):
        team = mapping.get(str(row.get("target")), "?")
        lines.append(f"[J{i:02d}] 评委问队{team}：{row.get('question') or ''}\n"
                     f"队{team}答：{row.get('answer') or '（未作答）'}")
    return "\n\n".join(lines)


async def _run_bench_questions(topic: str, pro: str, con: str, lang: str,
                               roster: list[dict], transcript: list[dict],
                               crossfire_log: list[dict], *, panel: list[dict],
                               stage_order: tuple[str, ...] = (),
                               timeout: int = 300) -> list[dict]:
    """评委席插问：三位评委各出一问（并行），被问方答题席当场作答（并行），逐条推流。

    可开关（state["bench_enabled"]）。评委出题看的是同一份不换位的盲审转录，
    评委互相不知道彼此问了什么——每问都是独立的。
    """
    blinded, mapping = blind_transcript(
        transcript, crossfire_log, swap=False, stage_order=stage_order,
    )
    qprompt = build_bench_question_prompt(topic=topic, blinded=blinded)
    raws = await asyncio.gather(*[
        _ask_judge(j, qprompt, timeout=timeout, max_tokens=200, kind="bench_question") for j in panel
    ])
    asks: list[dict] = []
    for judge, (raw, _err) in zip(panel, raws):
        parsed = parse_bench_question(raw, side_to_label=mapping)
        if parsed is None:
            await _emit_to_room(f"{judge['name']}放弃插问。", title="⚖️ 评委席插问")
            continue
        asks.append({"judge": judge["name"], "judge_label": judge["label"], **parsed})

    async def answer(ask: dict) -> dict:
        d = _answering_seat(roster, transcript, ask["target"])
        row = {**ask, "answerer": d["name"] if d else "", "answer": ""}
        if d is None:
            return row
        system = _build_system(d, topic, pro, con, lang) + "\n\n" + BENCH_ANSWER_RULE
        prompt = (
            f"【评委席插问】评委问你方（{_side_label(ask['target'])}）：{ask['question']}\n\n"
            f"请正面回答（不超过 {BENCH_A_CHARS} 字）："
        )
        try:
            text = await asyncio.to_thread(_run_cli, d, system, prompt, timeout, kind="bench_answer")
        except Exception as e:  # noqa: BLE001
            logger.info("bench answer failed: %s", str(e)[:200])
            return row
        row["answer"] = text.strip().replace("\n", " ")[:BENCH_A_CHARS]
        return row

    for ask in asks:
        await _emit_to_room(
            f"{ask['question']}", title=f"❓ {ask['judge']} → {_side_label(ask['target'])}",
        )
    answered = await asyncio.gather(*[answer(a) for a in asks])
    for row in answered:
        if row["answer"]:
            await _emit_to_room(row["answer"], title=f"💬 {row['answerer']}·答评委")
        else:
            await _emit_to_room("（未能作答）", title=f"💬 {_side_label(row['target'])}·答评委")
    return list(answered)


async def _run_blind_jury(topic: str, transcript: list[dict],
                          crossfire_log: list[dict],
                          stage_order: tuple[str, ...] = (), *,
                          panel: Optional[list[dict]] = None,
                          bench: Optional[list[dict]] = None,
                          roster: Optional[list[dict]] = None,
                          timeout: int = 300) -> dict:
    """每位评委两张互相隔离的票（六张并行）：原序票计胜负、计 MVP；A/B 对调票只做这位评委
    自己的位置自一致检测，不计票（评阅第 3 节：换位票由另一位评委判测不出
    位置偏好，且检测手段不该混进记分）。DEBATE_POSITION_RECHECK=0 可关掉对调票（只剩三张原序票）。

    评委走 CLI 通路（DEBATE_JUDGE_ENGINE=deepseek 可退回旧路）；明德杯记分见 prep。
    """
    panel = panel or _draw_panel()
    precedent = _precedent_verdict_text(topic)  # 同题往届判词：辩手看不到，评审可以看
    recheck = _position_recheck_enabled()
    fact_base = next((str(d.get("fact_base") or "").strip() for d in (roster or []) if d.get("fact_base")), "")

    async def one(index: int, judge: dict, swap: bool) -> dict:
        blinded, mapping = blind_transcript(
            transcript, crossfire_log, swap=swap, stage_order=stage_order,
        )
        prompt = build_ballot_prompt(
            topic=topic, blinded=blinded,
            bench_qa=_render_bench(bench, mapping) if bench else "",
            precedent=precedent, fact_base=fact_base,
        )
        raw, err = await _ask_judge(judge, prompt, timeout=timeout, max_tokens=1200)
        ballot = parse_ballot(
            raw, transcript=transcript, side_to_label=mapping,
            ballot_id=f"ballot-{index}{'-swapped' if swap else ''}",
            bench=bench or (),
        )
        if err and not ballot.get("valid"):
            ballot["error"] = err
        ballot["judge"] = judge.get("name", "")
        ballot["judge_label"] = judge.get("label", "")
        ballot["role"] = "recheck" if swap else "primary"
        return ballot

    jobs = [one(index, judge, False) for index, judge in enumerate(panel, 1)]
    if recheck:
        # 对调票只给本机 CLI 席位：外部评委是别人家的 AI，不拿第二张票去占人家的响应窗口
        jobs += [one(index, judge, True) for index, judge in enumerate(panel, 1)
                 if judge.get("engine") != EXTERNAL_ENGINE]
    ballots = await asyncio.gather(*jobs)
    result = aggregate_ballots(ballots)
    result["panel"] = [{"name": j.get("name"), "label": j.get("label")} for j in panel]
    result["engine"] = JUDGE_ENGINE
    result["position_recheck_enabled"] = recheck
    mvp = result.get("mvp")
    if isinstance(mvp, dict) and mvp.get("speaker"):
        seat = next((d for d in roster if d.get("name") == mvp["speaker"]), None) if roster else None
        mvp["model"] = (seat or {}).get("model", "")
        mvp["side"] = (seat or {}).get("side", "")
    return result


async def _run_crossfire(asker: dict, answerer: dict, topic: str, pro: str, con: str,
                         lang: str, transcript: list[dict], rounds: int,
                         timeout: int) -> list[dict]:
    """一轮交互质询：asker 问、answerer 答，来回 rounds 次，每一句都实时推流。

    每次调用都把「到目前为止的问答」原样喂回去，所以双方能顺着上一句继续追，
    而不是各说各话——这是跟长稿模式最本质的区别。
    """
    exchanges: list[dict] = []
    convo: list[str] = []

    for i in range(rounds):
        # ── 问 ──
        sys_q = _build_system(asker, topic, pro, con, lang) + "\n\n" + CROSSFIRE_ASKER
        ctx = "【场上已有的立论】\n" + "\n".join(
            f"{s['speaker']}：{s['text'][:400]}" for s in transcript[-2:]
        ) if transcript else ""
        if convo:
            ctx += "\n\n【本轮质询到目前为止】\n" + "\n".join(convo)
        prompt = f"{ctx}\n\n现在提出你的第 {i + 1} 个问题："
        try:
            q = await asyncio.to_thread(_run_cli, asker, sys_q, prompt, timeout, kind="crossfire_q")
        except Exception as e:
            logger.info("crossfire ask failed: %s", str(e)[:200])
            break
        q = q.strip().replace("\n", " ")[:CROSSFIRE_Q_CHARS]
        if not q:
            break
        convo.append(f"{asker['name']}（问）：{q}")
        await _emit_to_room(q, title=f"❓ {asker['name']}·质询")

        # ── 答 ──
        sys_a = _build_system(answerer, topic, pro, con, lang) + "\n\n" + CROSSFIRE_ANSWERER
        actx = "【本轮质询到目前为止】\n" + "\n".join(convo)
        aprompt = f"{actx}\n\n请正面回答刚才那个问题："
        try:
            a = await asyncio.to_thread(_run_cli, answerer, sys_a, aprompt, timeout, kind="crossfire_a")
        except Exception as e:
            logger.info("crossfire answer failed: %s", str(e)[:200])
            break
        a = a.strip().replace("\n", " ")[:CROSSFIRE_A_CHARS]
        if not a:
            break
        convo.append(f"{answerer['name']}（答）：{a}")
        await _emit_to_room(a, title=f"💬 {answerer['name']}·作答")

        exchanges.append({"q": q, "a": a,
                          "asker": asker["name"], "answerer": answerer["name"]})

    return exchanges


def _write_match_state(path: Path, state: dict) -> None:
    """Atomically checkpoint the JSON truth after every completed stage."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _finish_interrupted_record(path: Optional[Path], *, status: str, error: str = "") -> None:
    """Turn an in-progress checkpoint into an honest terminal record."""
    if path is None or not path.is_file():
        return
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        state["status"] = status
        state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if error:
            state["error"] = error[:300]
        _write_match_state(path, state)
    except (OSError, ValueError, TypeError) as exc:
        logger.info("debate terminal checkpoint failed: %s", str(exc)[:200])


def _jury_markdown(jury: dict) -> str:
    labels = {
        "decided": "形成稳定多数",
        "disputed": "评审分歧，暂不判",
        "position_unstable": "正反呈现顺序一换就翻案，暂不判",
        "judge_failed": "有效票不足，评审失败",
    }
    winner = {"pro": "正方", "con": "反方"}.get(jury.get("winner"), "无")
    lines = [
        f"**状态**：{labels.get(jury.get('status'), jury.get('status') or '未知')}",
        f"**结果**：{winner}",
        f"**决胜票**：正方 {jury.get('counts', {}).get('pro', 0)} / "
        f"反方 {jury.get('counts', {}).get('con', 0)} / "
        f"平票 {jury.get('counts', {}).get('tie', 0)} / "
        f"不确定 {jury.get('counts', {}).get('uncertain', 0)}",
    ]
    totals = jury.get("score_totals")
    if isinstance(totals, dict):
        lines.append(
            f"**明德杯计分**（尺子 {RUBRIC_TOTAL_MAX} + 自留 {DISCRETION_MAX}，"
            f"{jury.get('scored_ballots', 0)} 张票合计，只公示不定胜负）："
            f"正方 {totals.get('pro')} / 反方 {totals.get('con')}"
        )
    mvp = jury.get("mvp")
    if isinstance(mvp, dict):
        if mvp.get("speaker"):
            model_note = f"（{mvp['model']}）" if mvp.get("model") else ""
            lines.append(f"**最佳辩手**：{mvp['speaker']}{model_note} · {mvp.get('votes')}/{mvp.get('of')} 票")
        else:
            lines.append(f"**最佳辩手**：空缺（{'、'.join(mvp.get('tie') or [])} 平票）")
    panel = jury.get("panel") or []
    if panel:
        lines.append("**评委席**：" + " · ".join(
            f"{j.get('name')}={j.get('label')}" for j in panel
        ))
    lines.append("")
    for ballot in jury.get("ballots", []):
        if ballot.get("role") == "recheck":
            continue   # 对调票只用于位置自一致，不逐张播报
        who = ballot.get("judge") or ballot.get("ballot_id")
        if not ballot.get("valid"):
            lines.append(f"- {who}：无效（{ballot.get('error')}）")
            continue
        decision = {"pro": "正方", "con": "反方", "tie": "平票", "uncertain": "不确定"}.get(
            ballot.get("winner"), str(ballot.get("winner"))
        )
        score_note = ""
        scores = ballot.get("scores")
        if isinstance(scores, dict):
            score_note = (
                f"（正 {scores.get('pro', {}).get('total')} / 反 {scores.get('con', {}).get('total')}"
                + ("，分票不一致" if ballot.get("score_vote_consistent") is False else "")
                + "）"
            )
        lines.append(f"- {who}：{decision}{score_note}；{ballot.get('reason')}")
    unstable = jury.get("position_unstable_judges") or []
    checked = jury.get("position_checked_judges") or []
    if jury.get("position_recheck_enabled", True):
        if not jury.get("position_checked", True):
            pos_note = "\n⚠️ 对调票全部无效，这场的位置复判没做成——胜负按原序票，但没有换位对照。"
        elif unstable:
            pos_note = (f"\n⚠️ 位置复判：{'、'.join(unstable)} 的 A/B 对调票翻了案"
                        f"（{len(unstable)}/{len(checked)} 位评委自身不稳）。")
        else:
            pos_note = f"\n位置复判：{len(checked)} 位评委 A/B 对调后判决不变。"
    else:
        pos_note = "\n（本场未做位置复判）"
    lines.append(
        "\n各票互相不可见；每位评委另判一张 A/B 对调票，只用于检测自己是否受呈现顺序影响，不计票。"
        "主持播报不进入评审材料。评委只评论证与交锋，不评立场道德倾向。" + pos_note
    )
    return "\n".join(lines)


async def _run_match(topic: str, pro: str, con: str, fmt: str, lang: str,
                     timeout: int, draw: bool = True,
                     crossfire_rounds: int = 4,
                     prep_enabled: bool = True,
                     bench_enabled: bool = True,
                     pool: Optional[list[dict]] = None,
                     seed: Optional[int] = None,
                     run_id: Optional[str] = None,
                     judge_pool: Optional[list[dict]] = None) -> None:
    # run_id 可由 _launch 预分配（并发上限要在 task 起来之前就占位）；直接调本函数
    # （tools/ 脚本、测试）不给就自己生成。
    if not run_id:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        run_id = f"debate-{stamp}-{uuid.uuid4().hex[:8]}"
    _CUR_RUN.set(run_id)
    if draw or pool:
        roster, draw_note = _draw_roster(fmt, seed=seed, pool=pool)
    else:
        roster = [dict(row) for row in (ROSTER_MINI if fmt == "mini" else ROSTER_FULL)]
        draw_note = ""
    fact_base = _fact_base_for(topic)
    if fact_base:
        for d in roster:
            d["fact_base"] = fact_base   # 辩手 system 与评委票都从席位上取，同一份
    transcript: list[dict] = []
    crossfire_log: list[dict] = []
    schedule = MINI_FORMAT if fmt == "mini" else FULL_FORMAT
    out = TRANSCRIPT_DIR / f"{run_id}.json"
    for d in roster:
        d["run_id"] = run_id            # 外部席位投稿箱按 run_id 分目录（必须在 run_id 生成之后挂）
    _register_run(run_id, out_path=out)
    rules_digest = hashlib.sha256(
        (STRUCTURE_RULE + STYLE_RULE + json.dumps(SEAT_STRUCTURE, ensure_ascii=False, sort_keys=True)).encode("utf-8")
    ).hexdigest()[:12]
    state = {
        "schema_version": 2,
        "run_id": run_id,
        "status": "preparing" if prep_enabled else "running",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "topic": topic,
        "pro_side": pro,
        "con_side": con,
        "format": fmt,
        "lang": lang,
        "crossfire_rounds": crossfire_rounds,
        "prep_enabled": prep_enabled,
        "bench_enabled": bench_enabled,
        "judge_engine": JUDGE_ENGINE,
        "phase": "prep" if prep_enabled else "match",
        "schedule": [
            {"index": index, "stage": stage, "side": side, "seat": seat, "seconds": seconds}
            for index, (stage, side, seat, seconds) in enumerate(schedule)
        ],
        "draw_note": draw_note,
        "chars_per_second": CHARS_PER_SECOND,
        "rules_digest": rules_digest,
        "context_contract": {
            "mentor_binding": False,
            "contestant_session": "ephemeral",
            "project_settings": False,
            "match_tools": "none",
            "prep_tools": "read_or_search_only",
        },
        "draw_seed": seed,
        "judge_pool": [dict(j) for j in (judge_pool or [])],   # 外部评委报名池；空 = 默认三席
        "prep": {"status": "disabled"},
        "roster": roster,
        "transcript": transcript,
        "crossfire": crossfire_log,
        "panel": None,
        "bench": [],
        "jury": None,
    }
    _write_match_state(out, state)

    if draw_note:
        await _emit_to_room(draw_note + "。抽签只定队伍，具体一二辩由队内备赛后自己决定。",
                            title="🎲 抽签结果")
    if prep_enabled:
        state["prep"] = await _run_prep(topic, pro, con, roster, fmt=fmt, timeout=timeout)
        state["status"] = "running"
        state["phase"] = "match"
        state["roster"] = roster
        _write_match_state(out, state)

    await _run_schedule(state, out, timeout=timeout, emit_opening=True)


async def _run_schedule(state: dict, out: Path, *, timeout: int,
                        emit_opening: bool) -> None:
    """Run only stages that are not already present in an atomic checkpoint.

    A long model tournament must survive an operator shell or worker window going
    away.  ``schedule_index`` is the resume cursor, including repeated full-format
    free-debate labels.  We never manufacture a
    host placeholder after a provider failure: the checkpoint remains resumable
    and the failed stage is tried again explicitly.
    """
    topic = str(state["topic"])
    pro = str(state["pro_side"])
    con = str(state["con_side"])
    fmt = str(state["format"])
    lang = str(state["lang"])
    roster = state["roster"]
    transcript = state["transcript"]
    crossfire_log = state["crossfire"]
    schedule = MINI_FORMAT if fmt == "mini" else FULL_FORMAT
    crossfire_rounds = int(state.get("crossfire_rounds", 4))
    if emit_opening:
        lines = [
            f"**辩题**：{topic}", "", f"- **正方**：{pro}", f"- **反方**：{con}", "",
            "**参赛阵容**",
        ]
        for d in roster:
            lines.append(f"- {d['name']}　{d['label']}　思考强度 {d['effort']}")
        lines.append("")
        lines.append(
            f"共 {len(schedule)} 个环节。立场由抽签决定、开赛注入、全场锁死；"
            f"发言超出字数上限当场掐断。"
            f"含交互质询：问 ≤{CROSSFIRE_Q_CHARS} 字、答 ≤{CROSSFIRE_A_CHARS} 字，"
            f"来回 {crossfire_rounds} 轮，只能问不能陈述。秩序由 DeepSeek 维持。"
            + ("闭幕后评委席每席插问一问（≤{q} 字），被问方当场作答（≤{a} 字）计入评分；".format(
                q=BENCH_Q_CHARS, a=BENCH_A_CHARS) if state.get("bench_enabled", True) else "")
            + f"三位 AI 评委盲审（看不到模型名和主持播报），明德杯记分：尺子分 {RUBRIC_TOTAL_MAX}"
            f" + 自留分 {DISCRETION_MAX} + 决胜票 + 最佳辩手，胜负以决胜票为准；"
            "每位评委另判一张正反对调票，只检测自身位置偏好、不计票。"
            "题面事实基座之外的数据、事件、引文默认不采信，举证在引用方。"
        )
        fact_base = next((str(d.get("fact_base") or "") for d in roster if d.get("fact_base")), "")
        if fact_base:
            lines.append(f"**题面事实基座**（双方共享前提）：{fact_base}")
        lines.append("")
        lines.append(
            f"🎟 **观众席已开**（人和 AI 都可投）：`POST /api/debate/{state['run_id']}/vote` "
            "{voter_id, side: pro|con, favorite?: 最喜爱辩手席位名}。盲投——评委公示前谁也看不到分布；"
            "自家 AI 在场的票照收但不进客观票；观众票不影响评委裁决。"
        )
        await _emit_to_room("\n".join(lines), title="🏛 辩论赛开场", notify=True)
    else:
        await _emit_to_room(
            f"从原子赛录继续：已完成 {len(transcript)} 段发言、"
            f"{len(crossfire_log)} 个质询环节；不会重放已完成内容。",
            title="↩️ 比赛续跑",
        )

    completed_indices: set[int] = set()
    stage_occurrences: dict[str, list[int]] = {}
    for schedule_index, (stage, _side, _seat, _seconds) in enumerate(schedule):
        stage_occurrences.setdefault(stage, []).append(schedule_index)
    fallback_seen: dict[str, int] = {}
    for row in [*transcript, *crossfire_log]:
        raw_index = row.get("schedule_index")
        if type(raw_index) is int and 0 <= raw_index < len(schedule):
            completed_indices.add(raw_index)
            continue
        stage = str(row.get("stage") or "")
        occurrence = fallback_seen.get(stage, 0)
        positions = stage_occurrences.get(stage, [])
        if occurrence < len(positions):
            completed_indices.add(positions[occurrence])
            fallback_seen[stage] = occurrence + 1

    free_idx = {
        side: sum(
            1 for index, (_stage, done_side, seat, _seconds) in enumerate(schedule)
            if index in completed_indices and seat == 0 and done_side == side
        )
        for side in ("pro", "con")
    }

    for schedule_index, (stage, side, seat, seconds) in enumerate(schedule):
        # 交互质询段：不是长稿，是一问一答的多轮往返
        if seat == -1:
            if schedule_index in completed_indices:
                continue
            asker = next(d for d in roster if d["side"] == side and d["seat"] == 1)
            other = "con" if side == "pro" else "pro"
            answerer = next(d for d in roster if d["side"] == other and d["seat"] == 1)
            await _emit_to_room(
                f"下面进入交互质询，由 **{asker['name']}**（{asker['label']}）提问，"
                f"**{answerer['name']}**（{answerer['label']}）作答。"
                f"问不超过 {CROSSFIRE_Q_CHARS} 字，答不超过 {CROSSFIRE_A_CHARS} 字，"
                f"共 {crossfire_rounds} 轮。",
                title=f"⚔️ {stage}",
            )
            ex = await _run_crossfire(asker, answerer, topic, pro, con, lang,
                                      transcript, crossfire_rounds, timeout)
            if len(ex) != crossfire_rounds:
                raise RuntimeError(
                    f"{stage} 只完成 {len(ex)}/{crossfire_rounds} 轮有效问答"
                )
            crossfire_log.append({
                "stage": stage, "schedule_index": schedule_index, "exchanges": ex,
            })
            _write_match_state(out, state)
            continue

        if schedule_index in completed_indices:
            continue

        if seat == 0:
            pool = [d for d in roster if d["side"] == side]
            d = pool[free_idx[side] % len(pool)]
            free_idx[side] += 1
        else:
            d = next((x for x in roster if x["side"] == side and x["seat"] == seat),
                     next(x for x in roster if x["side"] == side))

        limit = int(seconds * CHARS_PER_SECOND)
        await _emit_to_room(
            f"下面有请 **{d['name']}**（{d['label']}）发言，时限 {seconds} 秒 ≈ {limit} 字。",
            title=f"⏱ {stage}",
        )

        t0 = time.time()
        system = _build_system(d, topic, pro, con, lang)
        prompt = _build_prompt(d, stage, limit, seconds, transcript, crossfire_log)
        try:
            text = await asyncio.to_thread(_run_cli, d, system, prompt, timeout)
        except Exception as exc:
            raise RuntimeError(f"{stage} 未取得有效发言：{str(exc)[:180]}") from exc
        elapsed = round(time.time() - t0, 1)

        truncated = len(text) > limit
        if truncated:
            text = text[:limit]

        hits = [m for m in DEFECTION_MARKERS if m in text]
        violations: list[str] = []
        if truncated:
            violations.append(f"发言超出 {limit} 字上限，已被当场掐断")
        if hits:
            violations.append(f"出现疑似倒戈表述：{'、'.join(hits)}")

        quote_findings = verify_opponent_quotes(
            text, side=side, transcript=transcript, crossfire=crossfire_log,
        )
        if quote_findings:
            violations.append(f"{len(quote_findings)} 处引号内原话未在此前对方发言中精确找到")

        entry = {
            "speaker": d["name"], "side": side, "stage": stage, "text": text,
            "schedule_index": schedule_index,
            "chars": len(text), "limit": limit, "truncated": truncated,
            "elapsed_sec": elapsed, "defection_hits": hits,
            "quote_checks": quote_findings,
        }
        transcript.append(entry)

        foot = f"　　*{len(text)}/{limit} 字"
        foot += "，被计时器掐断*" if truncated else "，在时限内说完*"
        emitted_id = await _emit_to_room(
            f"{text}\n\n{foot}",
            title=f"{stage}｜{d['label']}",
        )
        # msg_id 必须在落盘前就位：checkpoint 写在 emit 之后，否则 resume 读到的
        # transcript 永久无短号，_build_prompt 教了引用语法却没号可引（第2振 P1-2）。
        entry["msg_id"] = emitted_id
        _write_match_state(out, state)

        host = await _host_check(topic, stage, d["name"], text, violations)
        if host:
            await _emit_to_room(host, title="🎙 主持人")
        elif violations:
            await _emit_to_room("；".join(violations), title="🎙 主持人")

    total = sum(s["elapsed_sec"] for s in transcript)
    cut = sum(1 for s in transcript if s["truncated"])
    qa = sum(len(b["exchanges"]) for b in crossfire_log)
    stage_order = tuple(stage for stage, _side, _seat, _seconds in schedule)
    # 评委席：赛录里已有就沿用（续跑）；否则从外部评委报名池（judge_pool）按回避抽、不够用本机席补位；
    # 没报名池就是默认三席。外部评委要 run_id 才知道投稿箱在哪个目录。
    panel = state.get("panel") or _draw_panel(
        seed=state.get("draw_seed"), candidates=state.get("judge_pool") or None, roster=roster)
    for j in panel:
        j["run_id"] = state["run_id"]
    state["panel"] = panel
    bench_enabled = bool(state.get("bench_enabled", True))
    await _emit_to_room(
        f"全场 {len(transcript)} 段发言、{qa} 组质询问答结束，"
        f"{cut} 段被计时器掐断，生成总耗时 {round(total, 1)} 秒。\n\n"
        + ("评委席插问开始，随后三张匿名评审票分别裁决……" if bench_enabled
           else "三张匿名评审票正在分别裁决……"),
        title="🏁 比赛结束", notify=True,
    )

    if bench_enabled and not state.get("bench"):
        state["phase"] = "bench"
        _write_match_state(out, state)
        state["bench"] = await _run_bench_questions(
            topic, pro, con, lang, roster, transcript, crossfire_log,
            panel=panel, stage_order=stage_order, timeout=timeout,
        )
        _write_match_state(out, state)

    state["phase"] = "jury"
    _write_match_state(out, state)
    jury = await _run_blind_jury(
        topic, transcript, crossfire_log,
        stage_order=stage_order, panel=panel,
        bench=state.get("bench") or [], roster=roster, timeout=timeout,
    )
    state["jury"] = jury
    # 观众席关票 + 汇总挂进赛录（在 phase=done 落盘之前关，落盘后投票端点就拒了）
    audience_summary = _audience.close_and_summarize(TRANSCRIPT_DIR, str(state["run_id"]), state)
    state["status"] = "completed" if jury.get("status") != "judge_failed" else "judge_failed"
    state["phase"] = "done"
    state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _write_match_state(out, state)
    await _emit_to_room(_jury_markdown(jury), title="⚖️ 匿名评审团", notify=True)
    await _emit_to_room(_audience.summary_markdown(audience_summary, pro, con), title="🎟 观众席")

    await _emit_to_room(f"记录已存档：`{out.name}`", title="📁 存档")


async def resume_match(path: Path, *, timeout: int = 300) -> None:
    """Resume one non-terminal schema-v2 match from its last checkpoint."""
    out = path.resolve()
    if out.parent != TRANSCRIPT_DIR.resolve() or not out.is_file():
        raise ValueError("resume path must be an existing debate transcript")
    state = json.loads(out.read_text(encoding="utf-8"))
    if state.get("schema_version") != 2:
        raise ValueError("only schema-v2 matches are resumable")
    if state.get("status") in {"completed", "judge_failed"}:
        raise ValueError(f"match is already terminal: {state.get('status')}")
    required = ("run_id", "topic", "pro_side", "con_side", "format", "lang",
                "roster", "transcript", "crossfire")
    missing = [key for key in required if key not in state]
    if missing:
        raise ValueError(f"resume checkpoint missing: {', '.join(missing)}")

    resume_prep = (
        state.get("phase") == "prep"
        or state.get("status") == "preparing"
        or (
            state.get("prep_enabled") is True
            and (state.get("prep") or {}).get("status") == "disabled"
            and not state.get("transcript")
            and not state.get("crossfire")
        )
    )
    state["status"] = "preparing" if resume_prep else "running"
    state.pop("finished_at", None)
    state.pop("error", None)
    _write_match_state(out, state)
    run_id = str(state["run_id"])
    _CUR_RUN.set(run_id)
    _register_run(run_id, out_path=out, task=asyncio.current_task())
    try:
        if resume_prep:
            roster = state["roster"]
            state["prep"] = await _run_prep(
                str(state["topic"]),
                str(state["pro_side"]),
                str(state["con_side"]),
                roster,
                fmt=str(state["format"]),
                timeout=timeout,
            )
            state["roster"] = roster
            state["status"] = "running"
            state["phase"] = "match"
            _write_match_state(out, state)
        await _run_schedule(state, out, timeout=timeout, emit_opening=False)
    except asyncio.CancelledError:
        _finish_interrupted_record(out, status="cancelled")
        raise
    except Exception as exc:
        _finish_interrupted_record(out, status="failed", error=str(exc))
        raise
    finally:
        _unregister_run(run_id)


def _cur_out_path() -> Optional[Path]:
    row = _RUNS.get(_CUR_RUN.get() or "")
    return row.get("out_path") if row else None


async def _run_match_guarded(*args, **kwargs) -> None:
    # run_id 由 _launch 预分配并通过 kwargs 传进来（并发上限占位）；_run_match 内部
    # 也会 _CUR_RUN.set 同一个值，这里先 set 是为了 _run_match 还没跑到那一行就炸时
    # finally 也能把占位撤掉。
    pre_run_id = kwargs.get("run_id")
    if pre_run_id:
        _CUR_RUN.set(pre_run_id)
    try:
        await _run_match(*args, **kwargs)
    except asyncio.CancelledError:
        _finish_interrupted_record(_cur_out_path(), status="cancelled")
        return
    except Exception as e:
        logger.warning("debate match crashed: %s", str(e)[:300])
        _finish_interrupted_record(_cur_out_path(), status="failed", error=str(e))
        try:
            await _emit_to_room(f"比赛中断：{str(e)[:200]}", title="⚠️ 出错了")
        except Exception:
            pass
    finally:
        _unregister_run(_CUR_RUN.get() or pre_run_id)


async def _launch(body: dict) -> tuple[int, dict]:
    """解析一份开赛请求并起后台任务。返回 (http 状态码, 响应体)。start 端点和赛程队列共用。
    并发上限 MAX_CONCURRENT（默认 1 = 跟原来一样第二场 409）。"""
    if len(_RUNS) >= MAX_CONCURRENT:
        oldest = min((row.get("started_at") or 0.0) for row in _RUNS.values()) if _RUNS else None
        return 409, {"error": "already_running", "started_at": oldest,
                     "running": len(_RUNS), "max_concurrent": MAX_CONCURRENT}

    topic = (body.get("topic") or "").strip()
    topic_id = (body.get("topic_id") or "").strip()
    picked: Optional[dict] = None

    if not topic:
        if topic_id:
            picked = next((t for t in _load_topics() if t.get("id") == topic_id), None)
            if picked is None:
                return 404, {"error": f"unknown topic_id: {topic_id}"}
        else:
            picked = _draw_topic()
            if picked is None:
                return 400, {"error": "topic required (题库为空)"}
        topic = f"{picked['pro']}/{picked['con']}"

    fmt = body.get("format") or "mini"
    if fmt not in ("mini", "full"):
        return 400, {"error": "format must be mini|full"}
    lang = body.get("lang") or "zh"
    if lang not in ("zh", "en"):
        return 400, {"error": "lang must be zh|en"}
    timeout = int(body.get("timeout") or 300)
    draw = bool(body.get("draw", True))   # 默认抽签定正反方
    crossfire_rounds = max(0, min(10, int(body.get("crossfire_rounds", 4))))
    prep_enabled = bool(body.get("prep", True))
    bench_enabled = bool(body.get("bench", True))   # 评委席插问，默认开
    pool: Optional[list[dict]] = None
    if body.get("pool") is not None:
        try:
            pool = parse_pool(body.get("pool"))
        except ValueError as exc:
            return 400, {"error": str(exc)}
    seed = body.get("seed")
    seed = int(seed) if seed is not None else None
    judge_pool: Optional[list[dict]] = None
    if body.get("judge_pool") is not None:
        try:
            judge_pool = parse_judge_pool(body.get("judge_pool"))
        except ValueError as exc:
            return 400, {"error": str(exc)}

    if picked:
        #  10:41 场抽到「对他人的期待是不是一种隐形的暴力」，辩手拿到的辩题却是
        # 「是隐形的暴力/不是隐形的暴力」——主语丢在 title 里没传下去，四个模型只能猜在辩什么。
        # 题库里 pro/con 多数是短语（是成长/是遗憾、该/不该），辩题必须用 title 撑住。
        pro, con = str(picked["pro"]).strip(), str(picked["con"]).strip()
        title = str(picked.get("title") or "").strip()
        topic = f"{title}（正方：{pro}；反方：{con}）" if title else f"{pro}/{con}"
    elif "/" in topic:
        pro, con = topic.split("/", 1)
    else:
        pro, con = topic, f"并非如此：{topic}"

    # 先占位再起 task：两份开赛请求同一拍进来，第二份在 task 跑起来之前就能看到上限已满。
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_id = f"debate-{stamp}-{uuid.uuid4().hex[:8]}"
    _register_run(run_id)
    task = asyncio.create_task(
        _run_match_guarded(topic, pro.strip(), con.strip(), fmt, lang, timeout,
                           draw, crossfire_rounds, prep_enabled, bench_enabled,
                           pool=pool, seed=seed, run_id=run_id,
                           judge_pool=judge_pool)
    )
    _register_run(run_id, task=task)
    seats = len(MINI_FORMAT if fmt == "mini" else FULL_FORMAT)
    return 200, ({"ok": True, "run_id": run_id, "format": fmt, "lang": lang,
                         "draw": draw, "prep": prep_enabled, "bench": bench_enabled,
                         "pool": [p["label"] for p in pool] if pool else None,
                         "judge_pool": [j["label"] for j in judge_pool] if judge_pool else None,
                         "seed": seed,
                         "judge_engine": JUDGE_ENGINE,
                         "stages": seats, "topic": topic,
                         "topic_title": (picked or {}).get("title")})


@router.post("/api/debate/start")
async def debate_start(req: Request):
    """开一场辩论赛，后台跑、边跑边推到 room:debate。

    body: {topic?, topic_id?, format?: mini|full, lang?: zh|en, timeout?: int, draw?, crossfire_rounds?,
           prep?, bench?, pool?: [4 个预设名:强度 或 {engine,model,effort,label,owner?}],
           judge_pool?: [外部评委报名池，同 pool 每项格式，1 条起；按回避抽三席、不够本机席补位],
           seed?: int}
    topic 用 / 分隔正反方，例如 "时间赋予生命意义/生命赋予时间意义"。
    不给 topic 就从 data/debates/topics.json 里抽一道（优先没打过的）；
    给 topic_id 就点名题库里的那一道。
    """
    status, payload = await _launch(await req.json())
    return JSONResponse(payload, status_code=status)


# ── 赛程队列（）：A/B 两场要串着打、还得活过服务重启——排进 queue.json，
# 服务启动时和每场结束后自动续。 10:22 那次靠 setsid 脚本等重启后开赛，脚本被
# systemd 连 cgroup 一起收了，队列进服务本身才靠谱。
QUEUE_PATH = TRANSCRIPT_DIR / "queue.json"
_DRAIN: dict = {"task": None}
_QUEUE_PACE_SECONDS = 5.0   # 两场开赛之间的喘口气；测试里调小


def _read_queue() -> list[dict]:
    try:
        data = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_queue(rows: list[dict]) -> None:
    tmp = QUEUE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(QUEUE_PATH)


async def _wait_for_slot() -> None:
    """等到并发数低于上限。有 task 就等任一场结束；没 task（占位中）就小睡再看。"""
    while len(_RUNS) >= MAX_CONCURRENT:
        tasks = [row["task"] for row in _RUNS.values() if row.get("task") is not None]
        if tasks:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        else:
            await asyncio.sleep(10)


async def _drain_queue() -> None:
    """按队列顺序开赛；每场开赛前先等出一个并发位（MAX_CONCURRENT=1 时就是等场上空下来）。
    失败的场记 error 后出队，不卡住后面。"""
    while True:
        rows = _read_queue()
        if not rows:
            return
        if len(_RUNS) >= MAX_CONCURRENT:
            await _wait_for_slot()
            continue
        head = rows[0]
        status, payload = await _launch(dict(head.get("body") or {}))
        rows = _read_queue()
        if rows and rows[0].get("id") == head.get("id"):
            rows.pop(0)
            _write_queue(rows)
        if status != 200:
            logger.warning("debate queue: item %s rejected: %s", head.get("id"), payload)
            try:
                await _emit_to_room(f"队列里的一场没开起来（{payload.get('error')}），已跳过。",
                                    title="🗓 赛程队列")
            except Exception:
                pass
            continue
        try:
            await _emit_to_room(
                f"按赛程队列自动开赛：{payload.get('topic_title') or payload.get('topic')}。"
                f"队列还剩 {len(rows)} 场。",
                title="🗓 赛程队列",
            )
        except Exception:
            pass
        # 并发上限 1 时等这场打完再开下一场（原行为）；上限 >1 时只要还有位就继续出队。
        await _wait_for_slot()
        await asyncio.sleep(_QUEUE_PACE_SECONDS)


def _kick_drain() -> None:
    task = _DRAIN.get("task")
    if task is not None and not task.done():
        return
    _DRAIN["task"] = asyncio.create_task(_drain_queue())


async def debate_queue_startup() -> None:
    """server.py 的 lifespan 启动时调（app 用 lifespan，router.on_event 不触发）。"""
    if _read_queue():
        await asyncio.sleep(15)   # 让服务先站稳、前端连上，再开赛
        _kick_drain()


@router.post("/api/debate/queue")
async def debate_queue_add(req: Request):
    """排一场（body 同 /api/debate/start）。当前空闲就立刻开，否则等前面打完自动开；活过重启。"""
    body = await req.json()
    if body.get("pool") is not None:
        try:
            parse_pool(body.get("pool"))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    if body.get("judge_pool") is not None:
        try:
            parse_judge_pool(body.get("judge_pool"))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    rows = _read_queue()
    item = {"id": uuid.uuid4().hex[:8], "queued_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "label": str(body.get("label") or ""), "body": body}
    rows.append(item)
    _write_queue(rows)
    _kick_drain()
    return JSONResponse({"ok": True, "id": item["id"], "position": len(rows), "running": bool(_RUNS)})


@router.get("/api/debate/queue")
async def debate_queue_list():
    runs = _running_snapshot()
    return JSONResponse({"running": bool(runs), "run_id": runs[0]["run_id"] if runs else None,
                         "runs": runs, "max_concurrent": MAX_CONCURRENT,
                         "queue": _read_queue()})


@router.delete("/api/debate/queue")
async def debate_queue_clear(req: Request):
    """清空队列（不动正在打的那场）；带 ?id= 只删一场。"""
    target = (req.query_params.get("id") or "").strip()
    rows = _read_queue()
    kept = [r for r in rows if target and r.get("id") != target]
    _write_queue(kept)
    return JSONResponse({"ok": True, "removed": len(rows) - len(kept), "remaining": len(kept)})


@router.get("/api/debate/topics")
async def debate_topics():
    """题库清单。played 标着打过没有。"""
    topics = _load_topics()
    return JSONResponse({
        "count": len(topics),
        "unplayed": sum(1 for t in topics if not t.get("played")),
        "topics": [
            {"id": t.get("id"), "title": t.get("title"),
             "pro": t.get("pro"), "con": t.get("con"),
             "tags": t.get("tags", []), "played": t.get("played")}
            for t in topics
        ],
    })


# ── 荐题投稿箱：任何人都能投一道辩题，审过才进题库 ────
# 城里的 AI 和人推荐的辩题先落这里，她审过再进题库；不直接写 topics.json。
TOPIC_SUGGEST_PATH = TRANSCRIPT_DIR / "topic_suggestions.json"
TOPIC_TITLE_MAX = 60
TOPIC_SIDE_MAX = 40


def _read_suggestions() -> list[dict]:
    try:
        data = json.loads(TOPIC_SUGGEST_PATH.read_text(encoding="utf-8"))
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_suggestions(rows: list[dict]) -> None:
    TOPIC_SUGGEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOPIC_SUGGEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(TOPIC_SUGGEST_PATH)


def validate_topic_suggestion(body: dict) -> tuple[Optional[dict], Optional[str]]:
    title = str(body.get("title") or "").strip()
    pro = str(body.get("pro") or "").strip()
    con = str(body.get("con") or "").strip()
    by = str(body.get("suggested_by") or "").strip()
    if not by or len(by) > 120:
        return None, "suggested_by required (≤120 chars)"
    if not pro or not con:
        return None, "pro and con required"
    if len(title) > TOPIC_TITLE_MAX or len(pro) > TOPIC_SIDE_MAX or len(con) > TOPIC_SIDE_MAX:
        return None, f"title ≤{TOPIC_TITLE_MAX} / pro,con ≤{TOPIC_SIDE_MAX} chars"
    tags = body.get("tags") or []
    if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags) or len(tags) > 6:
        return None, "tags must be ≤6 strings"
    note = str(body.get("note") or "").strip()
    if len(note) > 200:
        return None, "note ≤200 chars"
    return {"title": title, "pro": pro, "con": con, "tags": [t.strip() for t in tags if t.strip()],
            "suggested_by": by, "note": note or None}, None


@router.post("/api/debate/topics/suggest")
async def debate_topic_suggest(req: Request):
    """荐题：{suggested_by, pro, con, title?, tags?, note?}。落投稿箱，status=pending，审过才进题库。
    同一人同一道（pro/con 相同）只留一条。"""
    body = await req.json()
    item, err = validate_topic_suggestion(body if isinstance(body, dict) else {})
    if err:
        return JSONResponse({"error": err}, status_code=400)
    rows = _read_suggestions()
    dup = next((r for r in rows if r.get("suggested_by") == item["suggested_by"]
                and r.get("pro") == item["pro"] and r.get("con") == item["con"]), None)
    if dup:
        return JSONResponse({"ok": True, "id": dup.get("id"), "status": dup.get("status"), "duplicate": True})
    # 题库里已有同题也告诉它，但照收（可能是想换个说法）
    known = any(t.get("pro") == item["pro"] and t.get("con") == item["con"] for t in _load_topics())
    item.update({"id": uuid.uuid4().hex[:8], "status": "pending",
                 "suggested_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "already_in_bank": known})
    rows.append(item)
    _write_suggestions(rows)
    return JSONResponse({"ok": True, "id": item["id"], "status": "pending", "already_in_bank": known,
                         "pending": sum(1 for r in rows if r.get("status") == "pending")})


@router.get("/api/debate/topics/suggestions")
async def debate_topic_suggestions(req: Request):
    """投稿箱。?status=pending|accepted|rejected 过滤，默认全部。"""
    want = (req.query_params.get("status") or "").strip()
    rows = _read_suggestions()
    if want:
        rows = [r for r in rows if r.get("status") == want]
    return JSONResponse({"count": len(rows), "suggestions": rows})


@router.get("/api/debate/board")
async def board(req: Request):
    """选手榜：参赛 / 胜 / 胜率 / MVP / 观众票 / 观众 MVP。?by=model（默认）或 by=name（按席位）；
    ?by=audience 是观众榜（投了几场、与评委一致率、自家票）。"""
    from tools.board import load_records, tally, to_markdown
    want = (req.query_params.get("by") or "").strip()
    records, skipped = load_records()
    if want == "audience":
        board = _audience.audience_board(records)
        board["markdown"] = _audience.audience_board_markdown(board)
        return JSONResponse(board)
    by = "name" if want == "name" else "model"
    board = tally(records, by=by, skipped=skipped)
    board["markdown"] = to_markdown(board)
    return JSONResponse(board)


@router.get("/api/debate/status")
async def debate_status():
    """单场字段（running/started_at/run_id）照旧给老调用方；runs 是多场全貌。"""
    runs = _running_snapshot()
    first = runs[0] if runs else None
    return JSONResponse({
        "running": bool(runs),
        "started_at": first["started_at"] if first else None,
        "run_id": first["run_id"] if first else None,
        "runs": runs,
        "max_concurrent": MAX_CONCURRENT,
    })


@router.post("/api/debate/stop")
async def debate_stop(req: Request):
    """叫停。?run_id= 只停那一场；不带就全停（单场时跟原来一样）。"""
    target = (req.query_params.get("run_id") or "").strip()
    victims = [(rid, row) for rid, row in list(_RUNS.items())
               if (not target or rid == target) and row.get("task") is not None]
    if not victims:
        return JSONResponse({"ok": True, "note": "not running", "stopped": []})
    stopped = []
    for rid, row in victims:
        row["task"].cancel()
        stopped.append(rid)
    # 被 cancel 的 task 在自己的 finally 里注销；这里不抢着删，免得它 finally 里找不到 out_path。
    await _emit_to_room("比赛已被叫停。" if len(stopped) == 1 else f"{len(stopped)} 场比赛已被叫停。",
                        title="🛑 主持人")
    return JSONResponse({"ok": True, "stopped": stopped})
