"""B10 回归：备赛产物解析对「合法 JSON 但内容全空」要老实——不能标成 parsed。

早期 stub 的 prep 分支一律回 "{}"，parse_scout_brief / parse_team_review 拿到这种
空对象照样把 raw_status 记成默认值 "parsed"：备赛收据显示「已完成」，笔记却是空的，
比真正的解析失败更隐蔽（下游 arena/room.py 的 all_outputs_parsed 全靠这个字段判断
prep.status 是 complete 还是 partial）。这里只测 arena/prep.py 的纯函数，不碰网络/模型。
"""
from __future__ import annotations

from arena import prep


def test_empty_json_object_scout_brief_is_not_parsed():
    brief = prep.parse_scout_brief("{}", scout_label="X")
    assert brief.raw_status != "parsed"
    assert brief.main_case == [] and brief.opponent_best_case == [] and brief.evidence == []


def test_scout_brief_with_only_role_and_no_content_is_not_parsed():
    """光报了 preferred_role、四项实质内容一个都没给，也不算真的收集到东西。"""
    brief = prep.parse_scout_brief('{"preferred_role": "rebuttal"}', scout_label="X")
    assert brief.raw_status != "parsed"
    assert brief.preferred_role == "rebuttal"  # 角色偏好本身还是要保留，供 decide_roles 用


def test_scout_brief_with_real_content_is_still_parsed():
    """防止这条回归把正常情况也改坏了：真有内容的还是要标 parsed。"""
    brief = prep.parse_scout_brief(
        '{"preferred_role": "opening", "main_case": ["论点一"]}', scout_label="X",
    )
    assert brief.raw_status == "parsed"
    assert brief.main_case == ["论点一"]


def test_empty_json_object_team_review_is_not_parsed():
    review = prep.parse_team_review("{}", reviewer_label="Y")
    assert review.raw_status != "parsed"


def test_team_review_with_real_content_is_still_parsed():
    review = prep.parse_team_review('{"strongest_shared": "共同主线"}', reviewer_label="Y")
    assert review.raw_status == "parsed"
    assert review.strongest_shared == "共同主线"


def test_board_from_briefs_still_skips_empty_scout_briefs():
    """board_from_briefs 早就对 raw_status!=parsed 或 main_case 为空两条都设防；
    这条只是确认新引入的 "empty" 状态没有绕开这层已有的保护。"""
    empty = prep.parse_scout_brief("{}", scout_label="队长")
    real = prep.ScoutBrief(scout="队友", preferred_role="opening", main_case=["真论点"])
    stitched = prep.board_from_briefs([empty, real])
    assert "真论点" in stitched
    assert "队长" not in stitched
