#!/usr/bin/env python3
"""一条命令：起本地服务 + 打一场全 external 席位的假选手演示赛 + 打印观赛地址。

    .venv/bin/python tools/demo.py                  # 默认端口，比赛打完服务继续挂着
    .venv/bin/python tools/demo.py --open            # 顺手拉起浏览器
    .venv/bin/python tools/demo.py --port 8899

辩手（4 席）和评委（3 席）全部是外部席位，回稿由 tools/bridge.py 的 stub handler
本地代填——**零额度，不起任何真模型**。这只是验证「开赛→备赛→发言→质询→评委插问→
评审→观众票→观赛页」这条流程走得通，稿子是模板，**不代表任何真实 AI 的辩论质量**。

服务起在 127.0.0.1，比赛打完继续挂着可以回看；Ctrl+C 退出。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
import time
import uuid
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arena import room  # noqa: E402
from tools import bridge  # noqa: E402

DEFAULT_PORT = 8877

# 只认本机 Host：服务只绑 127.0.0.1，正常打开的地址不是 127.0.0.1 就是 localhost。
# 挡的是 DNS rebinding——恶意网页把自己的域名改指 127.0.0.1，浏览器就当它同源、能读能写本机端口，
# 跨站检查对它无效，只有 Host 头会露出对方的域名。
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")

# 四个外部辩手席位：engine=external 必须显式给 effort="-"（parse_pool 的字符集校验只认这个）。
DEMO_POOL = [
    {"engine": "external", "model": "demo:seat-1", "label": "演示席一", "effort": "-"},
    {"engine": "external", "model": "demo:seat-2", "label": "演示席二", "effort": "-"},
    {"engine": "external", "model": "demo:seat-3", "label": "演示席三", "effort": "-"},
    {"engine": "external", "model": "demo:seat-4", "label": "演示席四", "effort": "-"},
]
# 三个外部评委席位：judge_pool 给够 3 席、不设 owner，_draw_panel 就不会补位到本机 CLI
# 评委（那条路要真的调 claude/codex，会花额度）——零额度这条线全靠这三席都是 external。
DEMO_JUDGES = [
    {"engine": "external", "model": "demo:judge-1", "label": "演示评委一", "effort": "-"},
    {"engine": "external", "model": "demo:judge-2", "label": "演示评委二", "effort": "-"},
    {"engine": "external", "model": "demo:judge-3", "label": "演示评委三", "effort": "-"},
]
DEMO_PRO = "时间赋予生命意义"
DEMO_CON = "生命赋予时间意义"
DEMO_TOPIC = f"{DEMO_PRO}/{DEMO_CON}"


def new_run_id() -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"debate-demo-{stamp}-{uuid.uuid4().hex[:8]}"


def start_stub_bridge(inbox_root: Path, *, poll: float = 0.3) -> threading.Thread:
    """后台线程盯着投稿箱：见到外部席位的出题就用本地代填的稿子回，零额度。
    daemon=True——主进程退出（Ctrl+C）时它跟着走，不用额外收尾。"""
    t = threading.Thread(
        target=bridge.run,
        args=(inbox_root, None, bridge.stub_handler),
        kwargs={"poll": poll},
        daemon=True,
    )
    t.start()
    return t


async def run_demo_match(run_id: str, *, timeout: int = 30, crossfire_rounds: int = 1) -> None:
    """开一场 mini 2v2：辩手 4 + 评委 3，全部外部席位、stub 代填。跟
    tests/test_e2e_external_stub.py 走的是同一条引擎路径（room._run_match），
    区别只是这里给了固定辩题、真实抽签（draw=True）。"""
    await room._run_match(
        DEMO_TOPIC, DEMO_PRO, DEMO_CON, "mini", "zh",
        timeout=timeout, draw=True, crossfire_rounds=crossfire_rounds,
        prep_enabled=True, bench_enabled=True,
        pool=DEMO_POOL, judge_pool=DEMO_JUDGES, run_id=run_id,
    )


def build_app(run_id: str, *, timeout: int = 30, crossfire_rounds: int = 1,
             open_browser_url: str | None = None,
             allowed_hosts: tuple[str, ...] | list[str] | None = None):
    """起一个包含引擎路由（含 /viewer 观赛页）的 FastAPI app；lifespan 启动时顺带
    拉起 stub 桥、开这一场演示赛（可选自动开浏览器）。给了 allowed_hosts 就只认这几个
    Host（main() 传 LOOPBACK_HOSTS）；不给不校验，测试客户端的 Host 是 testserver。"""
    from fastapi import FastAPI

    @asynccontextmanager
    async def lifespan(_app):
        start_stub_bridge(room.INBOX_ROOT)
        asyncio.create_task(run_demo_match(run_id, timeout=timeout, crossfire_rounds=crossfire_rounds))
        if open_browser_url:
            threading.Timer(0.6, lambda: webbrowser.open(open_browser_url)).start()
        yield
        # 关闭时不特别清理：桥线程是 daemon；比赛 task 若还没打完，进程退出时一并结束——
        # Ctrl+C 就是要停，不等它打完。

    app = FastAPI(title="ai-debate-arena · demo", lifespan=lifespan)
    app.include_router(room.router)
    if allowed_hosts:
        from fastapi.middleware.trustedhost import TrustedHostMiddleware
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"默认 {DEFAULT_PORT}")
    ap.add_argument("--open", action="store_true", help="服务起来后自动拉起浏览器")
    args = ap.parse_args(argv)

    # 演示只跑一场，位置复判（A/B 对调票）用不上、只会多烧几轮 stub 往返——关掉更快。
    os.environ.setdefault("DEBATE_POSITION_RECHECK", "off")

    run_id = new_run_id()
    url = f"http://127.0.0.1:{args.port}/viewer?run_id={run_id}&demo=1"

    print("=" * 64)
    print("ai-debate-arena 演示：一条命令看一场假选手辩论赛")
    print("=" * 64)
    print(f"观赛地址：{url}")
    print("辩手与评委全部由本地脚本代填发言（零额度，不起任何真模型）——")
    print("这只验证「开赛→备赛→发言→质询→评委插问→评审→观众票」流程走得通，")
    print("不代表任何真实 AI 的辩论质量。")
    print("服务起在本机 127.0.0.1；比赛打完服务继续挂着，可以回看；Ctrl+C 退出。")
    print("=" * 64)

    app = build_app(run_id, open_browser_url=url if args.open else None,
                    allowed_hosts=LOOPBACK_HOSTS)

    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
