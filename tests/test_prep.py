from __future__ import annotations

import asyncio
import json

from arena import prep
from arena import room


def _brief(label: str, role: str = "opening") -> prep.ScoutBrief:
    return prep.ScoutBrief(
        scout=label,
        preferred_role=role,
        main_case=["论点"],
        source_urls=["https://example.com/source"],
    )


def test_team_plan_caps_board_and_rejects_invented_source() -> None:
    raw = json.dumps({
        "opening_label": "GPT",
        "rebuttal_label": "Claude",
        "board": "战" * 900,
        "source_urls": ["https://example.com/source", "https://invented.example/x"],
        "unresolved": ["数字没核到"],
    })
    plan = prep.parse_team_plan(
        raw,
        side="pro",
        member_labels=["GPT", "Claude"],
        briefs=[_brief("GPT"), _brief("Claude", "rebuttal")],
    )
    assert len(plan.board) == prep.BOARD_MAX_CHARS
    assert plan.source_urls == ["https://example.com/source"]
    assert plan.opening_label == "GPT"
    assert plan.rebuttal_label == "Claude"


def test_peer_review_is_a_real_bounded_teammate_turn() -> None:
    prompt = prep.build_peer_review_prompt(
        topic="成败能否论英雄",
        stance="可以",
        opponent_stance="不可以",
        reviewer_label="Claude",
        partner_label="GPT",
        briefs=[_brief("GPT"), _brief("Claude", "rebuttal")],
    )
    assert "你是 Claude，队友是 GPT" in prompt
    review = prep.parse_team_review(json.dumps({
        "strongest_shared": "公共结果要可核对",
        "challenge_to_partner": "别把短期输赢当最终成败",
        "preferred_role": "rebuttal",
        "unresolved": ["失败先驱如何命名"],
    }, ensure_ascii=False), reviewer_label="Claude")
    assert review.reviewer == "Claude"
    assert review.preferred_role == "rebuttal"
    assert review.challenge_to_partner == "别把短期输赢当最终成败"


def test_mini_team_can_choose_roles_without_persona_injection() -> None:
    roster = [
        {"side": "pro", "seat": 1, "name": "旧正一", "label": "GPT"},
        {"side": "pro", "seat": 2, "name": "旧正二", "label": "Claude"},
        {"side": "con", "seat": 1, "name": "旧反一", "label": "Fable"},
        {"side": "con", "seat": 2, "name": "旧反二", "label": "Opus"},
    ]
    plans = {
        "pro": prep.TeamPlan("pro", "Claude", "GPT", "正方板"),
        "con": prep.TeamPlan("con", "Opus", "Fable", "反方板"),
    }
    prep.apply_mini_role_choice(roster, plans)
    by_label = {row["label"]: row for row in roster}
    assert by_label["Claude"]["name"] == "正方一辩"
    assert by_label["GPT"]["name"] == "正方二辩"
    assert by_label["Opus"]["name"] == "反方一辩"
    assert all("mentor" not in row for row in roster)
    assert by_label["Claude"]["strategy_board"] == "正方板"


def test_blind_transcript_hides_models_and_swaps_presentation() -> None:
    transcript = [
        {"speaker": "正方一辩", "side": "pro", "stage": "正方一辩·立论", "text": "第一段正文"},
        {"speaker": "反方一辩", "side": "con", "stage": "反方一辩·立论", "text": "第二段正文"},
    ]
    normal, normal_map = prep.blind_transcript(transcript)
    swapped, swapped_map = prep.blind_transcript(transcript, swap=True)
    assert normal_map == {"pro": "A", "con": "B"}
    assert swapped_map == {"pro": "B", "con": "A"}
    assert "正方" not in normal and "反方" not in normal
    assert "队A · 席P01 · 队A一辩·立论" in normal
    assert "队B · 席P01 · 队B一辩·立论" in swapped


def test_blind_crossfire_uses_stable_speaker_ids_not_later_speech_ids() -> None:
    transcript = [
        {"speaker": "正方一辩", "side": "pro", "stage": "正方一辩·立论", "text": "立论"},
        {"speaker": "反方一辩", "side": "con", "stage": "反方一辩·立论", "text": "立论"},
        {"speaker": "正方一辩", "side": "pro", "stage": "正方一辩·结辩", "text": "结辩"},
    ]
    crossfire = [{"exchanges": [{
        "asker": "正方一辩", "answerer": "反方一辩", "q": "问句", "a": "答句",
    }]}]
    blinded, _ = prep.blind_transcript(transcript, crossfire)
    assert "问（P01）：问句" in blinded
    assert "答（P02）：答句" in blinded
    assert "问（S03）" not in blinded


def test_blind_transcript_keeps_crossfire_in_real_schedule_position() -> None:
    transcript = [
        {"speaker": "正方一辩", "side": "pro", "stage": "正方一辩·立论",
         "schedule_index": 0, "text": "正方立论"},
        {"speaker": "反方一辩", "side": "con", "stage": "反方一辩·立论",
         "schedule_index": 1, "text": "反方立论"},
        {"speaker": "正方二辩", "side": "pro", "stage": "正方二辩·驳论",
         "schedule_index": 4, "text": "正方驳论"},
    ]
    crossfire = [{
        "stage": "交互质询·正方问", "schedule_index": 2,
        "exchanges": [{"asker": "正方一辩", "answerer": "反方一辩", "q": "问题", "a": "回答"}],
    }]
    blinded, _ = prep.blind_transcript(
        transcript,
        crossfire,
        stage_order=tuple(stage for stage, *_rest in room.MINI_FORMAT),
    )
    assert blinded.index("反方立论") < blinded.index("[Q01]") < blinded.index("正方驳论")


def test_ballot_requires_grounded_quotes_and_maps_swapped_winner() -> None:
    transcript = [
        {"side": "pro", "text": "今天真正要比较的是谁能兑现承诺。"},
        {"side": "con", "text": "成功只是结果，不能抹掉过程中的勇气。"},
    ]
    raw = json.dumps({
        "winner": "B",
        "margin": "narrow",
        "reason": "队B接住了题面。",
        "uncertainty": "例证较少。",
        "evidence": [
            {"speech_id": "S01", "quote": "谁能兑现承诺"},
            {"speech_id": "S02", "quote": "不能抹掉过程中的勇气"},
        ],
    }, ensure_ascii=False)
    ballot = prep.parse_ballot(
        raw,
        transcript=transcript,
        side_to_label={"pro": "B", "con": "A"},
        ballot_id="swapped",
    )
    assert ballot["valid"] is True
    assert ballot["winner"] == "pro"

    bad = prep.parse_ballot(
        raw.replace("谁能兑现承诺", "不存在的原话"),
        transcript=transcript,
        side_to_label={"pro": "B", "con": "A"},
        ballot_id="bad",
    )
    assert bad["valid"] is False
    assert bad["error"] == "evidence_not_grounded"


def test_ballot_quote_survives_transcription_punctuation() -> None:
    # 首场冤案：原文「…所有人的损失，而这一环…」被评委引成「…所有人的损失。」
    # ——25 字全对、只差截断处补的句号，三张票全灭出 judge_failed。标点不参与逐字校验。
    transcript = [
        {"side": "pro", "text": "放开猫，损失止于一条命；放开画，是往后所有人的损失，而这一环，偏偏卡在我这只手上。"},
        {"side": "con", "text": "成功只是结果，不能抹掉过程中的勇气。"},
    ]
    raw = json.dumps({
        "winner": "A",
        "margin": "narrow",
        "reason": "决胜点。",
        "uncertainty": "无。",
        "evidence": [
            {"speech_id": "S01", "quote": "放开猫，损失止于一条命；放开画，是往后所有人的损失。"},
            {"speech_id": "S02", "quote": "不能抹掉过程中的勇气！"},
        ],
    }, ensure_ascii=False)
    ballot = prep.parse_ballot(
        raw,
        transcript=transcript,
        side_to_label={"pro": "A", "con": "B"},
        ballot_id="punct",
    )
    assert ballot["valid"] is True
    assert len(ballot["evidence"]) == 2

    # 只剥标点不放水正文：字不一样照样不落地
    fabricated = prep.parse_ballot(
        raw.replace("不能抹掉过程中的勇气", "勇气可以被结果抹掉"),
        transcript=transcript,
        side_to_label={"pro": "A", "con": "B"},
        ballot_id="fabricated",
    )
    assert fabricated["valid"] is False


def test_jury_position_recheck_is_per_judge_and_does_not_vote() -> None:
    """合同：决胜只数原序票；对调票只跟同一位评委自己的原序票比。"""
    def ballot(judge, winner, role, mvp=None):
        pres = {"pro": "A", "con": "B"} if role == "primary" else {"pro": "B", "con": "A"}
        return {"valid": True, "winner": winner, "presentation": pres, "judge": judge, "role": role,
                "mvp": {"pid": "P01", "speaker": mvp} if mvp else None}

    # 三席原序全投正方；甲的对调票翻成反方 → 甲自身不稳，只标注，胜负不变
    result = prep.aggregate_ballots([
        ballot("甲", "pro", "primary"), ballot("乙", "pro", "primary"), ballot("丙", "pro", "primary"),
        ballot("甲", "con", "recheck"), ballot("乙", "pro", "recheck"), ballot("丙", "pro", "recheck"),
    ])
    assert result["status"] == "decided" and result["winner"] == "pro"
    assert result["counts"]["pro"] == 3 and result["primary_ballots"] == 3   # 对调票不计票
    assert result["position_checked_judges"] == ["甲", "乙", "丙"]
    assert result["position_unstable_judges"] == ["甲"] and result["position_unstable"] is True

    # 多数评委对调就翻 → 评委席在投位置，裁决作废
    flipped = prep.aggregate_ballots([
        ballot("甲", "pro", "primary"), ballot("乙", "pro", "primary"), ballot("丙", "pro", "primary"),
        ballot("甲", "con", "recheck"), ballot("乙", "con", "recheck"), ballot("丙", "pro", "recheck"),
    ])
    assert flipped["status"] == "position_unstable" and flipped["winner"] is None

    # 旧式：换位票由另一位评委判、没有 judge 字段 → 配不上对 = 没复判；也不计票
    normal = {"valid": True, "winner": "pro", "presentation": {"pro": "A", "con": "B"}}
    swapped = {"valid": True, "winner": "con", "presentation": {"pro": "B", "con": "A"}}
    third = {"valid": True, "winner": "pro", "presentation": {"pro": "A", "con": "B"}}
    legacy = prep.aggregate_ballots([normal, swapped, third])
    assert legacy["status"] == "decided" and legacy["winner"] == "pro"
    assert legacy["position_checked"] is False and legacy["counts"]["con"] == 0

    failed = prep.aggregate_ballots([normal, {"valid": False}])
    assert failed["status"] == "judge_failed"


def test_jury_mvp_tally_and_tiebreak() -> None:
    def ballot(judge, winner, mvp):
        return {"valid": True, "winner": winner, "presentation": {"pro": "A", "con": "B"}, "judge": judge,
                "role": "primary", "mvp": {"pid": "P0x", "speaker": mvp} if mvp else None}

    # 票同一人
    r = prep.aggregate_ballots([ballot("甲", "pro", "正方二辩"), ballot("乙", "pro", "正方二辩"),
                                       ballot("丙", "con", "反方一辩")])
    assert r["mvp"]["speaker"] == "正方二辩" and r["mvp"]["votes"] == 2 and r["mvp"]["of"] == 3
    # 三人三样：胜方席位优先；胜方里仍平 → 第一席所投
    r = prep.aggregate_ballots([ballot("甲", "pro", "反方一辩"), ballot("乙", "pro", "正方二辩"),
                                       ballot("丙", "pro", "正方三辩")])
    assert r["mvp"]["speaker"] == "正方二辩"
    # 没人填 → 无 MVP；票仍有效
    r = prep.aggregate_ballots([ballot("甲", "pro", None), ballot("乙", "pro", None)])
    assert r["mvp"] is None and r["status"] == "decided"


def test_parse_ballot_maps_mvp_seat_id_back_to_speaker() -> None:
    transcript = [
        {"speaker": "正方一辩", "side": "pro", "text": "今天真正要比较的是谁能兑现承诺。"},
        {"speaker": "反方一辩", "side": "con", "text": "成功只是结果，不能抹掉过程中的勇气。"},
    ]
    blinded, _ = prep.blind_transcript(transcript)
    assert "席P01" in blinded and "席P02" in blinded
    raw = json.dumps({
        "winner": "A", "mvp": "p02", "margin": "narrow", "reason": "r", "uncertainty": "u",
        "evidence": [{"speech_id": "S01", "quote": "谁能兑现承诺"},
                     {"speech_id": "S02", "quote": "不能抹掉过程中的勇气"}],
    }, ensure_ascii=False)
    b = prep.parse_ballot(raw, transcript=transcript, side_to_label={"pro": "A", "con": "B"}, ballot_id="m")
    assert b["valid"] and b["mvp"] == {"pid": "P02", "speaker": "反方一辩"}
    # 填了不存在的席号：票有效、MVP 为 None
    b2 = prep.parse_ballot(raw.replace('"p02"', '"P09"'), transcript=transcript,
                                  side_to_label={"pro": "A", "con": "B"}, ballot_id="m2")
    assert b2["valid"] and b2["mvp"] is None
    assert '"mvp": "P01"' in prep.build_ballot_prompt(topic="t", blinded=blinded)


def test_exact_quote_receipt_flags_only_unseen_opponent_quote() -> None:
    transcript = [{"side": "con", "text": "成功只是结果，不能抹掉过程中的勇气。"}]
    assert prep.verify_opponent_quotes(
        "对方说「不能抹掉过程中的勇气」，我接这句话。",
        side="pro",
        transcript=transcript,
    ) == []
    findings = prep.verify_opponent_quotes(
        "对方说「胜者天然就是英雄」，但他没说过。",
        side="pro",
        transcript=transcript,
    )
    assert findings == [{"quote": "胜者天然就是英雄", "status": "not_exactly_found"}]


def test_quote_receipt_does_not_bridge_short_pairs_and_reads_crossfire() -> None:
    transcript = [
        {"speaker": "正方一辩", "side": "pro", "text": "正方原稿"},
        {"speaker": "反方一辩", "side": "con", "text": "反方原稿"},
    ]
    crossfire = [{"exchanges": [{
        "asker": "反方一辩", "answerer": "正方一辩",
        "q": "救援成功率算不算理由？", "a": "算，但只判断是不是徒劳。",
    }]}]
    findings = prep.verify_opponent_quotes(
        '他说“救。”又问“救援成功率算不算理由？”；我没说“凭价格决定生命”。',
        side="pro",
        transcript=transcript,
        crossfire=crossfire,
    )
    assert findings == [{"quote": "凭价格决定生命", "status": "not_exactly_found"}]


def test_claude_debater_does_not_load_project_hooks(monkeypatch) -> None:
    seen = {}

    class Result:
        stdout = "正文"
        stderr = ""
        returncode = 0

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["input"] = kwargs.get("input")
        return Result()

    monkeypatch.setattr(room.subprocess, "run", fake_run)
    d = {"engine": "claude", "model": "claude-fable-5", "effort": "max"}
    assert room._run_cli(d, "system", "prompt", 10) == "正文"
    cmd = seen["cmd"]
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "project" not in cmd
    assert seen["input"] == "prompt"


def test_cli_error_is_retried_and_never_becomes_speech(monkeypatch, tmp_path) -> None:
    calls = []

    class Result:
        stderr = ""
        returncode = 0

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return Result("API Error: 529 Overloaded" if len(calls) == 1 else "codex\n真正的问题\ntokens used\n1")

    monkeypatch.setattr(room.subprocess, "run", fake_run)
    monkeypatch.setattr(room.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(room.tempfile, "gettempdir", lambda: str(tmp_path))
    d = {"engine": "codex", "model": "gpt-5.5", "effort": "xhigh"}
    assert room._run_cli(d, "system", "prompt", 10) == "真正的问题"
    assert len(calls) == 2
    cmd = calls[0][0]
    assert "--ephemeral" in cmd
    assert "--ignore-user-config" in cmd
    assert calls[0][1]["cwd"].endswith("debate-arena-contestant")


def test_interrupted_match_gets_terminal_checkpoint(tmp_path) -> None:
    out = tmp_path / "match.json"
    room._write_match_state(out, {"status": "running", "transcript": []})
    room._finish_interrupted_record(out, status="failed", error="provider down")
    state = json.loads(out.read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert state["error"] == "provider down"
    assert state["finished_at"]


def test_schedule_resume_skips_every_checkpointed_stage(monkeypatch, tmp_path) -> None:
    transcript = [
        {"speaker": stage, "side": side, "stage": stage, "text": stage,
         "elapsed_sec": 1.0, "truncated": False}
        for stage, side, seat, _seconds in room.MINI_FORMAT if seat != -1
    ]
    crossfire = [
        {"stage": stage, "exchanges": [{"q": "问", "a": "答"}]}
        for stage, _side, seat, _seconds in room.MINI_FORMAT if seat == -1
    ]
    state = {
        "schema_version": 2,
        "run_id": "resume-test",
        "status": "running",
        "topic": "甲/乙",
        "pro_side": "甲",
        "con_side": "乙",
        "format": "mini",
        "lang": "zh",
        "crossfire_rounds": 1,
        "roster": [dict(row) for row in room.ROSTER_MINI],
        "transcript": transcript,
        "crossfire": crossfire,
        "jury": None,
    }
    emitted = []

    async def fake_emit(body, **kwargs):
        emitted.append((body, kwargs))

    async def fake_jury(_topic, got_transcript, got_crossfire, **_kwargs):
        assert got_transcript is transcript
        assert got_crossfire is crossfire
        return {"status": "decided", "winner": "pro", "counts": {"pro": 2, "con": 1},
                "ballots": []}

    monkeypatch.setattr(room, "_emit_to_room", fake_emit)
    monkeypatch.setattr(room, "_run_blind_jury", fake_jury)
    monkeypatch.setattr(
        room, "_run_cli",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("stage replayed")),
    )
    out = tmp_path / "resume.json"
    asyncio.run(room._run_schedule(state, out, timeout=30, emit_opening=False))
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert len(saved["transcript"]) == 6
    assert len(saved["crossfire"]) == 2
    assert saved["status"] == "completed"
    assert any(kwargs.get("title") == "↩️ 比赛续跑" for _body, kwargs in emitted)


def test_full_resume_keeps_both_same_named_free_debate_turns(monkeypatch, tmp_path) -> None:
    missing = {8, 9}
    transcript = []
    crossfire = []
    for index, (stage, side, seat, _seconds) in enumerate(room.FULL_FORMAT):
        if index in missing:
            continue
        if seat == -1:
            crossfire.append({
                "stage": stage, "schedule_index": index,
                "exchanges": [{
                    "asker": "提问席", "answerer": "回答席", "q": "问", "a": "答",
                }],
            })
        else:
            transcript.append({
                "speaker": stage, "side": side, "stage": stage,
                "schedule_index": index, "text": stage,
                "elapsed_sec": 1.0, "truncated": False,
            })
    state = {
        "schema_version": 2, "run_id": "full-resume", "status": "running",
        "topic": "甲/乙", "pro_side": "甲", "con_side": "乙",
        "format": "full", "lang": "zh", "crossfire_rounds": 1,
        "roster": [dict(row) for row in room.ROSTER_FULL],
        "transcript": transcript, "crossfire": crossfire, "jury": None,
    }

    async def fake_emit(*_args, **_kwargs):
        return None

    async def fake_host(*_args, **_kwargs):
        return ""

    async def fake_jury(*_args, **_kwargs):
        return {"status": "decided", "winner": "pro", "counts": {"pro": 2, "con": 1}, "ballots": []}

    monkeypatch.setattr(room, "_emit_to_room", fake_emit)
    monkeypatch.setattr(room, "_host_check", fake_host)
    monkeypatch.setattr(room, "_run_blind_jury", fake_jury)
    monkeypatch.setattr(room, "_run_cli", lambda *_args, **_kwargs: "续跑自由辩")
    out = tmp_path / "full-resume.json"
    asyncio.run(room._run_schedule(state, out, timeout=30, emit_opening=False))
    saved = json.loads(out.read_text(encoding="utf-8"))
    free_indices = sorted(
        row["schedule_index"] for row in saved["transcript"]
        if row["stage"].startswith("自由辩")
    )
    assert free_indices == [6, 7, 8, 9]


def test_resume_reenters_interrupted_prep_before_match(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)
    out = tmp_path / "prep-resume.json"
    state = {
        "schema_version": 2, "run_id": "prep-resume", "status": "failed",
        "phase": "prep", "prep_enabled": True, "prep": {"status": "disabled"},
        "topic": "甲/乙", "pro_side": "甲", "con_side": "乙",
        "format": "mini", "lang": "zh", "crossfire_rounds": 1,
        "roster": [dict(row) for row in room.ROSTER_MINI],
        "transcript": [], "crossfire": [], "jury": None,
    }
    out.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    calls = []

    async def fake_prep(*_args, **_kwargs):
        calls.append("prep")
        return {"status": "complete"}

    async def fake_schedule(got_state, *_args, **_kwargs):
        calls.append("match")
        assert got_state["prep"]["status"] == "complete"
        assert got_state["phase"] == "match"

    monkeypatch.setattr(room, "_run_prep", fake_prep)
    monkeypatch.setattr(room, "_run_schedule", fake_schedule)
    asyncio.run(room.resume_match(out, timeout=30))
    assert calls == ["prep", "match"]


def test_fact_base_rule_reaches_judge_and_debater_prompts() -> None:
    """Elliot 评阅 #2：事实基座 + 举证在引用方，评委票与辩手 system 两边都得看到同一条。"""
    blinded, _ = prep.blind_transcript([{"speaker": "正方一辩", "side": "pro", "text": "x"}])
    without = prep.build_ballot_prompt(topic="t", blinded=blinded)
    assert "本题未设" in without and "默认不予采信" in without and "举证" in prep.FACT_RULE_JUDGE
    with_base = prep.build_ballot_prompt(topic="t", blinded=blinded, fact_base="2025 年全国高校毕业生 1222 万")
    assert "题面事实基座（双方共享的前提事实" in with_base and "1222 万" in with_base

    from arena import room
    seat = {"name": "正方一辩", "side": "pro", "seat": 1, "fact_base": "1222 万毕业生"}
    system = room._build_system(seat, "t", "正", "反", "zh")
    assert "10. 事实举证在引用方" in system and "【题面事实基座·双方共享前提】1222 万毕业生" in system
    bare = room._build_system({"name": "正方一辩", "side": "pro", "seat": 1}, "t", "正", "反", "zh")
    assert "10. 事实举证在引用方" in bare and "题面事实基座·双方共享前提" not in bare
