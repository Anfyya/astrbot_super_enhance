"""网页实时推送：写数据的地方喊一声（notify），推送接口挂着等（listen），不查文件、不轮询。

插件的数据文件都从 ``jsonio.save`` 写出去，存完按文件名喊一声；会话由页面接口包住 AstrBot 数据库的
几个写会话方法，写完喊一声。写盘可能发生在别的线程里（``asyncio.to_thread`` 里算存货时会顺手存盘），
所以都转回事件循环再分发。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

# 数据目录里的文件 → 网页上哪一块跟着变（以 / 结尾的是整个子目录）
TOPICS = (
    ("world/", ("world",)),
    ("world_state.json", ("world",)),
    ("routine.json", ("world",)),
    ("step_notes.json", ("world",)),
    ("step_thoughts.json", ("world",)),
    ("daily_seeds.json", ("world", "pools")),
    ("things.json", ("things", "world")),
    ("pools.json", ("pools",)),
    ("abilities.json", ("ledger",)),
    ("promises.json", ("ledger",)),
    ("trainings.json", ("ledger",)),
    ("request_snapshots/index.json", ("requests",)),
)

_loop: asyncio.AbstractEventLoop | None = None
_root = ""
_queues: set = set()


def bind(loop: asyncio.AbstractEventLoop, data_dir) -> None:
    """页面接口注册时调一次：记下事件循环和插件数据目录。"""
    global _loop, _root
    _loop = loop
    _root = os.path.realpath(str(data_dir)) + os.sep


def topics_of(path) -> tuple:
    """数据目录里这个文件归网页的哪几块；不在表里的不推。"""
    if not _root:
        return ()
    text = os.path.realpath(str(Path(path)))
    if not text.startswith(_root):
        return ()
    rel = text[len(_root):].replace(os.sep, "/")
    for prefix, topics in TOPICS:
        if rel == prefix or (prefix.endswith("/") and rel.startswith(prefix)):
            return topics
    return ()


def file_saved(path) -> None:
    if not _queues:
        return
    for topic in topics_of(path):
        notify(topic)


def notify(topic: str, payload: Any = None) -> None:
    """喊一声：topic 是网页上哪一块变了（conv / world / things……），run 带着跑完的结果。没人在听就什么都不做。"""
    loop = _loop
    if loop is None or not _queues or loop.is_closed():
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        _fan_out(topic, payload)
        return
    try:
        loop.call_soon_threadsafe(_fan_out, topic, payload)
    except RuntimeError:
        pass


def _fan_out(topic: str, payload: Any) -> None:
    for queue in list(_queues):
        try:
            queue.put_nowait((topic, payload))
        except asyncio.QueueFull:
            pass


def listen() -> asyncio.Queue:
    """推送接口连上时登记一个队列，断开时用 stop 撤掉。"""
    queue: asyncio.Queue = asyncio.Queue(maxsize=512)
    _queues.add(queue)
    return queue


def stop(queue: asyncio.Queue) -> None:
    _queues.discard(queue)
