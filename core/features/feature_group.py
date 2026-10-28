"""插件功能的有序启动 / 停止。"""

from __future__ import annotations

from dataclasses import dataclass

from astrbot.api import logger


@dataclass(frozen=True)
class FeatureHandle:
    name: str
    target: object
    start: str = "install"
    stop: str = "uninstall"


class FeatureGroup:
    def __init__(self, *features: FeatureHandle) -> None:
        self.features = list(features)

    def start(self) -> None:
        started: list[FeatureHandle] = []
        try:
            for feature in self.features:
                method = getattr(feature.target, feature.start, None)
                if callable(method):
                    method()
                started.append(feature)
        except Exception:
            logger.error("[super_enhance] 启动功能失败，回退已启动部分", exc_info=True)
            self._stop(started)
            raise

    def stop(self) -> None:
        self._stop(self.features)

    def _stop(self, features: list[FeatureHandle]) -> None:
        for feature in reversed(features):
            method = getattr(feature.target, feature.stop, None)
            if not callable(method):
                continue
            try:
                method()
            except Exception:
                logger.error("[super_enhance] 停止功能 %s 失败", feature.name, exc_info=True)
