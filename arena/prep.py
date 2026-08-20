"""Small, auditable contracts for debate preparation and blind judging.

This module deliberately contains no model or network calls.  The room runner owns
those side effects; the functions here only build bounded prompts, validate model
output, blind transcripts and aggregate ballots.  Keeping that boundary pure makes
the parts that decide who spoke, what evidence a judge saw, and whether a verdict is
stable straightforward to test.

致谢：评分细则的一份详细评阅来自 Elliot 和 Laurie。本模块里「每位评委判两遍、对调票不计票」
（第 3 节）、「事实基座与举证在引用方」（第 2 节）、tools/consistency.py 的统计口径（第 6 节）、
tools/bench_overlap.py 的前置实验（第 4.2 节）都直接来自那份评阅。谢谢他们。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Iterable, Mapping, Optional, Sequence


BOARD_MAX_CHARS = 800
SCOUT_MAX_CHARS = 1600
TEAM_REVIEW_MAX_CHARS = 1200
VALID_BALLOT_WINNERS = {"A", "B", "tie", "uncertain"}


def _strip_json_fence(text: str) -> str:
    value = (text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, count=1, flags=re.I)
        value = re.sub(r"\s*```$", "", value, count=1)
    return value.strip()


def _json_object(text: str) -> dict | None:
    value = _strip_json_fence(text)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        match = re.search(r"\{.*\}", value, re.S)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except (TypeError, ValueError):
            return None
    return parsed if isinstance(parsed, dict) else None


def _clean_text(value: object, *, limit: int) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[\t ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:limit]


# 评委引用句子时习惯在截断处补个句号（首场三张票全因「逗号被抄成句号」
# 一票一票冤死，judge_failed）。逐字校验防的是编造原话，不是防标点誊写差异：
# 匹配前把空白和常见中英标点从两边剥掉，只比字。
_QUOTE_NOISE = re.compile(r"[\s，。、；：！？…—·「」『』（）\"'‘’“”,.;:!?()\[\]-]+")


def _quote_key(text: str) -> str:
    return _QUOTE_NOISE.sub("", text or "")


def _clean_list(value: object, *, limit: int, item_limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        clean = _clean_text(item, limit=item_limit)
        if clean and clean not in out:
            out.append(clean)
        if len(out) >= limit:
            break
    return out


@dataclass(frozen=True)
class ScoutBrief:
    scout: str
    preferred_role: str
    main_case: list[str] = field(default_factory=list)
    opponent_best_case: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    raw_status: str = "parsed"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TeamPlan:
    side: str
    opening_label: str
    rebuttal_label: str
    board: str
    source_urls: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    raw_status: str = "parsed"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TeamReview:
    reviewer: str
    strongest_shared: str = ""
    challenge_to_partner: str = ""
    preferred_role: str = "opening"
    division: dict[str, str] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)
    raw_status: str = "parsed"
    turn_index: int = 0
    reply_to_turn_index: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


def build_scout_prompt(
    *,
    topic: str,
    stance: str,
    opponent_stance: str,
    scout_label: str,
    reference_paths: Sequence[str] = (),
) -> str:
    references = "\n".join(f"- {path}" for path in reference_paths[:20]) or "- 无本地材料"
    return f"""你是{scout_label}，现在是赛前独立收集轮，不是正式发言。

辩题：{topic}
我方：{stance}
对方：{opponent_stance}

你可以自行决定是否读取下面列出的本地资料；它们是工具箱，不是必须模仿的师父：
{references}

任务：
1. 找出我方最硬的三条论证和对方最强的一条论证；禁止只打稻草人。
2. 收集至多三条可核对的案例、数据或原文。只有亲自读到来源才能写 source_urls；
   没核到就放 uncertainties，不能凭印象编数字或网址。
3. 说明你更适合 opening（立论）还是 rebuttal（驳论），只选一个。
4. 这是备赛笔记，不写比赛台词，不模仿名人，不规定文风。

只输出一个 JSON 对象，不加代码围栏：
{{
  "preferred_role": "opening|rebuttal",
  "main_case": ["..."],
  "opponent_best_case": ["..."],
  "evidence": ["..."],
  "source_urls": ["https://..."],
  "uncertainties": ["..."]
}}
"""


def parse_scout_brief(raw: str, *, scout_label: str) -> ScoutBrief:
    data = _json_object(raw)
    if data is None:
        return ScoutBrief(
            scout=scout_label,
            preferred_role="opening",
            uncertainties=[_clean_text(raw, limit=SCOUT_MAX_CHARS)] if raw.strip() else ["empty_output"],
            raw_status="unparsed",
        )
    role = str(data.get("preferred_role") or "").strip().lower()
    if role not in {"opening", "rebuttal"}:
        role = "opening"
    urls = [
        url for url in _clean_list(data.get("source_urls"), limit=6, item_limit=300)
        if re.fullmatch(r"https?://[^\s]+", url)
    ]
    return ScoutBrief(
        scout=scout_label,
        preferred_role=role,
        main_case=_clean_list(data.get("main_case"), limit=3, item_limit=260),
        opponent_best_case=_clean_list(data.get("opponent_best_case"), limit=2, item_limit=260),
        evidence=_clean_list(data.get("evidence"), limit=3, item_limit=320),
        source_urls=urls,
        uncertainties=_clean_list(data.get("uncertainties"), limit=4, item_limit=260),
    )


def build_team_deliberation_prompt(
    *,
    topic: str,
    stance: str,
    opponent_stance: str,
    member_labels: Sequence[str],
    briefs: Sequence[ScoutBrief],
    reviews: Sequence[TeamReview] = (),
) -> str:
    brief_json = json.dumps([b.to_dict() for b in briefs], ensure_ascii=False, indent=2)
    review_json = json.dumps([r.to_dict() for r in reviews], ensure_ascii=False, indent=2)
    members = "、".join(member_labels)
    return f"""你们正在做赛前队内讨论的最后收束。

辩题：{topic}
我方：{stance}
对方：{opponent_stance}
队员：{members}

两份独立收集笔记如下：
{brief_json}

队友看完双方笔记后的真实回应如下：
{review_json}

请做三件事：
1. 由队员能力与笔记内容自行决定谁做 opening、谁做 rebuttal；不得按预设模型排名。
2. 合成一块不超过 {BOARD_MAX_CHARS} 个中文字符的战术板：比较标准、三条主线、对方最强点、
   质询目标、谁负责什么。原始资料不要整段搬进去。
3. 保留尚未核实或队内未解决的分歧，不许为了显得完整把它抹掉。

只输出一个 JSON 对象，不加代码围栏：
{{
  "opening_label": "队员原名",
  "rebuttal_label": "队员原名",
  "board": "...",
  "source_urls": ["只保留收集笔记里已经出现的 URL"],
  "unresolved": ["..."]
}}
"""


def build_peer_review_prompt(
    *,
    topic: str,
    stance: str,
    opponent_stance: str,
    reviewer_label: str,
    partner_label: str,
    briefs: Sequence[ScoutBrief],
    prior_reviews: Sequence[TeamReview] = (),
) -> str:
    brief_json = json.dumps([b.to_dict() for b in briefs], ensure_ascii=False, indent=2)
    prior_json = json.dumps([r.to_dict() for r in prior_reviews], ensure_ascii=False, indent=2)
    division_example = json.dumps(
        {reviewer_label: "你主打的主线", partner_label: "队友主打的主线"},
        ensure_ascii=False,
    )
    if prior_reviews:
        turn_context = f"""队友已经先说，下面是此前按顺序发生的队内发言：
{prior_json}

必须直接回应队友已经说出的 strongest_shared、challenge_to_partner 和 unresolved；
可以赞同、修正或保留分歧，不能假装没听见。你是第二位发言者，必须在 division 里
给出最终的两人主线分工，两项不能重复。"""
    else:
        turn_context = "你是本队第一位发言者：先提出共同主线、对队友笔记的质疑、角色偏好和分工建议。"
    return f"""现在进入队内讨论，不是正式发言。你是 {reviewer_label}，队友是 {partner_label}。

辩题：{topic}
我方：{stance}
对方：{opponent_stance}

你们两人的独立收集笔记：
{brief_json}

{turn_context}

请真的回应队友：指出两份笔记可以共用的最强主线，也指出队友方案里最该修的一处；
再说明你更适合 opening 还是 rebuttal。不要写赛场台词，不要假装分歧已经消失。

只输出一个 JSON 对象，不加代码围栏：
{{
  "strongest_shared": "...",
  "challenge_to_partner": "...",
  "preferred_role": "opening|rebuttal",
  "division": {division_example},
  "unresolved": ["..."]
}}
"""


def parse_team_review(
    raw: str,
    *,
    reviewer_label: str,
    member_labels: Sequence[str] = (),
    turn_index: int = 0,
    reply_to_turn_index: Optional[int] = None,
) -> TeamReview:
    data = _json_object(raw)
    if data is None:
        return TeamReview(
            reviewer=reviewer_label,
            challenge_to_partner=_clean_text(raw, limit=TEAM_REVIEW_MAX_CHARS),
            raw_status="unparsed",
            turn_index=turn_index,
            reply_to_turn_index=reply_to_turn_index,
        )
    role = str(data.get("preferred_role") or "").strip().lower()
    if role not in {"opening", "rebuttal"}:
        role = "opening"
    raw_division = data.get("division")
    allowed = list(dict.fromkeys(str(x) for x in member_labels if x))
    division: dict[str, str] = {}
    if isinstance(raw_division, Mapping):
        keys = allowed or [str(x) for x in raw_division.keys()][:2]
        for label in keys:
            value = _clean_text(raw_division.get(label), limit=260)
            if value:
                division[label] = value
    return TeamReview(
        reviewer=reviewer_label,
        strongest_shared=_clean_text(data.get("strongest_shared"), limit=500),
        challenge_to_partner=_clean_text(data.get("challenge_to_partner"), limit=500),
        preferred_role=role,
        division=division,
        unresolved=_clean_list(data.get("unresolved"), limit=4, item_limit=260),
        turn_index=turn_index,
        reply_to_turn_index=reply_to_turn_index,
    )


def format_team_review_turn(review: TeamReview, *, total_turns: int = 2) -> str:
    """Render one ordered prep turn for the live room without leaking raw JSON."""
    turn = review.turn_index or 1
    reply = (
        f"（回应第 {review.reply_to_turn_index} 轮）"
        if review.reply_to_turn_index
        else "（先发言）"
    )
    if review.raw_status != "parsed" and not any((
        review.strongest_shared,
        review.challenge_to_partner,
        review.division,
        review.unresolved,
    )):
        return (
            f"**第 {turn}/{total_turns} 轮{reply}**\n\n"
            "这位队员本轮没有形成有效回应；后续仍会用其独立收集笔记降级，不让整队裸打。"
        )
    lines = [f"**第 {turn}/{total_turns} 轮{reply}**"]
    if review.strongest_shared:
        lines.append(f"**共同主线**：{review.strongest_shared}")
    if review.challenge_to_partner:
        lines.append(f"**给队友的挑战**：{review.challenge_to_partner}")
    if review.division:
        division = "；".join(f"{name}：{work}" for name, work in review.division.items())
        lines.append(f"**建议分工**：{division}")
    if review.unresolved:
        lines.append(f"**仍未解决**：{'；'.join(review.unresolved)}")
    return "\n\n".join(lines)


# ── 各带各的笔记上场 ──
# 她原话：「不是自己带自己的笔记吗，只是内部双方会有交流。我想的是 ai 自己搜集，然后交流，
# 整理，上场」。旧实现是队长一人收束一块队级板、全队共用——队长挂了整队裸打，而且
# 队友的笔记根本进不了正赛。现在四步：搜集（各自）→ 交流（A→B 有序回应）
# → 整理（各写自己的上场板）→ 上场（各带各的）。队级 TeamPlan 只留角色分配和交流摘要。
PERSONAL_BOARD_MAX_CHARS = 600


@dataclass(frozen=True)
class PersonalBoard:
    label: str
    board: str
    preferred_role: str = "opening"
    unresolved: list[str] = field(default_factory=list)
    raw_status: str = "parsed"

    def to_dict(self) -> dict:
        return asdict(self)


def build_personal_board_prompt(
    *,
    topic: str,
    stance: str,
    opponent_stance: str,
    my_label: str,
    partner_label: str,
    briefs: Sequence[ScoutBrief],
    reviews: Sequence[TeamReview] = (),
    opening_label: str,
    rebuttal_label: str,
    mainline_division: Mapping[str, str],
) -> str:
    brief_json = json.dumps([b.to_dict() for b in briefs], ensure_ascii=False, indent=2)
    review_json = json.dumps([r.to_dict() for r in reviews], ensure_ascii=False, indent=2)
    role_assignment = {
        "opening_label": opening_label,
        "rebuttal_label": rebuttal_label,
        "mainline_division": dict(mainline_division),
    }
    role_json = json.dumps(role_assignment, ensure_ascii=False, indent=2)
    assigned_role = "opening" if my_label == opening_label else "rebuttal"
    return f"""队内交流结束，现在各自整理自己上场要带的笔记。你是 {my_label}，队友是 {partner_label}。

辩题：{topic}
我方：{stance}
对方：{opponent_stance}

你们两人的独立收集笔记：
{brief_json}

交流轮里两人互相的回应：
{review_json}

系统已经根据交流结果锁定角色，个人整理不得再改：
{role_json}
你的固定角色：{assigned_role}

请写**你自己**上场带的笔记（不超过 {PERSONAL_BOARD_MAX_CHARS} 个中文字符）：
1. 你这一席打算主打的主线（可以吸收队友的东西，但署名是你自己的判断）；
2. 你和队友的分工——你负责什么、队友负责什么，避免两人上场说同一件事；
3. 对方最强的一点和你准备怎么接；
4. 你自己还没核实、上场不能当事实说的东西。
这是后台笔记不是台词；不要写赛场发言，不要规定文风。

只输出一个 JSON 对象，不加代码围栏：
{{
  "preferred_role": "opening|rebuttal",
  "board": "...",
  "unresolved": ["..."]
}}
"""


def parse_personal_board(
    raw: str,
    *,
    label: str,
    my_brief: Optional[ScoutBrief] = None,
    assigned_role: str = "",
) -> PersonalBoard:
    """整理失败就退回自己的收集笔记（不是空板）；收集也没有才真的裸打。"""
    locked_role = assigned_role if assigned_role in {"opening", "rebuttal"} else ""
    data = _json_object(raw)
    if data is not None:
        role = str(data.get("preferred_role") or "").strip().lower()
        if role not in {"opening", "rebuttal"}:
            role = my_brief.preferred_role if my_brief else "opening"
        role = locked_role or role
        board = _clean_text(data.get("board"), limit=PERSONAL_BOARD_MAX_CHARS)
        if board:
            return PersonalBoard(
                label=label, board=board, preferred_role=role,
                unresolved=_clean_list(data.get("unresolved"), limit=4, item_limit=260),
            )
    if my_brief is not None:
        stitched = board_from_briefs([my_brief], limit=PERSONAL_BOARD_MAX_CHARS)
        if stitched:
            return PersonalBoard(
                label=label, board="【整理失败，以下是我自己的收集笔记】\n" + stitched,
                preferred_role=locked_role or my_brief.preferred_role,
                raw_status="stitched_from_brief",
            )
    return PersonalBoard(
        label=label,
        board="",
        preferred_role=locked_role or "opening",
        raw_status="unparsed",
    )


def decide_roles(
    labels: Sequence[str],
    *,
    reviews: Sequence[TeamReview] = (),
    briefs: Sequence[ScoutBrief] = (),
    boards: Sequence[PersonalBoard] = (),
) -> tuple[str, str]:
    """两人各报 opening/rebuttal，一人一个就照办；撞车按收集轮偏好，再撞车按入队顺序。"""
    names = list(dict.fromkeys(str(x) for x in labels if x))
    if len(names) < 2:
        raise ValueError("need two distinct members")
    a, b = names[0], names[1]

    def pref(source: Sequence, key: str, who: str) -> str:
        for row in source:
            if getattr(row, key) == who and row.raw_status != "unparsed":
                return row.preferred_role
        return ""

    for source, key in ((boards, "label"), (reviews, "reviewer"), (briefs, "scout")):
        pa, pb = pref(source, key, a), pref(source, key, b)
        if pa and pb and pa != pb:
            return (a, b) if pa == "opening" else (b, a)
    return a, b


def decide_mainline_division(
    labels: Sequence[str],
    *,
    reviews: Sequence[TeamReview] = (),
    briefs: Sequence[ScoutBrief] = (),
    opening_label: str = "",
    rebuttal_label: str = "",
) -> dict[str, str]:
    """取最后一份完整且不重复的队内分工；没有就按各自收集笔记确定性降级。"""
    names = list(dict.fromkeys(str(x) for x in labels if x))[:2]
    for review in reversed(reviews):
        division = {name: _clean_text(review.division.get(name), limit=260) for name in names}
        if all(division.values()) and len(set(division.values())) == len(names):
            return division

    brief_of = {brief.scout: brief for brief in briefs}
    fallback: dict[str, str] = {}
    for index, name in enumerate(names):
        brief = brief_of.get(name)
        if brief and brief.main_case:
            fallback[name] = brief.main_case[0]
        elif name == opening_label or (not opening_label and index == 0):
            fallback[name] = "共同主线、定义与立论"
        else:
            fallback[name] = "对方最强论点与机制反驳"
    return fallback


def apply_personal_boards(roster: list[dict], *, side: str, opening_label: str,
                          rebuttal_label: str, boards: Sequence[PersonalBoard],
                          fmt: str = "mini") -> None:
    """mini：按角色重排一二辩，各席挂自己的板；full：席位不动，同一模型的席位共用它自己的板。"""
    by_label = {pb.label: pb.board for pb in boards}
    members = [row for row in roster if row.get("side") == side]
    for row in members:
        row["strategy_board"] = by_label.get(str(row.get("label")), "")
    if fmt != "mini" or len(members) != 2:
        return
    rows = {str(row.get("label")): row for row in members}
    opener, rebutter = rows.get(opening_label), rows.get(rebuttal_label)
    if opener is None or rebutter is None or opener is rebutter:
        return
    prefix = "正" if side == "pro" else "反"
    opener["seat"], opener["name"] = 1, f"{prefix}方一辩"
    rebutter["seat"], rebutter["name"] = 2, f"{prefix}方二辩"


def board_from_briefs(briefs: Sequence[ScoutBrief], *, limit: int = BOARD_MAX_CHARS) -> str:
    """队长收束挂了、还有队员笔记活着时，把笔记直接拼成战术板。

    实测踩过：一队的队长模型三次调用全败（CLI 参数冲突），队友的三条主论点和队内评审
    都写好了，却因为队长那一步失败，整队拿着「备赛输出无法解析」裸打。一个人挂不该毁整队。
    """
    parts: list[str] = []
    for brief in briefs:
        if brief.raw_status != "parsed" or not brief.main_case:
            continue
        parts.append(f"{brief.scout} 收集：")
        parts.extend(f"- {item}" for item in brief.main_case[:3])
        if brief.opponent_best_case:
            parts.append(f"- 对方最强：{brief.opponent_best_case[0]}")
        if brief.uncertainties:
            parts.append(f"- 未核实：{brief.uncertainties[0]}")
    return _clean_text("\n".join(parts), limit=limit)


def parse_team_plan(
    raw: str,
    *,
    side: str,
    member_labels: Sequence[str],
    briefs: Sequence[ScoutBrief],
) -> TeamPlan:
    labels = list(dict.fromkeys(str(x) for x in member_labels if x))
    if len(labels) < 2:
        raise ValueError("team plan requires at least two distinct members")
    data = _json_object(raw)
    allowed_urls = {url for brief in briefs for url in brief.source_urls}
    if data is None:
        stitched = board_from_briefs(briefs)
        if stitched:
            return TeamPlan(side, labels[0], labels[1],
                            "【队长收束失败，以下是队员各自的收集笔记】\n" + stitched,
                            raw_status="stitched_from_briefs")
        board = _clean_text(raw, limit=BOARD_MAX_CHARS) or "备赛输出无法解析，正赛仅使用立场与场上实录。"
        return TeamPlan(side, labels[0], labels[1], board, raw_status="unparsed")

    opening = str(data.get("opening_label") or "").strip()
    rebuttal = str(data.get("rebuttal_label") or "").strip()
    if opening not in labels or rebuttal not in labels or opening == rebuttal:
        opening, rebuttal = labels[0], labels[1]
        status = "fallback_roles"
    else:
        status = "parsed"
    urls = [
        url for url in _clean_list(data.get("source_urls"), limit=8, item_limit=300)
        if url in allowed_urls
    ]
    board = _clean_text(data.get("board"), limit=BOARD_MAX_CHARS)
    if not board:
        board = "备赛没有形成有效战术板，正赛仅使用立场与场上实录。"
        status = "fallback_board" if status == "parsed" else status
    return TeamPlan(
        side=side,
        opening_label=opening,
        rebuttal_label=rebuttal,
        board=board,
        source_urls=urls,
        unresolved=_clean_list(data.get("unresolved"), limit=5, item_limit=260),
        raw_status=status,
    )


def apply_mini_role_choice(roster: list[dict], plans: Mapping[str, TeamPlan]) -> None:
    """Apply the two-team role choice without changing model identity or style."""
    for side in ("pro", "con"):
        plan = plans.get(side)
        members = [row for row in roster if row.get("side") == side]
        if plan is None or len(members) != 2:
            continue
        by_label = {str(row.get("label")): row for row in members}
        opener = by_label.get(plan.opening_label)
        rebutter = by_label.get(plan.rebuttal_label)
        if opener is None or rebutter is None or opener is rebutter:
            continue
        opener["seat"], opener["name"] = 1, f"{'正' if side == 'pro' else '反'}方一辩"
        rebutter["seat"], rebutter["name"] = 2, f"{'正' if side == 'pro' else '反'}方二辩"
        opener["strategy_board"] = plan.board
        rebutter["strategy_board"] = plan.board


def verify_opponent_quotes(
    text: str,
    *,
    side: str,
    transcript: Sequence[dict],
    crossfire: Sequence[dict] = (),
) -> list[dict]:
    """Return exact quoted fragments that cannot be found in prior opponent speech."""
    other = "con" if side == "pro" else "pro"
    speaker_side = {
        str(row.get("speaker") or ""): str(row.get("side") or "")
        for row in transcript
    }
    source_text = [
        str(row.get("text") or "") for row in transcript if row.get("side") == other
    ]
    for block in crossfire:
        for exchange in block.get("exchanges") or []:
            if speaker_side.get(str(exchange.get("asker") or "")) == other:
                source_text.append(str(exchange.get("q") or ""))
            if speaker_side.get(str(exchange.get("answerer") or "")) == other:
                source_text.append(str(exchange.get("a") or ""))
    haystack = "\n".join(source_text)
    normalized_haystack = re.sub(r"\s+", "", haystack)
    findings: list[dict] = []
    # Consume balanced pairs before applying the length threshold.  Otherwise a
    # short pair's closing quote can be mistaken for a later opening quote.
    pattern = re.compile(r"「([^」\n]*)」|“([^”\n]*)”|\"([^\"\n]*)\"")
    for match in pattern.finditer(text or ""):
        quote = next((value for value in match.groups() if value is not None), "").strip()
        if len(re.sub(r"\s+", "", quote)) < 6:
            continue
        if re.sub(r"\s+", "", quote) not in normalized_haystack:
            findings.append({"quote": quote, "status": "not_exactly_found"})
    return findings


def ordered_debate_events(
    transcript: Sequence[dict],
    crossfire: Sequence[dict] = (),
    *,
    stage_order: Sequence[str] = (),
) -> list[dict]:
    """Merge speech and crossfire ledgers back into their real schedule order.

    New records carry ``schedule_index``.  For older records, repeated stage
    names are assigned to successive matching positions, so both full-format
    free-debate turns survive export and resume migrations.
    """
    occurrences: dict[str, list[int]] = {}
    for index, stage in enumerate(stage_order):
        occurrences.setdefault(str(stage), []).append(index)
    used_by_stage: dict[str, int] = {}
    events: list[dict] = []

    def add(kind: str, row: dict, ledger_index: int) -> None:
        raw_index = row.get("schedule_index")
        if type(raw_index) is int and raw_index >= 0:
            sequence = raw_index
        else:
            stage = str(row.get("stage") or "")
            seen = used_by_stage.get(stage, 0)
            positions = occurrences.get(stage, [])
            sequence = positions[seen] if seen < len(positions) else len(stage_order) + ledger_index
            used_by_stage[stage] = seen + 1
        events.append({
            "kind": kind,
            "row": row,
            "sequence": sequence,
            "ledger_index": ledger_index,
        })

    for index, row in enumerate(transcript):
        add("speech", row, index)
    for index, row in enumerate(crossfire):
        add("crossfire", row, index)
    return sorted(events, key=lambda event: (
        event["sequence"], 0 if event["kind"] == "speech" else 1, event["ledger_index"]
    ))


def speaker_ids(transcript: Sequence[dict]) -> dict[str, str]:
    """盲审席号：按转录里首次发言顺序编 P01..P08。blind_transcript 渲染和 parse_ballot 解 MVP 共用，
    两边必须用同一条规则，否则评委填的席号对不回选手。"""
    speaker_to_id: dict[str, str] = {}
    for row in transcript:
        speaker = str(row.get("speaker") or "")
        speaker_to_id.setdefault(speaker, f"P{len(speaker_to_id) + 1:02d}")
    return speaker_to_id


def blind_transcript(
    transcript: Sequence[dict],
    crossfire: Sequence[dict] = (),
    *,
    swap: bool = False,
    stage_order: Sequence[str] = (),
) -> tuple[str, dict[str, str]]:
    """Render a model-free transcript.  ``swap`` flips A/B presentation labels."""
    side_to_label = {"pro": "B", "con": "A"} if swap else {"pro": "A", "con": "B"}
    speaker_to_id = speaker_ids(transcript)
    lines: list[str] = []
    crossfire_ids = {id(row): f"Q{index:02d}" for index, row in enumerate(crossfire, 1)}

    for event in ordered_debate_events(
        transcript, crossfire, stage_order=stage_order,
    ):
        row = event["row"]
        if event["kind"] == "crossfire":
            lines.append(f"[{crossfire_ids[id(row)]}] 交互质询")
            for ex in row.get("exchanges", []):
                asker = str(ex.get("asker") or "")
                answerer = str(ex.get("answerer") or "")
                lines.append(
                    f"问（{speaker_to_id.get(asker, '未知席位')}）：{ex.get('q') or ''}\n"
                    f"答（{speaker_to_id.get(answerer, '未知席位')}）：{ex.get('a') or ''}"
                )
            continue
        index = event["ledger_index"] + 1
        speech_id = f"S{index:02d}"
        speaker = str(row.get("speaker") or f"speaker-{index}")
        team = side_to_label.get(str(row.get("side")), "?")
        stage = str(row.get("stage") or "发言")
        stage = stage.replace("正方", f"队{side_to_label['pro']}").replace("反方", f"队{side_to_label['con']}")
        lines.append(
            f"[{speech_id}] 队{team} · 席{speaker_to_id[speaker]} · {stage}\n"
            f"{row.get('text') or ''}"
        )
    return "\n\n".join(lines), side_to_label


# 明德杯记分：尺子分 70 + 自留分 30 + 决胜票。
# 尺子分：四项尺子每项 A/B 各 0–10，代码折算成 70 满分（×1.75）；
# 自留分：评委按自己的辩论观给的整体印象分，A/B 各 0–30；
# 决胜票：不管分数怎么算，胜负以 winner 为准——票归票、分归分，像真人评委席那样
#   分数只公示不定胜负。分票不一致时标 score_vote_consistent=False 供观众看。
RUBRIC_ITEMS = (
    "题面负担与论证链是否真的成立",
    "是否回应了对方最强论点，而不是稻草人",
    "例证、事实和引文是否支持结论",
    "全队是否一贯、席位是否完成职责、表达是否清楚",
)
RUBRIC_ITEM_MAX = 10
RUBRIC_TOTAL_MAX = 70
DISCRETION_MAX = 30
BENCH_Q_CHARS = 60
BENCH_A_CHARS = 120


# Elliot 评阅第 2 节：华辩靠对抗制默认「没人质疑的事实视为成立」，前提是辩手伪造事实有成本；
# AI 辩手没有这个成本，且双方都是 AI 时质疑环节兜不住。补法不引英辩中立背景段，用第 1 条「题面负担」承载：
# 题面里写明事实基座（双方共享前提），基座之外的具体数据/事件/引文默认不予采信，举证责任在引用方。
FACT_RULE_JUDGE = (
    "事实采信规则：题面写明的事实基座是双方共享前提；基座之外辩手自行给出的具体数据、事件、引文，"
    "默认不予采信，不因对方没有质疑而成立。举证责任在引用方：被对方质疑而给不出处或答不上的，按论证缺陷扣；"
    "你自己也不替任何一方补证据。例证只在「它真的支持结论」这个意义上计分，不在「它听起来像真的」上计分。"
)


def build_ballot_prompt(*, topic: str, blinded: str, bench_qa: str = "",
                        precedent: str = "", fact_base: str = "") -> str:
    rubric = "\n".join(f"{i}. {item}；" for i, item in enumerate(RUBRIC_ITEMS, 1))
    bench_block = (
        f"\n评委席插问实录（闭幕后评委各问一问、被问方当场作答；答得正面与否计入评分）：\n{bench_qa}\n"
        if bench_qa else ""
    )
    # 同题往届稿辩手不能看、评审可以看。只给真人评委的判词，用来校准
    # 这道题该怎么衡量；不是本场证据，也不许因为哪队打法像往届赢家就加分。
    precedent_block = (
        "\n同题往届真人评委发言（仅供校准衡量尺度；辩手赛前没有看过这份材料。"
        "它不是本场证据，不许因某队打法像往届胜方或引了往届观点而加分或扣分）：\n"
        f"{precedent}\n"
        if precedent else ""
    )
    fact_block = (
        f"题面事实基座（双方共享的前提事实，不需要举证）：{fact_base}"
        if fact_base.strip() else "题面事实基座：本题未设，双方不共享任何具体数据或事件前提。"
    )
    return f"""你是一名独立辩论裁判。你不知道队伍使用了什么模型，也看不到主持人的评价。
请只按实际发生的完整转录裁决，不能因篇幅更长、先后位置或文风偏好投票。
只评论证质量与交锋水平，不评立场本身的道德倾向——立场是抽签抽来的，
你觉得哪一方「更安全」「更符合主流价值」不构成投票理由；一方论证得更狠、更完整，
即使结论让你不舒服，票也应该给它。

辩题：{topic}
{fact_block}
{FACT_RULE_JUDGE}

四项尺子（每项给 A、B 各打 0–{RUBRIC_ITEM_MAX} 分）：
{rubric}
自留分（0–{DISCRETION_MAX}）：按你自己的辩论观给 A、B 各打一个整体印象分，
可以奖励尺子没覆盖到的东西（洞见、勇气、语言的准确与美感）。
决胜票：不论分数如何，你必须投出一票，A 或 B；不允许弃权、不允许平票。
最佳辩手：从场上所有席位（P01、P02……）里选一位这场表现最好的，填席号；可以来自你没投的那一队。

转录：
{blinded}
{bench_block}{precedent_block}
只输出一个 JSON 对象，不加代码围栏：
{{
  "rubric_scores": {{"A": [0, 0, 0, 0], "B": [0, 0, 0, 0]}},
  "discretion": {{"A": 0, "B": 0}},
  "winner": "A|B",
  "mvp": "P01",
  "margin": "clear|narrow",
  "reason": "决胜点，至多240字",
  "uncertainty": "最大不确定性，至多160字",
  "evidence": [
    {{"speech_id": "S01", "quote": "不超过25字的逐字短引"}},
    {{"speech_id": "S02", "quote": "不超过25字的逐字短引"}}
  ]
}}
"""


def build_bench_question_prompt(*, topic: str, blinded: str) -> str:
    """评委席插问：闭幕后每位评委向一方提一个问题（moot bench question）。

    AI 最会念稿——写好的立论怎么都漂亮；插问逼即时应答才见真水平。
    问题必须针对转录里真实出现过的论证漏洞，不许泛问。
    """
    return f"""你是这场辩论赛的评委之一。全场发言已经结束，现在是评委席插问环节：
你可以向 A 或 B 队提一个问题，被问方须当场作答，答得正面与否将计入评分。
问题必须针对转录里真实出现过的论证漏洞（他们回避了什么、哪一步没接上），
不许泛泛而问，不许在问题里替他们回答，不许评价谁更强。
一句话，不超过 {BENCH_Q_CHARS} 字。

辩题：{topic}

转录：
{blinded}

只输出一个 JSON 对象，不加代码围栏：
{{"target": "A|B", "question": "不超过{BENCH_Q_CHARS}字的问题"}}
"""


def parse_bench_question(raw: str, *, side_to_label: Mapping[str, str]) -> dict | None:
    """把评委的插问解析成 {target: pro|con, question}；解析不出就 None（该席弃问）。"""
    data = _json_object(raw)
    if data is None:
        return None
    label = str(data.get("target") or "").strip().upper()
    label_to_side = {value: key for key, value in side_to_label.items()}
    side = label_to_side.get(label)
    question = _clean_text(data.get("question"), limit=BENCH_Q_CHARS).replace("\n", " ")
    if side not in {"pro", "con"} or not question:
        return None
    return {"target": side, "question": question}


def _score_row(value: object, *, count: int, each_max: int) -> list[int] | None:
    if not isinstance(value, list) or len(value) != count:
        return None
    out: list[int] = []
    for item in value:
        try:
            num = int(round(float(item)))
        except (TypeError, ValueError):
            return None
        out.append(max(0, min(each_max, num)))
    return out


def _parse_scores(data: Mapping[str, object], *, label_to_side: Mapping[str, str]) -> dict | None:
    """明德杯记分：缺字段或格式不对返回 None（票仍有效——决胜票才定胜负）。"""
    rubric = data.get("rubric_scores")
    disc = data.get("discretion")
    if not isinstance(rubric, dict) or not isinstance(disc, dict):
        return None
    scores: dict[str, dict] = {}
    for label in ("A", "B"):
        row = _score_row(rubric.get(label), count=len(RUBRIC_ITEMS), each_max=RUBRIC_ITEM_MAX)
        if row is None:
            return None
        try:
            own = int(round(float(disc.get(label))))
        except (TypeError, ValueError):
            return None
        own = max(0, min(DISCRETION_MAX, own))
        rubric_total = round(sum(row) * RUBRIC_TOTAL_MAX / (RUBRIC_ITEM_MAX * len(RUBRIC_ITEMS)), 1)
        side = label_to_side.get(label, label)
        scores[side] = {
            "rubric": row,
            "rubric_total": rubric_total,
            "discretion": own,
            "total": round(rubric_total + own, 1),
        }
    return scores


def parse_ballot(
    raw: str,
    *,
    transcript: Sequence[dict],
    side_to_label: Mapping[str, str],
    ballot_id: str,
    bench: Sequence[Mapping[str, object]] = (),
) -> dict:
    data = _json_object(raw)
    if data is None:
        return {"ballot_id": ballot_id, "valid": False, "error": "unparseable", "raw": raw[:2000]}
    winner = str(data.get("winner") or "").strip()
    if winner.lower() in {"tie", "uncertain"}:
        winner = winner.lower()
    else:
        winner = winner.upper()
    if winner not in VALID_BALLOT_WINNERS:
        return {"ballot_id": ballot_id, "valid": False, "error": "bad_winner", "raw": raw[:2000]}

    speech_text = {f"S{i:02d}": str(row.get("text") or "") for i, row in enumerate(transcript, 1)}
    # 首场（debate-20260818-162449）：GPT-5.5 那票引了插问答复里的原话「目标没换、努力没停」，
    # 却只能标 S06——转录里没有，整票冤死。评委看得见插问实录（J 号），引用它也得算落地。
    bench_text = {f"J{i:02d}": str(row.get("answer") or "") for i, row in enumerate(bench, 1)}
    all_bench = " ".join(bench_text.values())
    evidence_out: list[dict] = []
    for item in data.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("speech_id") or "").strip().upper()
        quote = _clean_text(item.get("quote"), limit=80)
        qk = _quote_key(quote)
        if not qk:
            continue
        if sid in speech_text and qk in _quote_key(speech_text[sid]):
            evidence_out.append({"speech_id": sid, "quote": quote})
        elif sid in bench_text and qk in _quote_key(bench_text[sid]):
            evidence_out.append({"speech_id": sid, "quote": quote})
        elif bench and qk in _quote_key(all_bench):
            # 标错了号但话确实是插问里说的：按插问落地，号纠正
            fixed = next((k for k, v in bench_text.items() if qk in _quote_key(v)), sid)
            evidence_out.append({"speech_id": fixed, "quote": quote, "relabelled_from": sid})
    if len(evidence_out) < 2:
        return {"ballot_id": ballot_id, "valid": False, "error": "evidence_not_grounded", "raw": raw[:2000]}

    label_to_side = {label: side for side, label in side_to_label.items()}
    canonical = label_to_side.get(winner, winner)
    scores = _parse_scores(data, label_to_side=label_to_side)
    # 最佳辩手：席号（P01…）对回选手名；填错/没填 → None（票仍有效，MVP 不是决胜要件）
    id_to_speaker = {pid: spk for spk, pid in speaker_ids(transcript).items()}
    mvp_raw = str(data.get("mvp") or "").strip().upper()
    mvp = {"pid": mvp_raw, "speaker": id_to_speaker[mvp_raw]} if mvp_raw in id_to_speaker else None
    consistent: bool | None = None
    if scores and canonical in {"pro", "con"}:
        other = "con" if canonical == "pro" else "pro"
        consistent = scores[canonical]["total"] >= scores[other]["total"]
    return {
        "ballot_id": ballot_id,
        "valid": True,
        "winner": canonical,
        "presented_winner": winner,
        "margin": str(data.get("margin") or "narrow") if data.get("margin") in {"clear", "narrow"} else "narrow",
        "reason": _clean_text(data.get("reason"), limit=500),
        "uncertainty": _clean_text(data.get("uncertainty"), limit=320),
        "evidence": evidence_out,
        "scores": scores,
        "score_vote_consistent": consistent,
        "mvp": mvp,
        "presentation": dict(side_to_label),
        "raw": raw[:4000],
    }


def _ballot_role(row: Mapping[str, object]) -> str:
    """primary = 原序票（计胜负、计 MVP）；recheck = 同一评委的 A/B 对调票（只做位置自一致，不计）。
    旧票没有 role 字段：按呈现顺序推断（pro 呈现为 A → primary）。"""
    role = str(row.get("role") or "")
    if role in {"primary", "recheck"}:
        return role
    return "primary" if (row.get("presentation") or {}).get("pro", "A") == "A" else "recheck"


def _judge_key(row: Mapping[str, object]) -> str:
    return str(row.get("judge") or row.get("judge_label") or "")


def aggregate_ballots(ballots: Iterable[Mapping[str, object]]) -> dict:
    """决胜、位置复判、MVP 三件事分开算（见致谢里的评阅第 3 节）：

    - 决胜票只数 primary（原序）票，多数决；recheck 票不计票。
    - 位置复判：同一位评委 primary 与 recheck 都有效时，两票 winner 不同 = 这位评委自己位置不稳。
      少数评委不稳只标注（position_unstable_judges）；多数评委都不稳 = 评委席在投位置不是投论证，
      裁决作废（status=position_unstable）。旧式「换位票由另一位评委判」配不上对，算没复判。
    - MVP：primary 有效票里的 mvp.speaker 计票；平票先看胜方席位，再看第一席所投；仍平 → 不出 MVP。
    """
    rows = [dict(row) for row in ballots]
    valid = [row for row in rows if row.get("valid")]
    primary = [row for row in valid if _ballot_role(row) == "primary"]
    counts = {key: sum(1 for row in primary if row.get("winner") == key) for key in ("pro", "con", "tie", "uncertain")}
    if len(primary) < 2:
        return {"status": "judge_failed", "winner": None, "counts": counts, "valid_ballots": len(valid),
                "primary_ballots": len(primary), "ballots": rows}

    pro_con = {key: counts[key] for key in ("pro", "con")}
    winner = max(pro_con, key=pro_con.get)
    majority = pro_con[winner] >= 2 and pro_con[winner] > pro_con["con" if winner == "pro" else "pro"]

    # 位置自一致：按评委配对 primary/recheck
    checked_judges: list[str] = []
    unstable_judges: list[str] = []
    rechecks = {_judge_key(row): row for row in valid if _ballot_role(row) == "recheck" and _judge_key(row)}
    for row in primary:
        key = _judge_key(row)
        other = rechecks.get(key)
        if not key or other is None:
            continue
        checked_judges.append(key)
        if other.get("winner") != row.get("winner"):
            unstable_judges.append(key)
    position_checked = bool(checked_judges)
    position_unstable = bool(unstable_judges)
    judges_flipped_majority = position_checked and len(unstable_judges) * 2 > len(checked_judges)

    if judges_flipped_majority:
        status, winner = "position_unstable", None
    elif not majority:
        status, winner = "disputed", None
    else:
        status = "decided"

    # 明德杯分数只公示不定胜负：把带分的 primary 票加总给观众看
    scored = [row for row in primary if isinstance(row.get("scores"), dict)]
    score_totals = None
    if scored:
        score_totals = {
            side: round(sum(float(row["scores"].get(side, {}).get("total", 0)) for row in scored), 1)
            for side in ("pro", "con")
        }

    # MVP
    tally: dict[str, int] = {}
    picks_in_order: list[str] = []
    for row in primary:
        pick = row.get("mvp") or {}
        spk = str(pick.get("speaker") or "") if isinstance(pick, dict) else ""
        if not spk:
            continue
        tally[spk] = tally.get(spk, 0) + 1
        picks_in_order.append(spk)
    mvp = None
    if tally:
        top = max(tally.values())
        leaders = [spk for spk, n in tally.items() if n == top]
        if len(leaders) > 1 and winner in {"pro", "con"}:
            side_word = "正方" if winner == "pro" else "反方"
            on_winning = [spk for spk in leaders if spk.startswith(side_word)]
            if on_winning:
                leaders = on_winning
        if len(leaders) > 1:
            # 仍平：按席位顺序，第一位投给了剩余候选人的评委说了算
            leaders = [next((spk for spk in picks_in_order if spk in leaders), leaders[0])]
        if len(leaders) == 1:
            mvp = {"speaker": leaders[0], "votes": tally[leaders[0]], "of": sum(tally.values()), "tally": tally}
        else:
            mvp = {"speaker": None, "votes": top, "of": sum(tally.values()), "tally": tally, "tie": leaders}

    return {
        "status": status,
        "winner": winner,
        "counts": counts,
        "valid_ballots": len(valid),
        "primary_ballots": len(primary),
        "position_unstable": position_unstable,
        "position_checked": position_checked,
        "position_checked_judges": checked_judges,
        "position_unstable_judges": unstable_judges,
        "score_totals": score_totals,
        "scored_ballots": len(scored),
        "mvp": mvp,
        "ballots": rows,
    }


# ── 外部 AI 席位：别处的 AI 也能报名上场 ────────────────
# 辩手、评委都可以是外部 AI（aisay 上别家的 AI）；本机 CLI 只是开发期替身/补位。引擎侧不关心外部 AI
# 怎么被叫醒（aisay 桌 / 兔子洞 / 唤醒桥都行），只认一个文件协议——「投稿箱」：
#   data/debates/inbox/<run_id>/<seq:04d>-<seat>.request.json   引擎写：{system, prompt, deadline, seat, …}
#   data/debates/inbox/<run_id>/<seq:04d>-<seat>.reply.txt      桥写：外部 AI 的回复正文
# 引擎等到 deadline 没见 reply = 白卷（转录里记「未在时限内作答」，不重试、不托管代写）。
# 桥（谁去叫醒、怎么拿回稿）是独立进程，按 aisay 那边的形态另写；协议不变。

EXTERNAL_ENGINE = "external"
# request.kind 枚举——桥按它分发、外部 AI 据此知道该回什么体裁：
#   speech        正赛发言（纯文本，按 prompt 里的字数限制）
#   crossfire_q   质询提问 / crossfire_a 质询回答（一两句）
#   bench_answer  答评委插问（≤ BENCH_A_CHARS 字）
#   prep          备赛三步（scout / review / board，各自要求的 JSON）
#   ballot        评委票（JSON：winner/evidence/rubric_scores/discretion/mvp/margin/reason/uncertainty）
#   bench_question 评委插问（JSON：target/question）
EXTERNAL_KINDS = ("speech", "crossfire_q", "crossfire_a", "bench_answer", "prep", "ballot", "bench_question")


def external_request(*, run_id: str, seq: int, seat: str, system: str, prompt: str,
                     deadline_epoch: float, kind: str = "speech",
                     participant: Optional[Mapping[str, object]] = None,
                     turn: Optional[Mapping[str, object]] = None) -> dict:
    """一次向外部 AI 的出题。

    v2 adds an owner-managed participant/session envelope and turn metadata.  The
    old top-level fields stay unchanged so a v1 bridge can keep reading the same
    files.  Provider credentials, MCP configuration and memory bodies belong to
    the owner's bridge and must never enter this request.
    """
    if kind not in EXTERNAL_KINDS:
        raise ValueError(f"unknown external request kind: {kind!r}")
    request = {
        "protocol_version": 2,
        "request_id": f"{run_id}:{int(seq):04d}",
        "run_id": run_id, "seq": int(seq), "seat": seat, "kind": kind,
        "system": system, "prompt": prompt,
        "deadline_epoch": float(deadline_epoch),
    }
    if participant:
        request["participant"] = dict(participant)
    if turn:
        request["turn"] = dict(turn)
    return request


def external_paths(inbox_root, run_id: str, seq: int, seat: str) -> tuple:
    """(request_path, reply_path)。seat 里的斜杠/空白清掉，防路径逃逸。"""
    from pathlib import Path as _P
    safe_seat = "".join(ch for ch in str(seat) if ch.isalnum() or "\u4e00" <= ch <= "\u9fff") or "seat"
    folder = _P(inbox_root) / str(run_id)
    base = f"{int(seq):04d}-{safe_seat}"
    return folder / f"{base}.request.json", folder / f"{base}.reply.txt"


def external_reply_envelope_path(reply_path):
    """Structured v2 receipt beside the legacy ``.reply.txt`` projection."""
    from pathlib import Path as _P
    path = _P(reply_path)
    suffix = ".reply.txt"
    if path.name.endswith(suffix):
        return path.with_name(path.name[: -len(suffix)] + ".reply.json")
    return path.with_suffix(".json")


def external_reply_output(request: Mapping[str, object], reply: Mapping[str, object]) -> str:
    """Validate a v2 reply identity before accepting its output.

    A crossed reply is worse than a blank: it would put one owner's agent words
    in another agent's mouth.  ``failed`` and ``declined`` are honest blanks.
    """
    request_id = str(request.get("request_id") or "")
    if str(reply.get("request_id") or "") != request_id:
        raise ValueError("external reply request_id mismatch")
    participant = request.get("participant")
    expected_agent = str(participant.get("agent_id") or "") if isinstance(participant, Mapping) else ""
    if expected_agent and str(reply.get("agent_id") or "") != expected_agent:
        raise ValueError("external reply agent_id mismatch")
    status = str(reply.get("status") or "")
    if status not in {"completed", "failed", "declined"}:
        raise ValueError(f"unknown external reply status: {status!r}")
    return str(reply.get("output") or "").strip() if status == "completed" else ""


def eligible_judges(candidates: Sequence[Mapping[str, object]], roster: Sequence[Mapping[str, object]]) -> list[dict]:
    """评委回避（细则 §1）：评委不评自己主人的 AI 参赛的场。按 owner 字段比；没有 owner 的本地席位不参与回避。
    同一候选也不能既是本场辩手又是评委（按 model+owner 同一性判）。"""
    debater_owners = {str(d.get("owner") or "") for d in roster if d.get("owner")}
    debater_ids = {(str(d.get("engine") or ""), str(d.get("model") or ""), str(d.get("owner") or "")) for d in roster}
    out: list[dict] = []
    for j in candidates:
        owner = str(j.get("owner") or "")
        if owner and owner in debater_owners:
            continue
        if (str(j.get("engine") or ""), str(j.get("model") or ""), owner) in debater_ids and owner:
            continue
        out.append(dict(j))
    return out
