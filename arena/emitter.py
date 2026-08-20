"""推流出口 —— 比赛过程往哪儿播。

引擎每产生一条内容（开场白、一段发言、评委插问、裁决书……）就调一次
`emit()`。默认实现把事件写进 JSONL 赛录并打到 stdout，不依赖任何宿主。

要接进自己的系统（聊天室、WebSocket、Discord、日志管道……），实现
`Emitter` 协议再 `set_emitter(YourEmitter())` 就行，引擎一行不用改：

    from arena import emitter

    class MyRoom(emitter.Emitter):
        async def emit(self, body, *, title, kind, notify, run_id, meta):
            await my_chat.post(f"{title}\\n{body}")
            return "msg-id"          # 返回值会成为这条发言的短号来源

    emitter.set_emitter(MyRoom())

返回的字符串是这条消息在宿主侧的 id。引擎拿它给发言编短号，
辩手可以用 `[[quote:#尾6位]]` 精确引用对方某一条发言；宿主如果不关心
引用功能，返回空串即可，引擎会退回自增序号。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol


class Emitter(Protocol):
    """推流出口协议。实现它就能把比赛播到任何地方。"""

    async def emit(
        self,
        body: str,
        *,
        title: str,
        kind: str = "debate",
        notify: bool = False,
        run_id: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> str:
        ...


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class StdoutEmitter:
    """默认出口：一条一条打到 stdout，人眼能直接跟着看。

    `stream_path` 给了就同时把事件按 JSONL 落盘（一行一条，方便回放/接管道）。
    """

    def __init__(self, stream_path: Optional[Path | str] = None, *, quiet: bool = False):
        self.stream_path = Path(stream_path) if stream_path else None
        self.quiet = quiet

    def _write(self, event: dict) -> None:
        if not self.quiet:
            title = event.get("title") or ""
            print(f"\n── {title} ──", file=sys.stdout, flush=False)
            print(event.get("body") or "", file=sys.stdout, flush=True)
        if self.stream_path:
            self.stream_path.parent.mkdir(parents=True, exist_ok=True)
            with self.stream_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    async def emit(
        self,
        body: str,
        *,
        title: str,
        kind: str = "debate",
        notify: bool = False,
        run_id: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> str:
        msg_id = f"debate_{uuid.uuid4().hex[:12]}"
        event = {
            "id": msg_id,
            "kind": kind,
            "title": title,
            "body": body,
            "notify": notify,
            "run_id": run_id,
            "ts": _now_iso(),
        }
        if meta:
            event.update(meta)
        await asyncio.to_thread(self._write, event)
        return msg_id


class NullEmitter:
    """什么都不播。跑批量对局、只要赛录不要过程时用。"""

    async def emit(
        self,
        body: str,
        *,
        title: str,
        kind: str = "debate",
        notify: bool = False,
        run_id: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> str:
        return ""


def _default_emitter() -> Emitter:
    """环境变量 DEBATE_STREAM_PATH 给了就顺带落盘；DEBATE_QUIET=1 只落盘不打屏。"""
    path = os.environ.get("DEBATE_STREAM_PATH") or None
    quiet = os.environ.get("DEBATE_QUIET", "").strip() in {"1", "true", "yes"}
    return StdoutEmitter(path, quiet=quiet)


_emitter: Optional[Emitter] = None


def set_emitter(emitter: Optional[Emitter]) -> None:
    """换出口。传 None 恢复默认（stdout）。"""
    global _emitter
    _emitter = emitter


def get_emitter() -> Emitter:
    global _emitter
    if _emitter is None:
        _emitter = _default_emitter()
    return _emitter


async def emit(
    body: str,
    *,
    title: str,
    kind: str = "debate",
    notify: bool = False,
    run_id: Optional[str] = None,
    meta: Optional[dict] = None,
) -> str:
    """引擎内部统一走这个入口。异常一律吞掉——推流挂了不该让比赛中断。"""
    try:
        return str(await get_emitter().emit(
            body, title=title, kind=kind, notify=notify, run_id=run_id, meta=meta
        ) or "")
    except Exception:
        return ""
