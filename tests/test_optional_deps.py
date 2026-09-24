"""可选依赖：没装的时候给一句人话（该装哪个 extra），装了的时候全新 clone 也能直接跑。"""
from __future__ import annotations

import json
import sys

import pytest

RECORD = {
    "run_id": "debate-x", "status": "completed", "format": "mini",
    "topic": "甲/乙", "pro_side": "甲", "con_side": "乙",
    "roster": [{"name": "正方一辩", "side": "pro", "engine": "external", "model": "m1", "effort": "-"}],
    "transcript": [{
        "speaker": "正方一辩", "side": "pro", "stage": "正方一辩·立论", "schedule_index": 0,
        "text": "立论正文", "chars": 4, "limit": 1170, "truncated": False, "elapsed_sec": 1.0,
    }],
    "crossfire": [],
}


def _block_reportlab(monkeypatch) -> None:
    """模拟没装 reportlab：前面的测试可能已经把它的子模块加载进来了，一并挡掉。"""
    for name in [n for n in list(sys.modules) if n == "reportlab" or n.startswith("reportlab.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "reportlab", None)


def test_export_without_reportlab_still_writes_md_and_names_the_pdf_extra(tmp_path, monkeypatch, capsys):
    from tools import export
    src = tmp_path / "debate-x.json"
    src.write_text(json.dumps(RECORD, ensure_ascii=False), encoding="utf-8")
    _block_reportlab(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["export.py", str(src)])

    assert export.main() == 0
    out = capsys.readouterr().out
    assert (tmp_path / "debate-x.md").is_file()
    assert not (tmp_path / "debate-x.pdf").exists()
    assert '.[pdf]' in out, out


def test_rubric_pdf_without_reportlab_names_the_pdf_extra(tmp_path, monkeypatch, capsys):
    from tools import rubric_pdf
    _block_reportlab(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["rubric_pdf.py", "--out", str(tmp_path / "rubric.pdf")])

    assert rubric_pdf.main() == 2
    err = capsys.readouterr().err
    assert "reportlab" in err and '.[pdf]' in err, err


def test_rubric_pdf_creates_missing_output_directory(tmp_path, monkeypatch):
    """默认输出到 data/uploads/，全新 clone 里这个目录不存在——以前直接 FileNotFoundError。"""
    pytest.importorskip("reportlab")
    from tools import rubric_pdf
    out = tmp_path / "not" / "there" / "yet" / "rubric.pdf"
    monkeypatch.setattr(sys, "argv", ["rubric_pdf.py", "--out", str(out)])

    assert rubric_pdf.main() == 0
    assert out.is_file() and out.stat().st_size > 1_000
