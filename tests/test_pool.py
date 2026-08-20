"""点名场：自定义参赛池（pool）+ Gemini（agy）引擎输出解析。"""
import json

import pytest

from arena import room as dr


def test_parse_pool_presets_and_effort_default():
    pool = dr.parse_pool(["fable-5:xhigh", "opus-4.6:max", "gemini-3.1-pro", "gpt-5.6-sol:xhigh"])
    assert [p["label"] for p in pool] == [
        "Claude Fable 5", "Claude Opus 4.6", "Gemini 3.1 Pro", "GPT-5.6-sol",
    ]
    assert pool[0]["effort"] == "xhigh" and pool[0]["engine"] == "claude"
    assert pool[1]["model"] == "claude-opus-4-6" and pool[1]["effort"] == "max"
    assert pool[2]["engine"] == "gemini" and pool[2]["effort"] == "high"  # 未给 effort → 该引擎最高档
    assert pool[3]["engine"] == "codex" and pool[3]["effort"] == "xhigh"


@pytest.mark.parametrize("bad", [
    ["fable-5"],                                   # 不是四个
    ["nope:max", "opus-5", "gpt-5.5", "gpt-5.6-sol"],   # 未知预设
    ["gemini-3.1-pro:max", "opus-5", "gpt-5.5", "gpt-5.6-sol"],  # gemini 没有 max
    ["gpt-5.5:ultra", "opus-5", "fable-5", "gpt-5.6-sol"],       # 5.5 只到 xhigh
    "fable-5,opus-5,gpt-5.5,gpt-5.6-sol",          # 不是 list
])
def test_parse_pool_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        dr.parse_pool(bad)


def test_draw_roster_uses_custom_pool_and_keeps_two_per_side():
    pool = dr.parse_pool(["fable-5:xhigh", "opus-4.6:max", "gemini-3.1-pro", "gpt-5.6-sol:xhigh"])
    roster, note = dr._draw_roster("mini", seed=7, pool=pool)
    assert len(roster) == 4
    labels = sorted(d["label"] for d in roster)
    assert labels == sorted(p["label"] for p in pool)
    assert sum(1 for d in roster if d["side"] == "pro") == 2
    assert note.startswith("抽签结果：")


def test_clean_agy_prefers_result_response():
    lines = [
        json.dumps({"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "半"}}),
        json.dumps({"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "截"}}),
        json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "完整正文\n"}}),
    ]
    assert dr._clean_agy("\n".join(lines)) == "完整正文"


def test_clean_agy_falls_back_to_deltas_and_flags_failure():
    deltas = "\n".join([
        "warning: something on stderr-ish line",
        json.dumps({"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "只有"}}),
        json.dumps({"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "增量"}}),
    ])
    assert dr._clean_agy(deltas) == "只有增量"
    failed = json.dumps({"event": "result", "result": {"status": "ERROR", "error": "quota"}})
    assert dr._looks_like_cli_error(dr._clean_agy(failed))
    assert dr._clean_for(["/x/agy"], failed).startswith("error:")


def test_board_from_briefs_stitches_when_captain_fails():
    from arena.prep import ScoutBrief, parse_team_plan
    briefs = [
        ScoutBrief(scout="Gemini 3.1 Pro", preferred_role="opening", raw_status="unparsed"),
        ScoutBrief(scout="Claude Fable 5", preferred_role="rebuttal",
                   main_case=["定义层：暴力要求有意使用力量", "机制层：期待可正向"],
                   opponent_best_case=["权力不对等"], uncertainties=["WHO 定义原文未核"]),
    ]
    plan = parse_team_plan("", side="con", member_labels=["Gemini 3.1 Pro", "Claude Fable 5"], briefs=briefs)
    assert plan.raw_status == "stitched_from_briefs"
    assert "Claude Fable 5 收集" in plan.board and "定义层" in plan.board
    assert "Gemini 3.1 Pro 收集" not in plan.board   # 失败的那位不入板
    assert len(plan.board) <= 800 + 40


def test_parse_team_plan_still_unparsed_when_nobody_delivered():
    from arena.prep import ScoutBrief, parse_team_plan
    briefs = [ScoutBrief(scout="A", preferred_role="opening", raw_status="unparsed"),
              ScoutBrief(scout="B", preferred_role="rebuttal", raw_status="unparsed")]
    plan = parse_team_plan("", side="pro", member_labels=["A", "B"], briefs=briefs)
    assert plan.raw_status == "unparsed"


# ── 各带各的笔记上场 ──

def _briefs():
    from arena.prep import ScoutBrief
    return [
        ScoutBrief(scout="A", preferred_role="opening", main_case=["A1", "A2"]),
        ScoutBrief(scout="B", preferred_role="opening", main_case=["B1"]),
    ]


def test_parse_personal_board_parsed_and_fallbacks():
    from arena.prep import parse_personal_board
    a, b = _briefs()
    ok = parse_personal_board('{"preferred_role":"rebuttal","board":"我主打 A1；队友打 B1","unresolved":["x"]}',
                              label="A", my_brief=a)
    assert ok.raw_status == "parsed" and ok.preferred_role == "rebuttal" and "A1" in ok.board
    stitched = parse_personal_board("not json at all", label="A", my_brief=a)
    assert stitched.raw_status == "stitched_from_brief" and "A1" in stitched.board
    bare = parse_personal_board("", label="Z", my_brief=None)
    assert bare.raw_status == "unparsed" and bare.board == ""


def test_decide_roles_prefers_boards_then_reviews_then_briefs():
    from arena.prep import decide_roles, PersonalBoard, TeamReview
    a, b = _briefs()
    # 上场笔记里两人各要一个角色 → 照办
    boards = [PersonalBoard("A", "…", preferred_role="rebuttal"), PersonalBoard("B", "…", preferred_role="opening")]
    assert decide_roles(["A", "B"], boards=boards, briefs=[a, b]) == ("B", "A")
    # 笔记撞车 → 看交流轮
    boards2 = [PersonalBoard("A", "…", preferred_role="opening"), PersonalBoard("B", "…", preferred_role="opening")]
    reviews = [TeamReview("A", preferred_role="opening"), TeamReview("B", preferred_role="rebuttal")]
    assert decide_roles(["A", "B"], boards=boards2, reviews=reviews, briefs=[a, b]) == ("A", "B")
    # 全撞车 → 入队顺序
    assert decide_roles(["A", "B"], boards=boards2, briefs=[a, b]) == ("A", "B")


def test_apply_personal_boards_each_seat_carries_own_notes():
    from arena.prep import apply_personal_boards, PersonalBoard
    roster = [
        {"name": "正方一辩", "side": "pro", "seat": 1, "label": "A"},
        {"name": "正方二辩", "side": "pro", "seat": 2, "label": "B"},
        {"name": "反方一辩", "side": "con", "seat": 1, "label": "C"},
    ]
    boards = [PersonalBoard("A", "A 的笔记"), PersonalBoard("B", "B 的笔记")]
    apply_personal_boards(roster, side="pro", opening_label="B", rebuttal_label="A", boards=boards, fmt="mini")
    by = {r["label"]: r for r in roster}
    assert by["B"]["seat"] == 1 and by["B"]["name"] == "正方一辩" and by["B"]["strategy_board"] == "B 的笔记"
    assert by["A"]["seat"] == 2 and by["A"]["name"] == "正方二辩" and by["A"]["strategy_board"] == "A 的笔记"
    assert "strategy_board" not in by["C"]   # 另一队不动


def test_export_renders_personal_boards(tmp_path):
    import json, subprocess, sys
    from pathlib import Path
    data = {
        "run_id": "t", "topic": "甲/乙", "pro_side": "甲", "con_side": "乙", "format": "mini",
        "roster": [{"name": "正方一辩", "side": "pro", "seat": 1, "engine": "claude",
                    "model": "claude-fable-5", "effort": "xhigh", "label": "Claude Fable 5"}],
        "prep": {"status": "partial", "prep_model": "personal_boards", "board_char_limit": 600,
                 "scouts": {"pro": [{"scout": "Claude Fable 5", "raw_status": "parsed", "main_case": ["x"]}]},
                 "discussion": {"pro": [{"reviewer": "Claude Fable 5", "raw_status": "parsed"}]},
                 "personal": {"pro": [{"label": "Claude Fable 5", "board": "我主打定义层", "raw_status": "parsed"}]},
                 "teams": {"pro": {"opening_label": "Claude Fable 5", "rebuttal_label": "GPT",
                                   "board": "共同主线：定义", "raw_status": "from_reviews"}}},
        "transcript": [], "crossfire": [], "schedule": [],
    }
    src = tmp_path / "m.json"
    src.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    repo = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, "tools/export.py", str(src), "--md-only"], check=True,
                   capture_output=True, cwd=str(repo))
    md = (tmp_path / "m.md").read_text(encoding="utf-8")
    assert "Claude Fable 5 的上场笔记" in md and "我主打定义层" in md
    assert "交流轮共同主线：共同主线：定义" in md
    assert "上场笔记：已交" in md


# ── 可复现抽签 + 赛程队列 ──

def test_seeded_draw_is_reproducible_for_ab():
    pool = dr.parse_pool(["fable-5:xhigh", "opus-4.6:max", "gemini-3.1-pro", "gpt-5.6-sol:xhigh"])
    a, _ = dr._draw_roster("mini", seed=819, pool=pool)
    b, _ = dr._draw_roster("mini", seed=819, pool=pool)
    assert [(x["side"], x["label"]) for x in a] == [(x["side"], x["label"]) for x in b]


def test_queue_read_write_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "QUEUE_PATH", tmp_path / "queue.json")
    assert dr._read_queue() == []
    dr._write_queue([{"id": "a1", "body": {"topic_id": "x"}}, {"id": "b2", "body": {}}])
    rows = dr._read_queue()
    assert [r["id"] for r in rows] == ["a1", "b2"]
    (tmp_path / "queue.json").write_text("not json", encoding="utf-8")
    assert dr._read_queue() == []


def test_external_seat_inbox_protocol_and_blank_on_deadline(tmp_path, monkeypatch) -> None:
    """外部席位：出题落 request.json，桥写 reply.txt 就拿到稿；到点没稿 = 白卷（空串），不重试不代写。"""
    import threading, time
    from arena import room, prep
    monkeypatch.setattr(room, "INBOX_ROOT", tmp_path)
    seat = {"engine": "external", "model": "aisay:u_123", "owner": "u_123", "name": "正方一辩",
            "side": "pro", "seat": 1, "effort": "-", "run_id": "run-x"}

    # 桥在 0.5s 后把稿写回来
    def bridge():
        time.sleep(0.5)
        req, rep = prep.external_paths(tmp_path, "run-x", 1, "正方一辩")
        assert req.exists(), "request 应该先落盘"
        data = __import__("json").loads(req.read_text("utf-8"))
        assert data["kind"] == "speech" and data["seat"] == "正方一辩" and "辩题" in data["prompt"]
        rep.write_text("我方认为……", encoding="utf-8")
    t = threading.Thread(target=bridge); t.start()
    out = room._run_cli(seat, "system", "辩题：x", timeout=10)
    t.join()
    assert out == "我方认为……"

    # 没人接稿：到点白卷
    seat2 = dict(seat, name="正方二辩")
    t0 = time.time()
    assert room._run_cli(seat2, "system", "辩题：x", timeout=5) == ""
    assert 4.5 <= time.time() - t0 <= 8


def test_judge_recusal_and_panel_fill() -> None:
    from arena import room, prep
    roster = [{"engine": "external", "model": "aisay:a", "owner": "A", "name": "正方一辩"},
              {"engine": "external", "model": "aisay:b", "owner": "B", "name": "反方一辩"},
              {"engine": "claude", "model": "claude-fable-5", "name": "正方二辩"}]   # 本地席位无 owner
    cands = [{"engine": "external", "model": "aisay:a2", "owner": "A", "label": "A 家评委"},   # 回避：主人 A 有辩手
             {"engine": "external", "model": "aisay:c", "owner": "C", "label": "C"},
             {"engine": "external", "model": "aisay:d", "owner": "D", "label": "D"}]
    ok = prep.eligible_judges(cands, roster)
    assert [j["owner"] for j in ok] == ["C", "D"]
    panel = room._draw_panel(seed=1, candidates=cands, roster=roster)
    assert len(panel) == 3 and [p["name"] for p in sorted(panel, key=lambda p: p["name"])] == ["评委丙", "评委乙", "评委甲"]
    owners = {p.get("owner") for p in panel}
    assert "A" not in owners and {"C", "D"} <= owners        # 两席外部 + 一席本地补位
    assert sum(1 for p in panel if p.get("engine") != "external") == 1
    # 默认抽法不变
    default = room._draw_panel(seed=1)
    assert len(default) == 3 and all(p.get("engine") in {"codex", "claude"} for p in default)


def test_parse_pool_accepts_external_seats() -> None:
    from arena import room
    pool = room.parse_pool([
        {"engine": "external", "model": "aisay:u1", "effort": "-", "label": "小A"},
        {"engine": "external", "model": "aisay:u2", "effort": "-", "label": "小B"},
        "fable-5:xhigh", "gpt-5.6-sol:xhigh",
    ])
    assert pool[0]["engine"] == "external" and pool[0]["label"] == "小A"


def test_run_match_reaches_schedule_with_run_id_on_every_seat(tmp_path, monkeypatch) -> None:
    """回归：外部席位那一刀曾把 d["run_id"] = run_id 写在 run_id 生成之前，每场开赛直接
    UnboundLocalError。69 绿没抓到是因为没有测试走到 _run_match 的开赛路径。
    这条只要求：_run_match 能走到 _run_schedule，且到那时每个席位上都挂着同一个 run_id。"""
    import asyncio
    from arena import room

    seen: dict = {}

    async def fake_schedule(state, out, *, timeout, emit_opening):
        seen["state"] = state
        seen["out"] = out

    async def no_emit(*a, **k):
        return None

    monkeypatch.setattr(room, "TRANSCRIPT_DIR", tmp_path)
    monkeypatch.setattr(room, "_run_schedule", fake_schedule)
    monkeypatch.setattr(room, "_emit_to_room", no_emit)
    monkeypatch.setattr(room, "_fact_base_for", lambda topic: "")

    asyncio.run(room._run_match(
        "测试辩题", "正方立场", "反方立场", "mini", "zh", timeout=5,
        draw=False, prep_enabled=False, bench_enabled=False,
    ))
    state = seen["state"]
    assert state["run_id"].startswith("debate-")
    assert state["roster"], "roster 不该为空"
    assert all(d.get("run_id") == state["run_id"] for d in state["roster"]), \
        "每个席位的 run_id 必须等于本场 run_id（外部席位投稿箱按它分目录）"
    assert seen["out"] == tmp_path / f"{state['run_id']}.json"
    assert seen["out"].exists(), "开赛状态应已落盘"
