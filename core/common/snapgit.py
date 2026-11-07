"""用 git 存「真正发给模型的东西」：一个会话（或一类内部调用）一个文件，每次覆盖写再提交一次。

git 只存跟前一版差的那点，所以每次都留、一直留也不怎么占地方
（11-07 试过：200 份请求快照 21MB，按时间提交进 git 压完 0.66MB）。
这个库只在服务器上，跟插件代码库是两回事：里面全是私聊，别推到任何地方；搬家时整个目录连 .git 一起搬。

存进去的 JSON 里，带换行的字符串拆成一行一行（{"$lines": [...]}），git diff 才看得出两次之间
具体哪几行变了；读回来用 decode() 拼回原样，跟存进去的一字不差。

在库目录里查（例：plugin_data/astrbot_plugin_super_enhance/request_snapshots/）：
  git log --date=iso -- 聊天/<会话>.json        每一次请求的时间线（提交时间就是请求时间，北京时间）
  git log --date=iso -- 内部/补写.json          补写每一次调模型
  git diff <提交1> <提交2> -- <文件>            两次之间她看到的东西变了哪些行
  python3 <插件目录>/core/common/snapgit.py show <库目录> <提交> <文件>
                                               那一次原样（字符串拼回去），重放脚本要的快照文件用它导
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

try:
    from astrbot.api import logger
except Exception:  # noqa: BLE001  单独当命令行用的时候没有 astrbot
    import logging

    logger = logging.getLogger("snapgit")

GIT = shutil.which("git")
LINES_KEY = "$lines"
# 每提交这么多次问一下 git 要不要压一遍（gc --auto：零散对象没攒够 GC_LOOSE 个它自己就不干）
GC_EVERY = 50
GC_LOOSE = 1000
# 提交时间按北京时间记，git log 看到的直接就是北京时间
TZ_OFFSET = "+0800"
TZ_SECONDS = 8 * 3600
TIMEOUT = 60
# 库被别人占着（插件重载那一下，新旧两份可能同时在写）时，等一会儿再试几回
LOCK_RETRIES = 5
LOCK_WAIT = 0.5
STALE_LOCK_SECONDS = 120


def encode(value: Any) -> Any:
    """带换行的字符串拆成一行一行（给 git diff 看）；别的原样。"""
    if isinstance(value, str):
        return {LINES_KEY: value.split("\n")} if "\n" in value else value
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [encode(v) for v in value]
    return value


def decode(value: Any) -> Any:
    """encode 的反过来：拆开的行拼回原来的字符串。"""
    if isinstance(value, dict):
        lines = value.get(LINES_KEY)
        if len(value) == 1 and isinstance(lines, list):
            return "\n".join(str(x) for x in lines)
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


def dumps(data: Any) -> str:
    return json.dumps(encode(data), ensure_ascii=False, indent=1, default=str) + "\n"


def clock(ts: float | None = None, fmt: str = "%m-%d %H:%M:%S") -> str:
    """时间戳 → 北京时间（容器是 UTC，不能用本地时间）。"""
    return time.strftime(fmt, time.gmtime(float(ts if ts is not None else time.time()) + TZ_SECONDS))


class GitStore:
    """一个 git 库。所有写都排进同一个后台线程，按先来后到一次一个；读哪个线程都行。"""

    def __init__(self, root) -> None:
        self.root = Path(root)
        self.ok: bool | None = None  # None：还没试过；True 能用；False 用不了（找不到 git、建库失败）
        self.worker = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="snapgit"
        )
        self._since_gc = 0

    # ------------------------------------------------------------------ 底层
    def _run(self, *args: str, env=None, check: bool = True) -> subprocess.CompletedProcess:
        for _ in range(LOCK_RETRIES):
            res = subprocess.run(
                [GIT or "git", "-C", str(self.root), *args],
                capture_output=True,
                env=env,
                timeout=TIMEOUT,
            )
            if res.returncode == 0 or b".lock" not in (res.stderr or b""):
                break
            time.sleep(LOCK_WAIT)
        if check and res.returncode != 0:
            err = (res.stderr or res.stdout or b"").decode("utf-8", "replace").strip()
            raise RuntimeError("git %s 失败：%s" % (args[0], err[:300]))
        return res

    def _out(self, *args: str) -> str:
        return self._run(*args).stdout.decode("utf-8", "replace")

    def ready(self) -> bool:
        """库能不能用；头一回调的时候建库（只在后台线程里调）。"""
        if self.ok is not None:
            return self.ok
        try:
            if not GIT:
                raise RuntimeError("找不到 git 命令")
            self.root.mkdir(parents=True, exist_ok=True)
            if not (self.root / ".git").is_dir():
                self._run("init", "-q")
            for key, val in (
                ("user.name", "super_enhance"),
                ("user.email", "snapshot@localhost"),
                ("core.quotepath", "false"),
                ("gc.auto", str(GC_LOOSE)),
                ("gc.autoDetach", "false"),
            ):
                self._run("config", key, val)
            ignore = self.root / ".gitignore"
            if not ignore.exists():
                ignore.write_text("*.tmp\nindex.json\nreq_*.json\n", encoding="utf-8")
            lock = self.root / ".git" / "index.lock"
            if lock.exists() and time.time() - lock.stat().st_mtime > STALE_LOCK_SECONDS:
                # 上次提交到一半被杀了留下的（正在提交的锁不会放这么久）
                lock.unlink()
            self.ok = True
        except Exception as exc:  # noqa: BLE001
            logger.error("[super_enhance/snap] git 库用不了：%s", exc)
            self.ok = False
        return self.ok

    def has_repo(self) -> bool:
        return (self.root / ".git").is_dir()

    def submit(self, fn: Callable, *args: Any) -> concurrent.futures.Future:
        """排进后台线程；出错只记日志。"""
        return self.worker.submit(self._guard, fn, *args)

    @staticmethod
    def _guard(fn: Callable, *args: Any) -> Any:
        try:
            return fn(*args)
        except Exception:  # noqa: BLE001
            logger.error("[super_enhance/snap] 后台存快照出错", exc_info=True)
            return None

    # ------------------------------------------------------------------ 写（只在后台线程里调）
    def commit(self, rel: str, body: str, message: str, when: float | None = None) -> str:
        """覆盖写 rel 再提交；回提交号。内容跟上一版一模一样时不新提交，回当前的。"""
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(body, encoding="utf-8")
        os.replace(temp, path)
        self._run("add", "--", rel)
        stamp = "@%d %s" % (int(when if when is not None else time.time()), TZ_OFFSET)
        env = dict(os.environ, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        res = self._run("commit", "-q", "-m", message, env=env, check=False)
        if res.returncode != 0 and self._out("status", "--porcelain", "--", rel).strip():
            err = (res.stderr or res.stdout or b"").decode("utf-8", "replace").strip()
            raise RuntimeError("git commit 失败：%s" % err[:300])
        head = self._out("rev-parse", "HEAD").strip()
        self._since_gc += 1
        if self._since_gc >= GC_EVERY:
            self._since_gc = 0
            self._run("gc", "--auto", "--quiet", check=False)
        return head

    # ------------------------------------------------------------------ 读
    def show(self, commit: str, rel: str) -> str | None:
        """那一次提交里这个文件的原文（还是拆成行的样子）；取不到回 None。"""
        if not commit or not self.has_repo():
            return None
        res = self._run("show", "%s:%s" % (commit, rel), check=False)
        if res.returncode != 0:
            return None
        return res.stdout.decode("utf-8", "replace")

    def find(self, needle: str) -> str:
        """提交说明里带着这串字的最近一次提交；没有回空串。"""
        if not needle or not self.has_repo():
            return ""
        res = self._run(
            "log", "-1", "--format=%H", "--fixed-strings", "--grep=" + needle, check=False
        )
        return res.stdout.decode("utf-8", "replace").strip() if res.returncode == 0 else ""


def _main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[0] == "show":
        root, commit, rel = argv[1:]
        res = subprocess.run(
            [GIT or "git", "-C", root, "show", "%s:%s" % (commit, rel)], capture_output=True
        )
        if res.returncode != 0:
            sys.stderr.write(res.stderr.decode("utf-8", "replace"))
            return 1
        data = decode(json.loads(res.stdout.decode("utf-8")))
        sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
        return 0
    sys.stdout.write(__doc__ or "")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
