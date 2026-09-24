"""可选依赖：没装的时候给一句人话（该装哪个 extra），装了的时候全新 clone 也能直接跑。"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

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


# ── 依赖声明：arena/ 和 tools/ 里 import 的第三方包，都得在 pyproject 里声明过 ─────────

# import 名 → pip 装的发行包名。starlette / pydantic 跟着 fastapi 一起装。
IMPORT_TO_DISTRIBUTION = {
    "fastapi": "fastapi", "starlette": "fastapi", "pydantic": "fastapi",
    "uvicorn": "uvicorn", "httpx": "httpx", "reportlab": "reportlab",
    "numpy": "numpy", "pytest": "pytest", "mcp": "mcp",
}


def _declared_distributions() -> dict[str, set[str]]:
    """{extra 名: 发行包名集合}；键 "" 是基础依赖。"""
    tomllib = pytest.importorskip("tomllib")   # Python 3.11 起才有；3.10 上跳过
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    groups = {"": list(project.get("dependencies", [])), **project.get("optional-dependencies", {})}
    return {
        group: {re.match(r"[A-Za-z0-9._-]+", req).group(0).lower().replace("_", "-") for req in reqs}
        for group, reqs in groups.items()
    }


def _third_party_imports() -> dict[str, set[str]]:
    sources = sorted([*(ROOT / "arena").glob("*.py"), *(ROOT / "tools").glob("*.py")])
    local = {"arena", "tools", *(p.stem for p in sources)}   # tools/export.py 会直接 import score
    found: dict[str, set[str]] = {}
    for path in sources:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top == "__future__" or top in sys.stdlib_module_names or top in local:
                    continue
                found.setdefault(top, set()).add(path.relative_to(ROOT).as_posix())
    return found


def test_every_third_party_import_is_declared_in_pyproject():
    declared = set().union(*_declared_distributions().values())
    imports = _third_party_imports()
    assert "fastapi" in imports, "扫描没扫到东西，检查一下路径"
    for top, files in sorted(imports.items()):
        where = "、".join(sorted(files))
        dist = IMPORT_TO_DISTRIBUTION.get(top)
        assert dist is not None, f"{top}（{where}）是新的第三方依赖：先在 pyproject 里声明，再补进映射表"
        assert dist in declared, f"{top}（{where}）用到了，pyproject 却没声明 {dist}"


def test_extras_named_in_install_hints_install_what_they_promise():
    declared = _declared_distributions()
    assert "fastapi" in declared[""]
    assert "uvicorn" in declared["demo"]            # tools/demo.py 的提示：pip install -e ".[demo]"
    assert "reportlab" in declared["pdf"]           # 导 PDF 的提示：pip install -e ".[pdf]"
    assert {"pytest", "httpx"} <= declared["dev"]   # fastapi 的 TestClient 要 httpx
