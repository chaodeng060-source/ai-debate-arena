"""README 里给人照抄的例子：开赛 curl 的请求体要能被引擎收下，提到的 extra 要真的存在。"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from arena import room

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_readme_start_example_is_accepted_and_zero_cost():
    m = re.search(r"curl -X POST \S*/api/debate/start .*?-d '(\{.*?\})'", README, re.S)
    assert m, "README 里找不到开赛的 curl 例子"
    body = json.loads(m.group(1))                      # 照抄的请求体得是合法 JSON
    err, params = room._parse_match_params(body)
    assert err is None, err
    seats = [*params["pool"], *(params["judge_pool"] or [])]
    assert params["judge_pool"], "不给 judge_pool 评委会落到本机 CLI 席位上"
    assert all(seat["engine"] == "external" for seat in seats), "README 的例子不能悄悄用上本机 CLI 席位（花额度）"


def test_extras_mentioned_in_readme_exist_in_pyproject():
    tomllib = pytest.importorskip("tomllib")   # Python 3.11 起才有
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = set(project.get("optional-dependencies", {}))
    mentioned = {name for group in re.findall(r'\.\[([A-Za-z0-9_,-]+)\]', README) for name in group.split(",")}
    assert mentioned, "README 里一个 extra 都没提到，检查一下正则"
    assert mentioned <= extras, f"README 提到了 pyproject 里没有的 extra：{sorted(mentioned - extras)}"
