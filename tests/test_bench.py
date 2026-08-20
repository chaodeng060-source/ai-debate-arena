"""赛制三样：铁律 +3、评委席走 CLI + 明德杯记分、评委插问。

这里只测合同：
prompt 里该有的字、票怎么解析、插问怎么落地、老赛录能不能续跑。不碰真模型。
"""

from __future__ import annotations

import asyncio
import json

from arena import prep
from arena import room


def _seat(name: str, side: str, seat: int) -> dict:
    return {"name": name, "side": side, "seat": seat, "engine": "claude",
            "model": "claude-fable-5", "effort": "max", "label": "Claude Fable 5"}


def test_iron_rules_forbid_goal_shift_compromise_and_exit() -> None:
    d = _seat("正方一辩", "pro", 1)
    system = room._build_system(d, "甲/乙", "甲", "乙", "zh")
    assert "竞技辩论" in system
    assert "不许改变代理目标" in system
    assert "不许提折中裁决" in system
    assert "不许退出立场" in system
    # 退出立场的硬词进了倒戈词表，主持人能当场判
    assert "作为AI" in room.DEFECTION_MARKERS
    assert "各退一步" in room.DEFECTION_MARKERS


def test_ballot_prompt_carries_mingde_scoring_and_no_moral_bias() -> None:
    prompt = prep.build_ballot_prompt(topic="甲/乙", blinded="[S01] 队A · 席P01 · 立论\n甲", bench_qa="")
    assert "不评立场本身的道德倾向" in prompt
    assert "rubric_scores" in prompt and "discretion" in prompt
    assert "\"winner\": \"A|B\"" in prompt          # 决胜票不许弃权
    assert "评委席插问实录" not in prompt
    with_bench = prep.build_ballot_prompt(topic="甲/乙", blinded="x", bench_qa="[J01] 评委问队A：？\n队A答：！")
    assert "评委席插问实录" in with_bench and "[J01]" in with_bench


def test_ballot_parses_scores_and_flags_inconsistent_vote() -> None:
    transcript = [
        {"side": "pro", "text": "今天真正要比较的是谁能兑现承诺。"},
        {"side": "con", "text": "成功只是结果，不能抹掉过程中的勇气。"},
    ]
    raw = json.dumps({
        "rubric_scores": {"A": [8, 7, 6, 9], "B": [5, 5, 5, 5]},
        "discretion": {"A": 20, "B": 25},
        "winner": "B",                      # 分高的是 A，票投 B → 分票不一致但票有效
        "margin": "narrow",
        "reason": "队B接住了题面。",
        "uncertainty": "例证较少。",
        "evidence": [
            {"speech_id": "S01", "quote": "谁能兑现承诺"},
            {"speech_id": "S02", "quote": "不能抹掉过程中的勇气"},
        ],
    }, ensure_ascii=False)
    ballot = prep.parse_ballot(
        raw, transcript=transcript, side_to_label={"pro": "A", "con": "B"}, ballot_id="b1",
    )
    assert ballot["valid"] is True
    assert ballot["winner"] == "con"
    assert ballot["scores"]["pro"]["rubric_total"] == 52.5     # 30/40 × 70
    assert ballot["scores"]["pro"]["total"] == 72.5
    assert ballot["scores"]["con"]["total"] == 60.0
    assert ballot["score_vote_consistent"] is False

    # 换位票：A/B 标签映射回 pro/con，分数也跟着走
    swapped = prep.parse_ballot(
        raw, transcript=transcript, side_to_label={"pro": "B", "con": "A"}, ballot_id="b2",
    )
    assert swapped["winner"] == "pro"
    assert swapped["scores"]["con"]["total"] == 72.5

    # 旧格式（没有分数字段）照样是有效票——决胜票才定胜负
    legacy = json.loads(raw)
    legacy.pop("rubric_scores"); legacy.pop("discretion")
    old = prep.parse_ballot(
        json.dumps(legacy, ensure_ascii=False), transcript=transcript,
        side_to_label={"pro": "A", "con": "B"}, ballot_id="b3",
    )
    assert old["valid"] is True and old["scores"] is None and old["score_vote_consistent"] is None


def test_aggregate_publishes_score_totals_but_votes_decide() -> None:
    def ballot(winner: str, pro_total: float, con_total: float, pro_label: str = "A") -> dict:
        return {"valid": True, "winner": winner,
                "presentation": {"pro": pro_label, "con": "B" if pro_label == "A" else "A"},
                "scores": {"pro": {"total": pro_total}, "con": {"total": con_total}}}
    # 起：分数只加总原序票；对调票（同一评委的 recheck）既不计票也不计分
    result = prep.aggregate_ballots([
        ballot("pro", 80, 70), ballot("pro", 60, 75), ballot("con", 50, 90),
        {**ballot("pro", 99, 1, pro_label="B"), "judge": "甲", "role": "recheck"},
    ])
    assert result["status"] == "decided" and result["winner"] == "pro"
    assert result["score_totals"] == {"pro": 190.0, "con": 235.0}   # 分数反着来也不改判
    assert result["scored_ballots"] == 3


def test_bench_question_maps_presented_label_back_to_side() -> None:
    raw = '{"target": "b", "question": "  你们说的成本\\n到底是谁的成本？"}'
    parsed = prep.parse_bench_question(raw, side_to_label={"pro": "B", "con": "A"})
    assert parsed == {"target": "pro", "question": "你们说的成本 到底是谁的成本？"}
    assert prep.parse_bench_question("嗯", side_to_label={"pro": "A", "con": "B"}) is None
    assert prep.parse_bench_question('{"target":"C","question":"x"}', side_to_label={"pro": "A", "con": "B"}) is None


def test_panel_draw_is_two_fixed_one_rotating_and_shuffled() -> None:
    panel = room._draw_panel(seed=7)
    labels = {j["label"] for j in panel}
    assert len(panel) == 3
    assert {"GPT-5.6-sol", "Claude Opus 5"} <= labels
    assert labels - {"GPT-5.6-sol", "Claude Opus 5"} <= {"GPT-5.5", "Claude Fable 5"}
    assert [j["name"] for j in panel] == list(room.JUDGE_SEAT_NAMES)
    assert all(j["effort"] == "high" for j in panel)


def test_bench_round_asks_each_judge_and_lets_last_speaker_answer(monkeypatch) -> None:
    roster = [_seat("正方一辩", "pro", 1), _seat("正方二辩", "pro", 2),
              _seat("反方一辩", "con", 1), _seat("反方二辩", "con", 2)]
    transcript = [
        {"speaker": "正方一辩", "side": "pro", "stage": "正方一辩·立论", "text": "甲的立论"},
        {"speaker": "反方一辩", "side": "con", "stage": "反方一辩·立论", "text": "乙的立论"},
        {"speaker": "反方二辩", "side": "con", "stage": "反方二辩·驳论", "text": "乙的驳论"},
        {"speaker": "正方二辩", "side": "pro", "stage": "正方二辩·驳论", "text": "甲的驳论"},
    ]
    panel = room._draw_panel(seed=1)
    calls: list[tuple[str, str]] = []
    emitted: list[tuple[str, dict]] = []

    def fake_cli(d, system, prompt, timeout, **_kw):
        calls.append((d.get("name", d.get("label")), prompt))
        if d in panel:
            # 评委：甲问 A（=正方），乙问 B，丙弃问
            if d["name"] == "评委甲":
                return '{"target": "A", "question": "正方的标准到底是什么？"}'
            if d["name"] == "评委乙":
                return '{"target": "B", "question": "反方回避了成本谁承担。"}'
            return "我不想问"
        assert "评委席插问" in system
        return f"{d['name']}答：" + "字" * 200

    async def fake_emit(body, **kwargs):
        emitted.append((body, kwargs))
        return "msg"

    monkeypatch.setattr(room, "_run_cli", fake_cli)
    monkeypatch.setattr(room, "_emit_to_room", fake_emit)
    monkeypatch.setattr(room, "JUDGE_ENGINE", "cli")

    bench = asyncio.run(room._run_bench_questions(
        "甲/乙", "甲", "乙", "zh", roster, transcript, [], panel=panel, timeout=30,
    ))
    assert [(b["judge"], b["target"], b["answerer"]) for b in bench] == [
        ("评委甲", "pro", "正方二辩"),     # 正方最后发言的是二辩
        ("评委乙", "con", "反方二辩"),
    ]
    assert all(len(b["answer"]) <= prep.BENCH_A_CHARS for b in bench)
    titles = [kw.get("title", "") for _b, kw in emitted]
    assert any("评委丙放弃插问" in b for b, _kw in emitted)
    assert sum(1 for t in titles if t.startswith("❓")) == 2
    assert sum(1 for t in titles if t.startswith("💬")) == 2


def test_blind_jury_runs_three_cli_judges_and_reports_panel(monkeypatch) -> None:
    transcript = [
        {"speaker": "正方一辩", "side": "pro", "stage": "正方一辩·立论", "text": "今天真正要比较的是谁能兑现承诺。"},
        {"speaker": "反方一辩", "side": "con", "stage": "反方一辩·立论", "text": "成功只是结果，不能抹掉过程中的勇气。"},
    ]
    panel = room._draw_panel(seed=3)
    seen_prompts: list[str] = []

    def fake_cli(d, system, prompt, timeout, **_kw):
        assert d in panel and system == room.JUDGE_SYSTEM
        seen_prompts.append(prompt)
        # 评委总是投「呈现为 A 的那一队」——换位票就会翻，测的是映射不是判断
        return json.dumps({
            "rubric_scores": {"A": [9, 9, 9, 9], "B": [3, 3, 3, 3]},
            "discretion": {"A": 30, "B": 10},
            "winner": "A", "margin": "clear", "reason": "A 更完整", "uncertainty": "无",
            "evidence": [{"speech_id": "S01", "quote": "谁能兑现承诺"},
                         {"speech_id": "S02", "quote": "不能抹掉过程中的勇气"}],
        }, ensure_ascii=False)

    monkeypatch.setattr(room, "_run_cli", fake_cli)
    monkeypatch.setattr(room, "JUDGE_ENGINE", "cli")
    monkeypatch.setenv("DEBATE_POSITION_RECHECK", "on")   # 默认抽样；这条测的是六票路径，显式开
    bench = [{"judge": "评委甲", "target": "con", "question": "问", "answerer": "反方一辩", "answer": "答"}]
    jury = asyncio.run(room._run_blind_jury(
        "甲/乙", transcript, [], stage_order=("正方一辩·立论", "反方一辩·立论"),
        panel=panel, bench=bench, timeout=30,
    ))
    assert jury["engine"] == "cli"
    assert [p["name"] for p in jury["panel"]] == ["评委甲", "评委乙", "评委丙"]
    # 起每位评委两张票：原序 + A/B 对调（六次调用，并行）
    assert len(seen_prompts) == 6 and all("评委席插问实录" in p for p in seen_prompts)
    # 对调票里插问实录也换了标签：反方在三张原序票里是队B、在三张对调票里是队A
    # （并行出票，seen_prompts 的顺序不定，只数个数）
    assert sum("评委问队B：问" in p for p in seen_prompts) == 3
    assert sum("评委问队A：问" in p for p in seen_prompts) == 3
    # 永远投「呈现为 A」的评委：三位原序都投正方、对调都投反方 → 三位自身都不稳 → 裁决作废
    assert jury["status"] == "position_unstable" and jury["winner"] is None
    assert jury["position_unstable_judges"] == ["评委甲", "评委乙", "评委丙"]
    assert [b["judge"] for b in jury["ballots"]] == ["评委甲", "评委乙", "评委丙"] * 2
    assert [b["role"] for b in jury["ballots"]] == ["primary"] * 3 + ["recheck"] * 3
    assert jury["ballots"][0]["winner"] == "pro" and jury["ballots"][3]["winner"] == "con"
    assert jury["counts"] == {"pro": 3, "con": 0, "tie": 0, "uncertain": 0}   # 对调票不计票
    assert jury["position_recheck_enabled"] is True


def test_judge_cli_failure_invalidates_that_ballot_only(monkeypatch) -> None:
    transcript = [
        {"speaker": "正方一辩", "side": "pro", "stage": "正方一辩·立论", "text": "今天真正要比较的是谁能兑现承诺。"},
        {"speaker": "反方一辩", "side": "con", "stage": "反方一辩·立论", "text": "成功只是结果，不能抹掉过程中的勇气。"},
    ]
    panel = room._draw_panel(seed=5)
    good = json.dumps({
        "winner": "A", "margin": "clear", "reason": "A", "uncertainty": "无",
        "evidence": [{"speech_id": "S01", "quote": "谁能兑现承诺"},
                     {"speech_id": "S02", "quote": "不能抹掉过程中的勇气"}],
    }, ensure_ascii=False)

    def fake_cli(d, system, prompt, timeout, **_kw):
        if d["name"] == "评委丙":
            raise RuntimeError("contestant CLI failed after retry: 529")
        return good

    monkeypatch.setattr(room, "_run_cli", fake_cli)
    monkeypatch.setattr(room, "JUDGE_ENGINE", "cli")
    monkeypatch.setenv("DEBATE_POSITION_RECHECK", "on")   # 同上：显式开六票路径
    jury = asyncio.run(room._run_blind_jury("甲/乙", transcript, [], panel=panel, timeout=30))
    bad = [b for b in jury["ballots"] if not b["valid"]]
    # 评委丙原序+对调两张都失败；甲乙四张有效，其中两张原序票定胜负
    assert len(bad) == 2 and {b["judge"] for b in bad} == {"评委丙"}
    assert all(b["error"].startswith("cli_failed") for b in bad)
    assert jury["valid_ballots"] == 4 and jury["primary_ballots"] == 2


def test_schedule_tail_runs_bench_before_jury_and_respects_switch(monkeypatch, tmp_path) -> None:
    transcript = [
        {"speaker": stage, "side": side, "stage": stage, "text": stage,
         "elapsed_sec": 1.0, "truncated": False, "schedule_index": i}
        for i, (stage, side, seat, _seconds) in enumerate(room.MINI_FORMAT) if seat != -1
    ]
    crossfire = [
        {"stage": stage, "exchanges": [{"q": "问", "a": "答"}], "schedule_index": i}
        for i, (stage, _side, seat, _seconds) in enumerate(room.MINI_FORMAT) if seat == -1
    ]

    def make_state(**extra) -> dict:
        return {
            "schema_version": 2, "run_id": "bench-test", "status": "running",
            "topic": "甲/乙", "pro_side": "甲", "con_side": "乙", "format": "mini", "lang": "zh",
            "crossfire_rounds": 1, "roster": [dict(row) for row in room.ROSTER_MINI],
            "transcript": [dict(r) for r in transcript], "crossfire": [dict(r) for r in crossfire],
            "jury": None, **extra,
        }

    order: list[str] = []

    async def fake_emit(body, **kwargs):
        return "msg"

    async def fake_bench(*_args, **kwargs):
        order.append("bench")
        assert kwargs["panel"]
        return [{"judge": "评委甲", "target": "pro", "question": "?", "answerer": "正方一辩", "answer": "!"}]

    jury_bench_seen: list[list] = []

    async def fake_jury(_topic, _transcript, _crossfire, **kwargs):
        order.append("jury")
        assert kwargs["panel"]
        jury_bench_seen.append(list(kwargs["bench"]))
        return {"status": "decided", "winner": "pro", "counts": {"pro": 2, "con": 1}, "ballots": []}

    monkeypatch.setattr(room, "_emit_to_room", fake_emit)
    monkeypatch.setattr(room, "_run_bench_questions", fake_bench)
    monkeypatch.setattr(room, "_run_blind_jury", fake_jury)
    monkeypatch.setattr(room, "_run_cli", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("replayed")))

    # 老赛录没有 bench_enabled 字段 → 默认开：先插问后评审，panel 落盘
    out = tmp_path / "a.json"
    asyncio.run(room._run_schedule(make_state(), out, timeout=30, emit_opening=False))
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert order == ["bench", "jury"]
    assert saved["panel"] and len(saved["panel"]) == 3 and saved["bench"][0]["judge"] == "评委甲"
    assert jury_bench_seen[-1][0]["judge"] == "评委甲"          # 插问实录进了评审材料
    assert saved["status"] == "completed" and saved["phase"] == "done"

    # 关掉插问：直接评审，评审材料里没有插问
    order.clear()
    out2 = tmp_path / "b.json"
    asyncio.run(room._run_schedule(make_state(bench_enabled=False), out2, timeout=30, emit_opening=False))
    assert order == ["jury"] and jury_bench_seen[-1] == []

    # 插问已经落盘的续跑：不重问
    order.clear()
    out3 = tmp_path / "c.json"
    asyncio.run(room._run_schedule(
        make_state(bench=[{"judge": "评委乙", "target": "con", "question": "q", "answerer": "反方一辩", "answer": "a"}],
                   panel=room._draw_panel(seed=2)),
        out3, timeout=30, emit_opening=False,
    ))
    assert order == ["jury"] and jury_bench_seen[-1][0]["judge"] == "评委乙"


def test_ballot_accepts_quotes_from_bench_answers_and_relabels() -> None:
    # 首场真实病例：GPT-5.5 引了插问答复「目标没换、努力没停」却标成 S06 → 整票冤死
    transcript = [
        {"side": "pro", "text": "今天真正要比较的是谁能兑现承诺。"},
        {"side": "con", "text": "成功只是结果，不能抹掉过程中的勇气。"},
    ]
    bench = [{"judge": "评委乙", "target": "pro", "question": "找回绝无可能时……",
              "answerer": "正方一辩", "answer": "目标决定她找谁，身份只回答她为何不停。目标没换、努力没停，这正是辩题里的人。"}]
    raw = json.dumps({
        "winner": "A", "margin": "narrow", "reason": "r", "uncertainty": "u",
        "evidence": [{"speech_id": "S01", "quote": "谁能兑现承诺"},
                     {"speech_id": "S06", "quote": "目标没换、努力没停"}],
    }, ensure_ascii=False)
    without = prep.parse_ballot(raw, transcript=transcript, side_to_label={"pro": "A", "con": "B"}, ballot_id="x")
    assert without["valid"] is False and without["error"] == "evidence_not_grounded"
    with_bench = prep.parse_ballot(raw, transcript=transcript, side_to_label={"pro": "A", "con": "B"},
                                          ballot_id="y", bench=bench)
    assert with_bench["valid"] is True
    assert with_bench["evidence"][1] == {"speech_id": "J01", "quote": "目标没换、努力没停", "relabelled_from": "S06"}
    # 直接标 J01 也行
    raw2 = raw.replace('"S06"', '"J01"')
    assert prep.parse_ballot(raw2, transcript=transcript, side_to_label={"pro": "A", "con": "B"},
                                    ballot_id="z", bench=bench)["valid"] is True


def test_aggregate_flags_unchecked_position_when_swapped_ballot_invalid() -> None:
    normal = {"valid": True, "winner": "pro", "presentation": {"pro": "A", "con": "B"}}
    third = {"valid": True, "winner": "pro", "presentation": {"pro": "A", "con": "B"}}
    result = prep.aggregate_ballots([normal, {"valid": False, "presentation": {"pro": "B", "con": "A"}}, third])
    assert result["status"] == "decided" and result["position_checked"] is False
    md = room._jury_markdown(result)
    assert "位置复判没做成" in md
    # 配上对（同一评委的原序+对调）才算做了复判
    normal_j = {**normal, "judge": "甲", "role": "primary"}
    recheck_j = {"valid": True, "winner": "pro", "presentation": {"pro": "B", "con": "A"}, "judge": "甲", "role": "recheck"}
    ok = prep.aggregate_ballots([normal_j, recheck_j, third])
    assert ok["position_checked"] is True and "位置复判没做成" not in room._jury_markdown(ok)
    assert "1 位评委 A/B 对调后判决不变" in room._jury_markdown(ok)
