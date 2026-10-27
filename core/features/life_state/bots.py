from __future__ import annotations


class BotRegistry:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.labels = self._labels()
    def _labels(self) -> dict[str, str]:
        raw = self.cfg.get("bot_labels") or ""
        if isinstance(raw, dict):
            return {str(k).strip(): str(v).strip() for k, v in raw.items() if str(k).strip()}
        parts = raw if isinstance(raw, list) else [x for line in str(raw).splitlines() for x in line.split(",")]
        out = {}
        for item in parts:
            text = str(item or "").strip()
            if "=" in text:
                bot, name = text.split("=", 1)
                if bot.strip() and name.strip():
                    out[bot.strip()] = name.strip()
        return out
    def label_for(self, bot: str) -> str:
        key = str(bot or "")
        return self.labels.get(key, key)

    def bot_ids(self) -> set[str]:
        root = self.cfg.root() if hasattr(self.cfg, "root") else {}
        section = root.get("bot_dialog") if isinstance(root, dict) else {}
        return {str(x).strip() for x in (section.get("bot_ids") or []) if str(x).strip()}
    def sender_is_bot(self, event) -> bool:
        try:
            sender = str(event.get_sender_id() or "").strip()
        except Exception:
            return False
        return bool(sender) and sender in self.bot_ids()
