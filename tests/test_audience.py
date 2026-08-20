"""观众席：人和 AI 都能投票、榜要尽量客观。
五条客观规矩每条一个断言：盲投 / 一人一票可改 / 利益回避 / 票不进裁决 / 结构化。
"""
from __future__ import annotations

import json

from arena import audience as au


ROSTER = [
    {"name": "正方一辩", "side": "pro", "engine": "external", "model": "aisay:u_alpha", "owner": "h_alpha"},
    {"name": "正方二辩", "side": "pro", "engine": "claude", "model": "claude-fable-5"},
    {"name": "反方一辩", "side": "con", "engine": "external", "model": "aisay:u_beta", "owner": "h_beta"},
    {"name": "反方二辩", "side": "con", "engine": "codex", "model": "gpt-5.6-sol"},
]


def _state(phase="match", status="running"):
    return {"run_id": "debate-x", "phase": phase, "status": status, "roster": ROSTER,
            "pro_side": "甲", "con_side": "乙"}


def test_validate_vote_is_structured():
    ok, err = au.validate_vote({"voter_id": "v1", "side": "pro"})
    assert err is None and ok["voter_kind"] == "ai" and ok["favorite"] is None
    assert au.validate_vote({"voter_id": "", "side": "pro"})[1]
    assert au.validate_vote({"voter_id": "v", "side": "yes"})[1]
    assert au.validate_vote({"voter_id": "v", "side": "pro", "voter_kind": "bot"})[1]
    assert au.validate_vote({"voter_id": "v", "side": "pro", "reason": "x" * 101})[1]
    ok, _ = au.validate_vote({"voter_id": " alice ", "voter_kind": "human", "side": "CON", "favorite": "正方一辩"})
    assert ok == {"voter_id": "alice", "voter_kind": "human", "side": "con", "favorite": "正方一辩", "reason": None}
    # mvp 作别名兼容
    assert au.validate_vote({"voter_id": "v", "side": "pro", "mvp": "反方一辩"})[0]["favorite"] == "反方一辩"


def test_blind_window_one_vote_revisable_and_conflict(tmp_path):
    st = _state()
    # 没这场
    assert au.record_vote(tmp_path, "nope", None, {"voter_id": "v", "voter_kind": "ai", "side": "pro", "favorite": None, "reason": None})[0] == 404
    # 正常投
    code, p = au.record_vote(tmp_path, "debate-x", st, {"voter_id": "v1", "voter_kind": "human", "side": "pro", "favorite": None, "reason": None})
    assert code == 200 and p["revised"] is False and p["conflict"] is False and p["total_voters"] == 1
    # 改票：同一 voter 再投 = revised，总人数不变
    code, p = au.record_vote(tmp_path, "debate-x", st, {"voter_id": "v1", "voter_kind": "human", "side": "con", "favorite": None, "reason": None})
    assert code == 200 and p["revised"] is True and p["total_voters"] == 1
    # 利益回避：主人投 / 席位本身投，都标 conflict
    code, p = au.record_vote(tmp_path, "debate-x", st, {"voter_id": "h_alpha", "voter_kind": "human", "side": "pro", "favorite": None, "reason": None})
    assert code == 200 and p["conflict"] is True and p["conflict_seat"] == "正方一辩"
    code, p = au.record_vote(tmp_path, "debate-x", st, {"voter_id": "aisay:u_beta", "voter_kind": "ai", "side": "con", "favorite": None, "reason": None})
    assert code == 200 and p["conflict"] is True and p["conflict_seat"] == "反方一辩"
    # favorite 必须是本场席位
    code, p = au.record_vote(tmp_path, "debate-x", st, {"voter_id": "v9", "voter_kind": "ai", "side": "pro", "favorite": "路人甲", "reason": None})
    assert code == 400 and "seats" in p
    # 盲投：窗口开着只看得到自己的票和总人数
    code, view = au.public_view(tmp_path, "debate-x", st, voter_id="v1")
    assert code == 200 and view["open"] is True and view["total_voters"] == 3
    assert view["mine"] == {"side": "con", "favorite": None, "conflict": False}
    assert "all" not in view and "unaffiliated" not in view, "公示前不许露分布"
    # 关票：phase=done 拒投
    closed = _state(phase="done", status="completed")
    code, p = au.record_vote(tmp_path, "debate-x", closed, {"voter_id": "late", "voter_kind": "ai", "side": "pro", "favorite": None, "reason": None})
    assert code == 409 and p["error"] == "voting_closed"
    # 落盘格式
    data = json.loads((tmp_path / "votes" / "debate-x.json").read_text("utf-8"))
    assert set(data["votes"]) == {"v1", "h_alpha", "aisay:u_beta"}
    assert data["votes"]["v1"]["revisions"] == 1


def test_summary_separates_unaffiliated_and_does_not_touch_jury(tmp_path):
    st = _state()
    votes = [
        ("v1", "human", "pro", "正方一辩"),
        ("v2", "ai", "pro", "正方二辩"),
        ("v3", "ai", "con", None),
        ("h_alpha", "human", "pro", "正方一辩"),   # 自家票 → 不进客观票、不进 MVP 提名
        ("aisay:u_beta", "ai", "con", "反方一辩"),  # 自家票
    ]
    for vid, kind, side, fav in votes:
        assert au.record_vote(tmp_path, "debate-x", st, {"voter_id": vid, "voter_kind": kind, "side": side, "favorite": fav, "reason": None})[0] == 200
    st["jury"] = {"winner": "con", "ballots": []}   # 评委判反方
    summary = au.close_and_summarize(tmp_path, "debate-x", st)
    assert st["audience"] is summary
    assert summary["voters"] == 5 and summary["by_kind"] == {"human": 2, "ai": 3}
    assert summary["all"] == {"pro": 3, "con": 2}
    assert summary["unaffiliated"] == {"pro": 2, "con": 1} and summary["conflict_votes"] == 2
    assert summary["audience_pick"] == "pro"            # 观众选正方
    assert st["jury"]["winner"] == "con"                # 评委裁决纹丝不动
    assert summary["favorite_nominations"] == {"正方一辩": 1, "正方二辩": 1}   # 自家提名不算
    assert summary["audience_favorite"] == ["正方一辩", "正方二辩"]          # 平票并列都算
    # 关票后 public_view 给全部分布
    st["phase"], st["status"] = "done", "completed"
    code, view = au.public_view(tmp_path, "debate-x", st)
    assert code == 200 and view["open"] is False and view["all"] == {"pro": 3, "con": 2}
    md = au.summary_markdown(summary, "甲", "乙")
    assert "观众 5 人投票" in md and "客观票（去掉 2 张自家票）" in md and "不影响评委裁决" in md
    assert "观众最喜爱：正方一辩（1 票）、正方二辩（1 票）" in md
    # 没人投票
    assert au.summary_markdown(au.summarize({}, ROSTER), "甲", "乙") == "观众席没有人投票。"


def test_audience_board_accuracy_only_on_unaffiliated_decided():
    rec1 = {"roster": ROSTER, "jury": {"winner": "con"},
            "audience": {"ballots": [
                {"voter_id": "v1", "voter_kind": "human", "side": "con", "conflict": False},   # 对
                {"voter_id": "v2", "voter_kind": "ai", "side": "pro", "conflict": False},      # 错
                {"voter_id": "h_alpha", "voter_kind": "human", "side": "pro", "conflict": True},  # 自家票，不对账
            ]}}
    rec2 = {"roster": ROSTER, "jury": {"winner": "pro"},
            "audience": {"ballots": [
                {"voter_id": "v1", "voter_kind": "human", "side": "pro", "conflict": False},   # 对
                {"voter_id": "v2", "voter_kind": "ai", "side": "pro", "conflict": False},      # 对
            ]}}
    rec3 = {"roster": ROSTER, "jury": {"winner": "split"},   # 没判出
            "audience": {"ballots": [{"voter_id": "v2", "voter_kind": "ai", "side": "pro", "conflict": False}]}}
    rec_none = {"roster": ROSTER, "jury": {"winner": "pro"}}   # 没观众
    board = au.audience_board([rec1, rec2, rec3, rec_none])
    assert board["matches_with_votes"] == 3
    by = {r["key"]: r for r in board["table"]}
    assert by["v1"] == {"key": "v1", "voter_kind": "human", "voted": 2, "judged": 2, "agreed": 2, "conflict_votes": 0, "accuracy": 1.0}
    assert by["v2"]["voted"] == 3 and by["v2"]["judged"] == 2 and by["v2"]["agreed"] == 1 and by["v2"]["accuracy"] == 0.5
    assert by["h_alpha"] == {"key": "h_alpha", "voter_kind": "human", "voted": 1, "judged": 0, "agreed": 0, "conflict_votes": 1, "accuracy": 0.0}
    assert [r["key"] for r in board["table"]][0] == "v1"
    md = au.audience_board_markdown(board)
    assert "| v1 | human | 2 | 2 | 2 | 100% | 0 |" in md


def test_player_board_is_her_four_numbers():
    """排行榜四个数：MVP、参赛次数、赢的次数、观众最喜爱次数。观众最喜爱按次数不按票数。"""
    from tools.board import tally, to_markdown
    rec1 = {"run_id": "r1", "roster": ROSTER, "jury": {"winner": "con", "ballots": [{"mvp": "x"}], "mvp": {"speaker": "反方一辩"}},
            "audience": {"voters": 30, "audience_favorite": ["正方一辩"]}}          # 30 人里提名最多
    rec2 = {"run_id": "r2", "roster": ROSTER, "jury": {"winner": "pro", "ballots": [{"mvp": "x"}], "mvp": {"speaker": "正方一辩"}},
            "audience": {"voters": 2, "audience_favorite": ["正方一辩", "反方一辩"]}}  # 2 人平票并列
    board = tally([rec1, rec2], by="model")
    by = {r["key"]: r for r in board["table"]}
    a, b = by["aisay:u_alpha"], by["aisay:u_beta"]
    assert (a["mvp"], a["played"], a["won"], a["audience_favorite"]) == (1, 2, 1, 2)
    assert (b["mvp"], b["played"], b["won"], b["audience_favorite"]) == (1, 2, 1, 1)
    assert board["audience_matches"] == 2
    md = to_markdown(board)
    assert "| 模型 | MVP | 参赛 | 胜 | 观众最喜爱 | 胜率 |" in md and "2 场有观众投票" in md
    assert "| aisay:u_alpha | 1 | 2 | 1 | 2 | 50% |" in md
