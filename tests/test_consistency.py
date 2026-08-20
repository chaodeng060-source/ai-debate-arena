"""跨场一致性：观测怎么选、kappa/ICC 算得对不对。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import consistency as dc


def test_cohen_kappa_known_values() -> None:
    assert dc.cohen_kappa([("pro", "pro")] * 5) == 1.0
    # 经典例子：a/b 各 50%，重合 70% → κ = (0.7-0.5)/(1-0.5) = 0.4（Elliot 第 6 节举的数）
    pairs = [("pro", "pro")] * 35 + [("con", "con")] * 35 + [("pro", "con")] * 15 + [("con", "pro")] * 15
    assert dc.cohen_kappa(pairs) == pytest.approx(0.4)
    assert dc.cohen_kappa([]) is None


def test_fleiss_kappa_all_agree_and_split() -> None:
    assert dc.fleiss_kappa([["pro"] * 3, ["con"] * 3]) == 1.0
    # 每场 2:1 分裂且总体 → p_bar = , p_e = 0.5 → κ = -
    assert dc.fleiss_kappa([["pro", "pro", "con"], ["con", "con", "pro"]]) == pytest.approx(-1 / 3)


def test_icc_2_1_perfect_and_noisy() -> None:
    assert dc.icc_2_1([[1, 1, 1], [5, 5, 5], [9, 9, 9]]) == pytest.approx(1.0)
    # 同一评委整体偏高 2 分：绝对一致 ICC 应明显低于 1（ICC(2,1) 惩罚系统偏差）
    shifted = dc.icc_2_1([[1, 3], [5, 7], [9, 11]])
    assert shifted is not None and 0.5 < shifted < 1.0
    assert dc.icc_2_1([[1, 2]]) is None


def test_collect_selects_independent_ballots_and_unifies_judge_keys() -> None:
    legacy = {"run_id": "old", "jury": {"winner": "pro", "ballots": [
        {"ballot_id": "ballot-1", "valid": True, "winner": "pro", "presentation": {"pro": "A", "con": "B"}},
        {"ballot_id": "ballot-2-swapped", "valid": True, "winner": "con", "presentation": {"pro": "B", "con": "A"}},
        {"ballot_id": "ballot-3", "valid": True, "winner": "pro", "presentation": {"pro": "A", "con": "B"}},
    ]}}
    new = {"run_id": "new", "jury": {"winner": "pro", "ballots": [
        {"judge": "评委甲", "role": "primary", "valid": True, "winner": "pro",
         "scores": {"pro": {"rubric": [9, 9, 9, 9], "discretion": 25}, "con": {"rubric": [5, 5, 5, 5], "discretion": 20}}},
        {"judge": "评委乙", "role": "primary", "valid": True, "winner": "pro"},
        {"judge": "评委丙", "role": "primary", "valid": False},
        {"judge": "评委甲", "role": "recheck", "valid": True, "winner": "con"},   # 对调票：不是独立观测
    ]}}
    got = dc.collect([legacy, new])["matches"]
    assert [m["run_id"] for m in got] == ["old", "new"]
    # 旧式三张都算，换位那张映射到评委乙且 winner 已是 pro/con 口径
    assert got[0]["judges"] == {"评委甲": {"winner": "pro"}, "评委乙": {"winner": "con"}, "评委丙": {"winner": "pro"}}
    # 新式：recheck 跳过、无效跳过、分项带出来
    assert set(got[1]["judges"]) == {"评委甲", "评委乙"}
    assert got[1]["judges"]["评委甲"]["pro"] == [9.0, 9.0, 9.0, 9.0] and got[1]["judges"]["评委甲"]["disc"]["con"] == 20.0

    report = dc.analyse({"matches": got})
    assert report["n_matches"] == 2 and report["n_fully_scored"] == 0
    assert report["votes"]["winner_distribution"] == {"pro": 2, "con": 0, "undecided": 0}
    md = dc.to_markdown(report)
    assert "一边倒" in md and "Fleiss" in md
