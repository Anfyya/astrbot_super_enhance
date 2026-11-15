"""已废弃的柴木门禁兼容空壳：保留空函数签名防止旧调用方 ImportError，不再执行任何拦截。"""

import re

WOOD = re.compile(r"$^")
FIRE = re.compile(r"$^")
REFUEL = re.compile(r"$^")


def actions(text: str, pattern):
    return iter(())


def burning(text: str, wood_context: bool = False) -> bool:
    return False


def refueling(text: str) -> bool:
    return False


def requirements(entry: dict, until) -> list:
    return []


def shared(first: dict, second: dict) -> bool:
    return False
