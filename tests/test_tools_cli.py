"""tools/ 下命令行工具的默认路径：跟引擎认同一个数据目录，默认输出落在一定存在的地方。"""
from __future__ import annotations

import json


def test_board_default_dir_follows_debate_data_dir(tmp_path, monkeypatch):
    """README 说 DEBATE_DATA_DIR 管赛录放哪；引擎和 /api/debate/board 都认它，
    命令行的 tools/board.py、tools/consistency.py 以前却写死 data/debates/，读到的是另一处。"""
    from tools import board
    record = {
        "run_id": "debate-x",
        "roster": [{"name": "正方一辩", "side": "pro", "model": "m1"}],
        "jury": {"winner": "pro", "ballots": [{"valid": True, "winner": "pro"}]},
    }
    (tmp_path / "debate-x.json").write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setenv("DEBATE_DATA_DIR", str(tmp_path))

    records, skipped = board.load_records()
    assert [r["run_id"] for r in records] == ["debate-x"] and skipped == 0


def test_bench_overlap_writes_report_next_to_the_record_by_default(tmp_path, monkeypatch):
    """默认输出以前指向 notes/corner/，这个仓里没有这个目录——评委 CLI 都调完了才在
    写文件那一步 FileNotFoundError，额度白花。现在默认写在赛录旁边。"""
    from tools import bench_overlap
    src = tmp_path / "debate-x.json"
    src.write_text("{}", encoding="utf-8")
    canned = {
        "file": src.name, "topic": "甲/乙", "runs": 1, "panel": [], "asked": 0, "failed": 0,
        "questions": [], "seat_targets": {}, "seat_speeches": {}, "pair_overlap": {},
        "self_stability": {}, "hot_speeches": [],
    }

    async def fake_run(path, runs, timeout):   # 不真的调评委 CLI
        return canned

    monkeypatch.setattr(bench_overlap, "run", fake_run)
    assert bench_overlap.main([str(src)]) == 0
    assert (tmp_path / "debate-x-bench-overlap.md").is_file()
