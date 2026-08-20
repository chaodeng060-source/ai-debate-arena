from __future__ import annotations

import subprocess

import pytest

from arena import room


def test_effort_timeout_lifts_only_long_think_tiers() -> None:
    assert room.effort_timeout("max", 300) == 480
    assert room.effort_timeout("ultra", 300) == 480
    # 已经给得更宽就不往回收
    assert room.effort_timeout("max", 600) == 600
    for effort in ("xhigh", "high", "medium", "low", ""):
        assert room.effort_timeout(effort, 300) == 300


def _debater(effort: str = "max") -> dict:
    return {"engine": "claude", "model": "claude-fable-5", "effort": effort}


def test_run_cli_timeout_falls_back_to_low_effort_wrapup(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_once(d, system, prompt, timeout, *, research_tools=False):
        calls.append({"effort": d["effort"], "prompt": prompt, "timeout": timeout})
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
        return "收束短稿"

    monkeypatch.setattr(room, "_run_cli_once", fake_once)
    text = room._run_cli(_debater("max"), "sys", "正文题", 300)

    assert text == "收束短稿"
    assert calls[0]["timeout"] == 480 - room.WRAPUP_RESERVE
    assert calls[1]["effort"] == "low"
    assert calls[1]["timeout"] == room.WRAPUP_RESERVE
    assert calls[1]["prompt"].startswith(room.WRAPUP_ALERT)
    assert calls[1]["prompt"].endswith("正文题")


def test_run_cli_small_budget_keeps_single_shot(monkeypatch) -> None:
    calls: list[int] = []

    def fake_once(d, system, prompt, timeout, *, research_tools=False):
        calls.append(timeout)
        raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)

    monkeypatch.setattr(room, "_run_cli_once", fake_once)
    # 90s 的讨论收束/旁听笔记：不抬档、不切收束，超时原样冒泡
    with pytest.raises(subprocess.TimeoutExpired):
        room._run_cli(_debater("medium"), "sys", "p", 90)
    assert calls == [90]


def test_run_cli_double_timeout_still_raises(monkeypatch) -> None:
    def fake_once(d, system, prompt, timeout, *, research_tools=False):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)

    monkeypatch.setattr(room, "_run_cli_once", fake_once)
    with pytest.raises(subprocess.TimeoutExpired):
        room._run_cli(_debater("ultra"), "sys", "p", 300)


def test_speak_double_timeout_keeps_host_placeholder(monkeypatch) -> None:
    """两次都超时（连降档收束也没交）时，落一句主持人占位，不留空白段。"""
    def always_timeout(d, system, prompt, timeout, *, research_tools=False):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)

    monkeypatch.setattr(room, "_run_cli_once", always_timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        room._run_cli(_debater("max"), "sys", "p", 300)
