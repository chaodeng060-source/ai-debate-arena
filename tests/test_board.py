"""选手榜口径：参赛按场去重、没判出胜负不计胜、MVP 只认评委票里的那一栏。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import board as board_tool


def _record(run_id: str, winner: str | None, *, roster: list[dict],
            mvp: str | None = None, ballots: list[dict] | None = None,
            status: str = "completed") -> dict:
    jury: dict = {"status": "decided" if winner else "disputed", "winner": winner,
                  "ballots": ballots if ballots is not None else [{"valid": True, "mvp": None}]}
    if mvp:
        jury["mvp"] = {"speaker": mvp, "votes": 2, "of": 3}
    return {"run_id": run_id, "status": status, "roster": roster, "jury": jury}


ROSTER = [
    {"name": "正方一辩", "side": "pro", "seat": 1, "model": "alpha"},
    {"name": "正方二辩", "side": "pro", "seat": 2, "model": "beta"},
    {"name": "反方一辩", "side": "con", "seat": 1, "model": "gamma"},
    {"name": "反方二辩", "side": "con", "seat": 2, "model": "alpha"},   # 同一模型两边都有席位
]


def test_tally_counts_play_win_mvp() -> None:
    board = board_tool.tally([
        _record("r1", "pro", roster=ROSTER, mvp="正方二辩"),
        _record("r2", "con", roster=ROSTER, mvp="反方一辩"),
        _record("r3", None, roster=ROSTER),          # 评审分歧：算参赛不算胜
    ])
    rows = {r["key"]: r for r in board["table"]}
    assert board["matches"] == 3 and board["decided"] == 2
    # alpha 两边都有席位：每场只算参赛一次，两场都在胜方 → 2 胜
    assert rows["alpha"]["played"] == 3 and rows["alpha"]["won"] == 2
    assert rows["beta"]["played"] == 3 and rows["beta"]["won"] == 1 and rows["beta"]["mvp"] == 1
    assert rows["gamma"]["won"] == 1 and rows["gamma"]["mvp"] == 1
    assert rows["beta"]["win_rate"] == round(1 / 3, 3)


def test_mvp_column_absent_in_old_records_is_reported_not_faked() -> None:
    old = _record("old", "pro", roster=ROSTER, ballots=[{"valid": True}])   # 票里没有 mvp 键
    new = _record("new", "pro", roster=ROSTER, mvp="正方一辩")
    board = board_tool.tally([old, new])
    assert board["mvp_capable_matches"] == 1 and board["matches"] == 2
    assert "此前 1 场的票里没有这一栏" in board_tool.to_markdown(board)
    assert sum(r["mvp"] for r in board["table"]) == 1


def test_load_records_dedupes_by_run_id_and_skips_unplayed(tmp_path: Path) -> None:
    raw = _record("same", None, roster=ROSTER, ballots=[])          # 只有 roster，没走到评审
    raw["jury"] = {}
    (tmp_path / "debate-a.json").write_text(json.dumps(raw), "utf-8")
    judged = _record("same", "pro", roster=ROSTER, mvp="正方一辩")   # 同一场的重判版
    (tmp_path / "debate-a-jury.json").write_text(json.dumps(judged), "utf-8")
    (tmp_path / "debate-b.json").write_text(json.dumps({"roster": ROSTER, "run_id": "b"}), "utf-8")

    records, skipped = board_tool.load_records(tmp_path)
    assert [r["run_id"] for r in records] == ["same"]      # 去重后取判过的那份
    assert records[0]["jury"]["winner"] == "pro"
    assert skipped == 1                                    # 没打完的那场报出来，不静默扔
    assert "另有 1 份记录没走到评审" in board_tool.to_markdown(
        board_tool.tally(records, skipped=skipped))
