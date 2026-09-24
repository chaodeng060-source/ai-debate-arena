"""tools/bridge.py 的 cmd handler（接任何读 stdin / 吐 stdout 的命令行程序）。

四类假命令覆盖 B1 的四条要求：正常回稿、超时、非零退出、空输出都不能让桥的轮询循环挂掉；
超时/非零/空输出都要落地成「白卷」（handler 返回 None，不重试不代写）。
这里全用假命令（python -c 写的小脚本），不碰任何真实模型 API。

另外覆盖 B12 的投稿箱目录跟随 DEBATE_DATA_DIR。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import bridge  # noqa: E402


def _req(**over) -> dict:
    base = {"seat": "测试席", "kind": "speech", "system": "系统提示", "prompt": "题面"}
    base.update(over)
    return base


def test_cmd_handler_normal_reply_roundtrips_stdin_to_stdout():
    handler = bridge.make_cmd_handler([sys.executable, "-c",
        "import sys; text = sys.stdin.read(); print('回稿：' + text.splitlines()[0])"])
    out = handler(_req(system="系统提示行", prompt="题面"))
    assert out == "回稿：系统提示行"


def test_cmd_handler_timeout_is_treated_as_blank_and_does_not_raise():
    handler = bridge.make_cmd_handler(
        [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3,
    )
    assert handler(_req()) is None


def test_cmd_handler_nonzero_exit_is_treated_as_blank():
    handler = bridge.make_cmd_handler(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(1)"],
    )
    assert handler(_req()) is None


def test_cmd_handler_empty_output_is_treated_as_blank():
    handler = bridge.make_cmd_handler([sys.executable, "-c", "pass"])
    assert handler(_req()) is None


def test_cmd_handler_does_not_use_shell_and_survives_special_chars():
    """题面里塞 shell 元字符（; 反引号 $()），命令按参数列表执行，不会被解释成 shell 语法。"""
    handler = bridge.make_cmd_handler([sys.executable, "-c",
        "import sys; print(len(sys.stdin.read()))"])
    dangerous = "正常内容; rm -rf /tmp/should-not-run `echo hey` $(echo hey)"
    out = handler(_req(system="", prompt=dangerous))
    expected_stdin = f"\n\n{dangerous}"
    assert out == str(len(expected_stdin))


def test_run_loop_keeps_polling_after_cmd_handler_returns_none(tmp_path):
    """跟 run() 的既有契约对齐：handler 返回 None 时不写 reply、不算已回，也不让循环退出。"""
    from arena.prep import external_paths, external_request
    inbox = tmp_path / "inbox"
    req_path, reply_path = external_paths(inbox, "run-cmd", 1, "正方一辩")
    req = external_request(run_id="run-cmd", seq=1, seat="正方一辩", system="s", prompt="p",
                           deadline_epoch=time.time() + 60, kind="speech")
    req_path.parent.mkdir(parents=True, exist_ok=True)
    req_path.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")

    blank_handler = bridge.make_cmd_handler([sys.executable, "-c", "pass"])
    n = bridge.run(inbox, "run-cmd", blank_handler, once=True)
    assert n == 0
    assert not reply_path.exists()


def test_inbox_root_honors_debate_data_dir_env(tmp_path):
    """B12：桥的默认投稿箱曾经写死在仓库路径下，不认 DEBATE_DATA_DIR，外部席位永远等不到桥。
    用子进程全新导入一次 tools.bridge，确认它跟引擎（arena.room.TRANSCRIPT_DIR）一样认这个
    环境变量、默认值也完全一致——不在同一个进程里 reload，避免影响其它测试模块已经拿到的引用。"""
    env = dict(os.environ)
    env["DEBATE_DATA_DIR"] = str(tmp_path)
    code = "from tools import bridge; print(bridge.INBOX_ROOT)"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == str(tmp_path / "inbox")

    env.pop("DEBATE_DATA_DIR", None)
    code_default = (
        "from tools import bridge; from arena import room;"
        "print(bridge.INBOX_ROOT == room.TRANSCRIPT_DIR / 'inbox')"
    )
    out2 = subprocess.run([sys.executable, "-c", code_default], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=30)
    assert out2.returncode == 0, out2.stderr
    assert out2.stdout.strip() == "True"


def test_cmd_handler_wired_through_main_requires_cmd_flag():
    with pytest.raises(SystemExit):
        bridge.main(["--all", "--handler", "cmd"])
