"""船屋与海边小镇的结构化金钱账本：分账户保管、物理携款、赊账/待收与物价边界。

设计原则：
1. `money.json` 保存已固化的账户余额、流水记录和欠账/待收；
2. 幕间补写（`step.py`）排进未来或进行中世界线条目的 `"money"` 字段先挂在 `entry["money"]` 上，
   `snapshot(now, world)` 只实时叠加「时刻已到且未被打断」的条目；已彻底完结一段的条目由
   `archive_settled()` 固化入 `money.json`，防止尚未发生的未来安排被对话取消时污染真实余额；
3. 100% 纯结构化校验，绝不从叙事正文猜数字。
"""

from __future__ import annotations

import copy
import datetime
import math
import threading
from pathlib import Path
from typing import Iterable

from astrbot.api import logger

from ...common import jsonio, timeutil
from .ledger import _JsonBook
from .things import CAT_FOOD, CAT_ITEM, norm_step
from .world import clean_steps, clean_text, fmt_time, is_event, parse_time, step_finished_at

MONEY_FILE_NAME = "money.json"
MAX_RECORDS = 400
SETTLE_DELAY_MINUTES = 90
VOID_KEEP_DAYS = 14

ACCOUNT_ORDER = ("master", "tin_box", "napcat2", "napcat")

DEFAULT_ACCOUNTS = {
    "master": {
        "label": "主人钱包",
        "balance": 3200.0,
        "place": "主人身上 / 书桌暗格",
        "note": "当家人与船长管着的大头家底，未经主人明确发话或同行出钱，其他人不能擅自动用",
    },
    "tin_box": {
        "label": "船屋公用铁盒",
        "balance": 100.0,
        "place": "船舱·矮桌抽屉里（p157 旧饼干铁盒）",
        "note": "日常买菜、买炭柴、买煤油与零碎家用；出门采买前要从盒里拿现钱揣身上，回来把零钱放回盒里",
    },
    "napcat2": {
        "label": "亚托莉口袋",
        "balance": 10.0,
        "place": "亚托莉·身上袖袋",
        "note": "日常跑腿找零、攒下的小零花；去集市买东西或替人跑腿付现只动用她兜里带着的钱",
    },
    "napcat": {
        "label": "丛雨零花",
        "balance": 0.0,
        "place": "丛雨·袖子里",
        "note": "丛雨的个人零钱；外人看不见她，没法独自去摊上交易，需要花钱得托主人或亚托莉代办",
    },
}

ACCOUNT_ALIASES = {
    "master": "master",
    "主人": "master",
    "主人钱包": "master",
    "船长": "master",
    "tin_box": "tin_box",
    "tinbox": "tin_box",
    "铁盒": "tin_box",
    "公用铁盒": "tin_box",
    "船屋铁盒": "tin_box",
    "船屋公用铁盒": "tin_box",
    "p157": "tin_box",
    "napcat2": "napcat2",
    "亚托莉": "napcat2",
    "亚托莉口袋": "napcat2",
    "napcat": "napcat",
    "丛雨": "napcat",
    "丛雨零花": "napcat",
}

MONEY_KINDS = ("earn", "spend", "transfer", "debt_add", "debt_settle", "adjust")
KIND_ALIASES = {
    "earn": "earn",
    "收入": "earn",
    "赚钱": "earn",
    "收款": "earn",
    "收钱": "earn",
    "卖": "earn",
    "spend": "spend",
    "支出": "spend",
    "花钱": "spend",
    "买": "spend",
    "付款": "spend",
    "transfer": "transfer",
    "转入": "transfer",
    "拿钱": "transfer",
    "放回": "transfer",
    "上交": "transfer",
    "给钱": "transfer",
    "拨款": "transfer",
    "交公": "transfer",
    "debt_add": "debt_add",
    "赊账": "debt_add",
    "欠账": "debt_add",
    "挂账": "debt_add",
    "待收": "debt_add",
    "debt_settle": "debt_settle",
    "还账": "debt_settle",
    "还款": "debt_settle",
    "结账": "debt_settle",
    "收尾款": "debt_settle",
    "adjust": "adjust",
    "调账": "adjust",
}

KIND_LABELS = {
    "earn": "进账",
    "spend": "花销",
    "transfer": "存取交接",
    "debt_add": "记欠/挂账",
    "debt_settle": "还账/结款",
    "adjust": "校准调账",
}

DEBT_DIRECTIONS = ("payable", "receivable")
EARN_REASONS = ("work", "salvage", "sell", "consign_payout")

# 单笔无主人事先委托的常规进账上限
MAX_ROUTINE_EARN = 300.0
MAX_WORK_EARN = 50.0
MAX_SALVAGE_EARN = 250.0
AUTONOMOUS_SELL_LIMIT = 120.0
# 大额进账习惯上交主人的参考阈值
HANDOVER_HINT_YUAN = 50.0

SEASIDE_PRICE_GUIDE = (
    "海边小镇人民币物价与赚钱门路（写花销/挣钱严格照这个尺度，别乱喊天价）：\n"
    "- 吃食日常：两条鲜海鱼/小螺虾 6~15 元，一把青菜/萝卜/豆腐 3~8 元，五斤米面/豆子 15~25 元，"
    "盐糖醋酱一瓶袋 5~12 元，食用油一瓶 20~35 元，摊头小吃/热汤包 5~15 元。\n"
    "- 柴炭煤油与日用：干硬木柴约 1~1.5 元/公斤（买 20 公斤约 20~30 元，半湿原木 100 公斤约 60~90 元），紧俏闷炭一小罐 15~30 元，点灯煤油一壶 18~30 元，"
    "绳钉灯芯肥皂火柴 3~12 元，公共澡堂泡一回 6~10 元/人。\n"
    "- 冬衣大件（必须主人拍板）：旧衣铺厚棉衣 35~60 元，旧棉被 20~40 元，修船大件木料五金 60~250 元。\n"
    "- 赚钱门路（全须真干活或真交出库存实物，不准凭空变钱）：\n"
    "  1. 打零工/手艺零活：帮码头渔民清网卸货、帮诊所学校或镇民修旧收音机/灯具/小水泵，挣 10~35 元；\n"
    "  2. 废金属过秤卖收购站：必须先把真在库存里的废铜旧铁旧零件扛去（items 记消耗/送人），"
    f"按斤过秤换 15~{int(AUTONOMOUS_SELL_LIMIT)} 元（单笔 ≤ {int(AUTONOMOUS_SELL_LIMIT)} 元可自主结现；超过须先问主人）；\n"
    "  3. 打捞所接委托单：外人下单捞沉船/公寓物件，须主人同意后再接，交货后收 30~150 元工钱；\n"
    "  4. 完好旧时代遗物寄卖：沉街摸到的完好相机/唱片/老怀表随补给船寄去东港福记旧物行，等下趟船结算 200~800 元；\n"
    "  5. 多余渔获/鱼干/好浮木在集市或船坞卖出，换 8~40 元，或直接以物易物少掏现金。"
)


def fmt_yuan(value) -> str:
    """把人民币数值格式化成干净可读的短串（整数不带 .00，带小数保留到角/分）。"""
    if isinstance(value, bool):
        return "0"
    try:
        raw = float(value or 0.0)
        if not math.isfinite(raw):
            return "0"
        num = round(raw, 2)
    except Exception:
        return "0"
    if abs(num - round(num)) < 1e-6:
        return str(int(round(num)))
    if abs(num * 10 - round(num * 10)) < 1e-6:
        return "%.1f" % num
    return "%.2f" % num


def round_yuan(value) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        num = float(value)
    except Exception:
        return 0.0
    if not math.isfinite(num):
        return 0.0
    return round(num, 2)


def _strict_yuan(value, field: str = "金额") -> float:
    """严格校验金额数值：拒绝 bool、非数字、NaN 与无穷大并抛 ValueError。"""
    if isinstance(value, bool):
        raise ValueError("%s必须是有效数字" % field)
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError("%s必须是有效数字" % field)
    if not math.isfinite(num):
        raise ValueError("%s不能为 NaN 或无穷大" % field)
    return round(num, 2)


def normalize_account(raw) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if text in ACCOUNT_ALIASES:
        return ACCOUNT_ALIASES[text]
    low = text.lower()
    if low in ACCOUNT_ALIASES:
        return ACCOUNT_ALIASES[low]
    for k, v in ACCOUNT_ALIASES.items():
        if k and k in text:
            return v
    return ""


def normalize_kind(raw) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if text in KIND_ALIASES:
        return KIND_ALIASES[text]
    return KIND_ALIASES.get(text.lower(), "")


def normalize_direction(raw) -> str:
    text = str(raw or "").strip().lower()
    if text in ("payable", "owe", "欠", "欠人", "赊账"):
        return "payable"
    if text in ("receivable", "owed", "待收", "别人欠", "应收"):
        return "receivable"
    return ""


def _blank_book() -> dict:
    return {
        "version": 1,
        "next_id": 1,
        "next_debt_id": 1,
        "accounts": copy.deepcopy(DEFAULT_ACCOUNTS),
        "debts": [],
        "records": [],
        "settled_wl": {},
        "void_wl": {},
    }


def _op_at(entry: dict, raw_op: dict) -> datetime.datetime | None:
    """复用 world.step_finished_at 计算世界线条目中单笔 money 动作发生的准确时刻；未中断时越界 step 自动 clamp 到总步数。"""
    at_s = str(raw_op.get("at") or "").strip()
    if at_s:
        dt = parse_time(at_s)
        if dt is not None:
            return dt
    total_steps = len(clean_steps(entry.get("steps")))
    raw_step = raw_op.get("step")
    try:
        step_no = int(raw_step) if raw_step not in (None, "") else 0
    except Exception:
        step_no = 0
    if step_no < 1:
        step_no = total_steps if (not entry.get("stopped") and total_steps > 0) else 0
    elif not entry.get("stopped"):
        step_no = min(step_no, total_steps) if total_steps > 0 else 0
    return step_finished_at(entry, step_no)


def _entry_wl_ops(entry: dict, date_text: str) -> list[tuple[str, datetime.datetime, dict]]:
    """提取一条世界线记录里在真实中断点之前已经到点发生的 money 操作。"""
    if not isinstance(entry, dict) or is_event(entry):
        return []
    m_list = entry.get("money")
    if not isinstance(m_list, list) or not m_list:
        return []
    eid = str(entry.get("id") or "").strip()
    out = []
    for idx, op in enumerate(m_list):
        if not isinstance(op, dict):
            continue
        dt = _op_at(entry, op)
        if dt is None:
            continue
        wl_key = f"{date_text}/{eid}/{idx}"
        out.append((wl_key, dt, op))
    return out


class MoneyBook(_JsonBook):
    """分账户金钱账本 + 挂账/待收 + 结构化收支流水。"""

    def __init__(self, path: Path) -> None:
        super().__init__(path, _blank_book())
        self._lock = threading.RLock()
        self._world = None

    def _resolve_world(self, world=None):
        if world is not None:
            self._world = world
            return world
        return self._world

    def _find_wl_op(self, wl_key: str, world=None, *, now: datetime.datetime | None = None):
        """在世界线中精确查找 wl_key (<date>/<eid>/<idx>) 对应的真实未中断操作，返回 (entry, op_dt, op, wl_ref) 或 None。"""
        w = self._resolve_world(world)
        if w is None or not wl_key:
            return None
        parts = str(wl_key).strip().split("/")
        if len(parts) != 3:
            return None
        date_str, eid, idx_str = parts
        if not date_str or not eid or not idx_str.isdigit():
            return None
        flat = now.replace(tzinfo=None) if now is not None else None
        try:
            entries = w.entries(date_str)
        except Exception:
            return None
        for entry in entries:
            if str(entry.get("id") or "").strip() != eid:
                continue
            for key, op_dt, op in _entry_wl_ops(entry, date_str):
                if key == wl_key:
                    if flat is not None and op_dt > flat:
                        return None
                    return entry, op_dt, op, f"{date_str}/{eid}"
        return None

    def _materialize_wl_debt(
        self,
        data: dict,
        debt_id: str,
        *,
        now: datetime.datetime | None = None,
        world=None,
    ) -> dict | None:
        """若 debt_id 是尚未固化的世界线欠账 (wl_<date>/<eid>/<idx>)，将其就地固化进 data['debts'] 并记 settled_wl 防重复归档。"""
        did = str(debt_id or "").strip()
        if not did.startswith("wl_"):
            return None
        existing = next((d for d in data["debts"] if isinstance(d, dict) and str(d.get("id") or "") == did), None)
        if existing is not None:
            return existing
        wl_key = did[3:]
        if not wl_key or wl_key in (data.get("void_wl") or {}):
            return None
        hit = self._find_wl_op(wl_key, world=world, now=now)
        if hit is None:
            return None
        entry, op_dt, op, wl_ref = hit
        if normalize_kind(op.get("kind")) != "debt_add":
            return None
        accs = data["accounts"]
        debts_map = {str(d["id"]): d for d in data["debts"] if isinstance(d, dict) and d.get("id")}
        rec = self._apply_op_on_state(
            accs,
            debts_map,
            op,
            at_str=fmt_time(op_dt),
            source="step",
            wl_key=wl_key,
            wl_ref=wl_ref,
            who=str(entry.get("who") or "").strip(),
            allow_negative=True,
        )
        debt = debts_map.get(did)
        if debt is None:
            return None
        data["debts"] = list(debts_map.values())
        if rec is not None and not any(str(r.get("wl_key") or "") == wl_key for r in data["records"] if isinstance(r, dict)):
            data["records"].append(rec)
            if len(data["records"]) > MAX_RECORDS:
                data["records"] = data["records"][-MAX_RECORDS:]
        data.setdefault("settled_wl", {})[wl_key] = fmt_time(now or timeutil.now())
        return debt

    def _ensure_shape(self) -> dict:
        with self._lock:
            data = self._load()
            accs = data.setdefault("accounts", {})
            for k, meta in DEFAULT_ACCOUNTS.items():
                if k not in accs or not isinstance(accs[k], dict):
                    accs[k] = copy.deepcopy(meta)
                else:
                    for mk, mv in meta.items():
                        if mk not in accs[k]:
                            accs[k][mk] = mv
                    accs[k]["balance"] = round_yuan(accs[k].get("balance", 0.0))
            if not isinstance(data.get("debts"), list):
                data["debts"] = []
            if not isinstance(data.get("records"), list):
                data["records"] = []
            if not isinstance(data.get("settled_wl"), dict):
                data["settled_wl"] = {}
            if not isinstance(data.get("void_wl"), dict):
                data["void_wl"] = {}
            return data

    def _next_debt_id(self) -> str:
        with self._lock:
            data = self._ensure_shape()
            try:
                val = int(data.get("next_debt_id") or 1)
            except Exception:
                val = 1
            data["next_debt_id"] = val + 1
            return f"d{val}"

    def _next_rec_id(self) -> str:
        with self._lock:
            return f"m{self._next_id()}"

    def _apply_op_on_state(
        self,
        accs: dict,
        debts: dict[str, dict],
        op: dict,
        *,
        at_str: str,
        source: str,
        wl_key: str = "",
        wl_ref: str = "",
        who: str = "",
        allow_negative: bool = True,
    ) -> dict | None:
        """把单笔已结构化校验过的操作套用到临时/持久化状态上，并返回标准化流水记录。"""
        kind = normalize_kind(op.get("kind"))
        if kind not in MONEY_KINDS:
            return None
        raw_amount = op.get("amount")
        if raw_amount is None:
            amount = 0.0
        else:
            if isinstance(raw_amount, bool):
                return None
            try:
                f_amt = float(raw_amount)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(f_amt):
                return None
            amount = round(f_amt, 2)
        what = clean_text(op.get("what"), 120)
        counterparty = clean_text(op.get("counterparty"), 40)
        items_ref = [str(x).strip() for x in (op.get("items") or []) if str(x).strip()]
        acc = normalize_account(op.get("account"))
        from_acc = normalize_account(op.get("from_account"))
        to_acc = normalize_account(op.get("to_account"))
        debt_id = str(op.get("debt_id") or "").strip()

        rec = {
            "id": str(op.get("id") or "") or (wl_key if wl_key else self._next_rec_id()),
            "at": at_str,
            "kind": kind,
            "reason": str(op.get("reason") or "").strip().lower(),
            "account": acc,
            "from_account": from_acc,
            "to_account": to_acc,
            "amount": amount,
            "who": str(who or op.get("who") or "").strip(),
            "counterparty": counterparty,
            "what": what,
            "items": items_ref,
            "debt_id": debt_id,
            "source": source,
            "wl": wl_ref,
            "wl_key": wl_key,
        }

        if kind == "earn":
            if acc not in accs or amount <= 0:
                return None
            accs[acc]["balance"] = round_yuan(accs[acc]["balance"] + amount)
        elif kind == "spend":
            if acc not in accs or amount <= 0:
                return None
            if not allow_negative and accs[acc]["balance"] + 1e-6 < amount:
                return None
            accs[acc]["balance"] = round_yuan(accs[acc]["balance"] - amount)
        elif kind == "transfer":
            if from_acc not in accs or to_acc not in accs or from_acc == to_acc or amount <= 0:
                return None
            if not allow_negative and accs[from_acc]["balance"] + 1e-6 < amount:
                return None
            accs[from_acc]["balance"] = round_yuan(accs[from_acc]["balance"] - amount)
            accs[to_acc]["balance"] = round_yuan(accs[to_acc]["balance"] + amount)
        elif kind == "debt_add":
            direction = normalize_direction(op.get("direction")) or "payable"
            if not counterparty or amount < 0:
                return None
            if not debt_id:
                debt_id = f"wl_{wl_key}" if wl_key else self._next_debt_id()
            rec["debt_id"] = debt_id
            rec["direction"] = direction
            debts[debt_id] = {
                "id": debt_id,
                "direction": direction,
                "counterparty": counterparty,
                "amount": amount,
                "remaining": amount,
                "about": what or clean_text(op.get("about"), 100),
                "status": "open",
                "created": at_str,
                "updated": at_str,
                "source": source,
                "wl": wl_ref,
                "wl_key": wl_key,
            }
        elif kind == "debt_settle":
            if amount < 0:
                return None
            target = debts.get(debt_id)
            if target is None and counterparty:
                for cand in debts.values():
                    if cand.get("status") == "open" and counterparty in str(cand.get("counterparty") or ""):
                        target = cand
                        debt_id = cand["id"]
                        rec["debt_id"] = debt_id
                        break
            direction = (target or {}).get("direction") or normalize_direction(op.get("direction")) or "payable"
            rec["direction"] = direction
            if target is not None:
                rem = round_yuan(target.get("remaining", target.get("amount", 0.0)))
                if rem > 0 and amount <= 0:
                    return None
            if acc and acc in accs and amount > 0:
                if direction == "payable":
                    if not allow_negative and accs[acc]["balance"] + 1e-6 < amount:
                        return None
                    accs[acc]["balance"] = round_yuan(accs[acc]["balance"] - amount)
                else:
                    accs[acc]["balance"] = round_yuan(accs[acc]["balance"] + amount)
            if target is not None:
                rem = round_yuan(target.get("remaining", target.get("amount", 0.0)))
                if rem <= 0:
                    target["remaining"] = 0.0
                    target["status"] = "settled"
                else:
                    left = max(0.0, round_yuan(rem - amount))
                    target["remaining"] = left
                    target["status"] = "settled" if left <= 0.01 else "open"
                target["updated"] = at_str
        elif kind == "adjust":
            if acc not in accs:
                return None
            accs[acc]["balance"] = round_yuan(accs[acc]["balance"] + amount)

        return rec

    def _collect_pending_wl(self, moment: datetime.datetime, world, settled: dict, void_wl: dict) -> list[tuple[str, datetime.datetime, dict, dict]]:
        if world is None:
            return []
        flat = moment.replace(tzinfo=None)
        out = []
        for back in range(6, -1, -1):
            d_str = (flat.date() - datetime.timedelta(days=back)).isoformat()
            for entry in world.entries(d_str):
                eid = str(entry.get("id") or "").strip()
                who = str(entry.get("who") or "").strip()
                wl_ref = f"{d_str}/{eid}" if eid else ""
                for wl_key, op_dt, op in _entry_wl_ops(entry, d_str):
                    if wl_key in settled or wl_key in void_wl:
                        continue
                    if op_dt > flat:
                        continue
                    out.append((wl_key, op_dt, op, {"who": who, "wl": wl_ref, "entry": entry}))
        out.sort(key=lambda x: (x[1], x[0]))
        return out

    def snapshot(self, now: datetime.datetime, world=None) -> dict:
        """计算到 `now` 为止的全盘真实账面（固化余额 + 到点未中断世界线收支）。"""
        with self._lock:
            eff_world = self._resolve_world(world)
            data = self._ensure_shape()
            accs = copy.deepcopy(data["accounts"])
            debts_map: dict[str, dict] = {
                str(d["id"]): copy.deepcopy(d) for d in data["debts"] if isinstance(d, dict) and d.get("id")
            }
            recs = [copy.deepcopy(r) for r in data["records"] if isinstance(r, dict)]
            pending = self._collect_pending_wl(now, eff_world, data.get("settled_wl") or {}, data.get("void_wl") or {})
            for wl_key, op_dt, op, meta in pending:
                rec = self._apply_op_on_state(
                    accs,
                    debts_map,
                    op,
                    at_str=fmt_time(op_dt),
                    source="world",
                    wl_key=wl_key,
                    wl_ref=meta["wl"],
                    who=meta["who"],
                    allow_negative=True,
                )
                if rec is not None:
                    recs.append(rec)

            recs.sort(key=lambda r: (str(r.get("at") or ""), str(r.get("id") or "")))
            # 从当前真实结尾余额逆推每笔流水发生后的账户结余
            cur_bal = {k: float(v.get("balance") or 0.0) for k, v in accs.items()}
            for rec in reversed(recs):
                kind = rec.get("kind")
                amt = float(rec.get("amount") or 0.0)
                if kind == "transfer":
                    fa, ta = rec.get("from_account") or "", rec.get("to_account") or ""
                    if fa in cur_bal:
                        rec["from_after"] = round_yuan(cur_bal[fa])
                        cur_bal[fa] = round_yuan(cur_bal[fa] + amt)
                    if ta in cur_bal:
                        rec["to_after"] = round_yuan(cur_bal[ta])
                        cur_bal[ta] = round_yuan(cur_bal[ta] - amt)
                else:
                    acc = rec.get("account") or ""
                    if acc in cur_bal and kind in ("earn", "spend", "adjust", "debt_settle"):
                        rec["after_balance"] = round_yuan(cur_bal[acc])
                        if kind == "earn" or kind == "adjust":
                            cur_bal[acc] = round_yuan(cur_bal[acc] - amt)
                        elif kind == "spend":
                            cur_bal[acc] = round_yuan(cur_bal[acc] + amt)
                        elif kind == "debt_settle" and amt > 0:
                            debt = debts_map.get(str(rec.get("debt_id") or ""))
                            direction = normalize_direction(rec.get("direction")) or (debt or {}).get("direction") or "payable"
                            cur_bal[acc] = round_yuan(cur_bal[acc] + amt if direction == "payable" else cur_bal[acc] - amt)

            debts_list = sorted(
                debts_map.values(),
                key=lambda d: (d.get("status") != "open", str(d.get("created") or ""), str(d.get("id") or "")),
            )
            return {
                "accounts": accs,
                "debts": debts_list,
                "records": recs,
                "total_cash": round_yuan(sum(accs[k]["balance"] for k in ACCOUNT_ORDER if k in accs)),
                "home_and_girls_cash": round_yuan(sum(accs[k]["balance"] for k in ("tin_box", "napcat2", "napcat") if k in accs)),
            }

    def archive_settled(self, now: datetime.datetime, world) -> int:
        """把已经彻底完结（end <= now - SETTLE_DELAY_MINUTES 且未中断）的世界线 money 操作固化进 money.json。"""
        with self._lock:
            if world is None:
                return 0
            self._resolve_world(world)
            flat = now.replace(tzinfo=None)
            cutoff = flat - datetime.timedelta(minutes=SETTLE_DELAY_MINUTES)
            data = self._ensure_shape()
            settled = data.setdefault("settled_wl", {})
            void_wl = data.get("void_wl") or {}
            pending = self._collect_pending_wl(flat, world, settled, void_wl)
            to_archive = []
            for wl_key, op_dt, op, meta in pending:
                entry = meta["entry"]
                end_dt = parse_time(entry.get("end"))
                if end_dt is None or end_dt > cutoff:
                    continue
                to_archive.append((wl_key, op_dt, op, meta))
            if not to_archive:
                return 0

            accs = data["accounts"]
            debts_map = {str(d["id"]): d for d in data["debts"] if isinstance(d, dict) and d.get("id")}
            count = 0
            for wl_key, op_dt, op, meta in to_archive:
                rec = self._apply_op_on_state(
                    accs,
                    debts_map,
                    op,
                    at_str=fmt_time(op_dt),
                    source="step",
                    wl_key=wl_key,
                    wl_ref=meta["wl"],
                    who=meta["who"],
                    allow_negative=True,
                )
                settled[wl_key] = fmt_time(flat)
                if rec is not None:
                    data["records"].append(rec)
                    count += 1
            data["debts"] = list(debts_map.values())
            if len(data["records"]) > MAX_RECORDS:
                data["records"] = data["records"][-MAX_RECORDS:]
            # 清理过旧的 settled_wl/void_wl 标记
            keep_cut = (flat.date() - datetime.timedelta(days=VOID_KEEP_DAYS)).isoformat()
            data["settled_wl"] = {k: v for k, v in settled.items() if str(k)[:10] >= keep_cut}
            data["void_wl"] = {k: v for k, v in void_wl.items() if str(k)[:10] >= keep_cut}
            self._save()
            if count:
                logger.info("[super_enhance/money] 已固化归档 %d 笔世界线账目", count)
            return count

    # ---------- 记录员 / 网页写盘接口 ----------

    def apply_record_ops(
        self,
        ops: list,
        *,
        now: datetime.datetime,
        default_at: str = "",
        source: str = "recorder",
        world=None,
        has_master: bool = True,
    ) -> list[dict]:
        """落盘来自聊天记录员的一组钱变动，单条不合规只跳过那条，绝不阻断主流程。"""
        if not isinstance(ops, list) or not ops:
            return []
        with self._lock:
            eff_world = self._resolve_world(world)
            self.archive_settled(now, eff_world)
            snap = self.snapshot(now, eff_world)
            sim_bal = {k: round_yuan(v["balance"]) for k, v in snap["accounts"].items()}
            snap_debts = {str(d["id"]): d for d in snap.get("debts") or [] if isinstance(d, dict) and d.get("id")}
            data = self._ensure_shape()
            accs = data["accounts"]
            debts_map = {str(d["id"]): d for d in data["debts"] if isinstance(d, dict) and d.get("id")}
            written: list[dict] = []

            for raw in ops:
                if not isinstance(raw, dict):
                    continue
                kind = normalize_kind(raw.get("kind"))
                if kind not in ("earn", "spend", "transfer", "debt_add", "debt_settle"):
                    continue
                raw_amt = raw.get("amount")
                if raw_amt is None:
                    amt = 0.0
                else:
                    if isinstance(raw_amt, bool):
                        continue
                    try:
                        f_amt = float(raw_amt)
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(f_amt):
                        continue
                    amt = round(f_amt, 2)
                at_str = clean_text(raw.get("at"), 16) or default_at or fmt_time(now)
                if parse_time(at_str) is None:
                    at_str = default_at or fmt_time(now)

                # 对话里机器人没提现钱足够或透支时保护账户不被扣穿；先去重再用合并视图 sim_bal 检查再套用
                op = dict(raw)
                op["kind"] = kind
                op["amount"] = amt
                acc = normalize_account(op.get("account"))
                from_acc = normalize_account(op.get("from_account"))
                to_acc = normalize_account(op.get("to_account"))
                # 职责划分：记录员(recorder)只记涉及主人钱包(master)的当场动钱，以及主人亲口发话指挥的账户间挪钱(transfer，如把兜里钱放铁盒)；
                # 女孩自己买菜买柴、卖废品、挂账结账一律归世界线(step)记账；且下方有 2 小时跨通道去重防重复转账
                if source == "recorder":
                    involves_master = (acc == "master" or from_acc == "master" or to_acc == "master")
                    allow_master_transfer = (kind == "transfer" and bool(has_master))
                    if not involves_master and not allow_master_transfer:
                        logger.info(
                            "[super_enhance/money] 跳过记录员非主人钱包账目（归世界线记账）：%s %s元 %s",
                            KIND_LABELS.get(kind, kind), fmt_yuan(amt), op.get("what") or "",
                        )
                        continue

                # 跨通道去重前移至 sim_bal 修改之前：重复账目直接跳过，绝不扣改 sim_bal 误杀后续合法交易
                op_dt_val = parse_time(at_str) or now.replace(tzinfo=None)
                dedup_pool = list(snap.get("records") or [])[-40:] + written
                dup = False
                for r in dedup_pool:
                    if r.get("kind") != kind or round_yuan(r.get("amount")) != amt:
                        continue
                    if (r.get("account") or "") != acc or (r.get("from_account") or "") != from_acc or (r.get("to_account") or "") != to_acc:
                        continue
                    r_dt = parse_time(r.get("at"))
                    if r_dt is not None and abs((op_dt_val - r_dt).total_seconds()) <= 7200:
                        dup = True
                        break
                    if str(r.get("at") or "")[:16] == at_str[:16]:
                        dup = True
                        break
                if dup:
                    logger.info(
                        "[super_enhance/money] [%s] 跳过重复账目（近 2 小时内已有同笔记录）：%s %s元 %s",
                        source, KIND_LABELS.get(kind, kind), fmt_yuan(amt), op.get("what") or "",
                    )
                    continue

                if kind == "spend":
                    if acc not in accs or amt <= 0:
                        continue
                    if sim_bal[acc] + 1e-6 < amt:
                        logger.info("[super_enhance/money] 跳过记录员超额支出：%s 余额 %.2f < %.2f (%s)", acc, sim_bal[acc], amt, op.get("what"))
                        continue
                    sim_bal[acc] = round_yuan(sim_bal[acc] - amt)
                elif kind == "transfer":
                    if from_acc not in accs or to_acc not in accs or from_acc == to_acc or amt <= 0:
                        continue
                    if sim_bal[from_acc] + 1e-6 < amt:
                        logger.info("[super_enhance/money] 跳过记录员超额挪用：%s 余额 %.2f < %.2f (%s)", from_acc, sim_bal[from_acc], amt, op.get("what"))
                        continue
                    sim_bal[from_acc] = round_yuan(sim_bal[from_acc] - amt)
                    sim_bal[to_acc] = round_yuan(sim_bal[to_acc] + amt)
                elif kind == "earn":
                    if acc not in accs or amt <= 0:
                        continue
                    sim_bal[acc] = round_yuan(sim_bal[acc] + amt)
                elif kind == "debt_settle":
                    if amt <= 0:
                        continue
                    did = str(op.get("debt_id") or "").strip()
                    tgt = debts_map.get(did) or snap_debts.get(did)
                    if tgt is None:
                        continue
                    direction = (tgt or {}).get("direction") or normalize_direction(op.get("direction")) or "payable"
                    if acc in accs and direction == "payable" and sim_bal[acc] + 1e-6 < amt:
                        logger.info("[super_enhance/money] 跳过记录员超额还账：%s 余额 %.2f < %.2f", acc, sim_bal[acc], amt)
                        continue
                    # 先就地固化未归档世界线欠账 (wl_*)，确认成功后再扣改 sim_bal，防止固化失败泄漏余额扣减
                    if did not in debts_map and did.startswith("wl_"):
                        mat = self._materialize_wl_debt(data, did, now=now, world=eff_world)
                        if mat is None:
                            continue
                        debts_map[did] = mat
                    if acc in accs:
                        if direction == "payable":
                            sim_bal[acc] = round_yuan(sim_bal[acc] - amt)
                        else:
                            sim_bal[acc] = round_yuan(sim_bal[acc] + amt)

                rec = self._apply_op_on_state(
                    accs, debts_map, op, at_str=at_str, source=source, who=str(op.get("who") or ""), allow_negative=True
                )
                if rec is not None:
                    data["records"].append(rec)
                    written.append(rec)
                    logger.info(
                        "[super_enhance/money] [%s] 记一笔 %s：%s元 %s",
                        source, KIND_LABELS.get(kind, kind), fmt_yuan(amt), rec.get("what") or "",
                    )

            if written:
                data["debts"] = list(debts_map.values())
                if len(data["records"]) > MAX_RECORDS:
                    data["records"] = data["records"][-MAX_RECORDS:]
                self._save()
            return written

    def set_balance_to(self, account: str, target_amount: float, *, now: datetime.datetime, note: str = "", world=None) -> dict:
        """网页手改某账户余额：精确校准到 target_amount 并记一条调账流水。"""
        with self._lock:
            acc = normalize_account(account)
            if not acc:
                raise ValueError("未知账户：%s" % account)
            target = _strict_yuan(target_amount, "目标余额")
            if target < 0:
                raise ValueError("账户余额不能为负数")
            if world is not None:
                self.archive_settled(now, world)
            eff_world = self._resolve_world(world)
            snap = self.snapshot(now, eff_world)
            cur = round_yuan(snap["accounts"][acc]["balance"])
            delta = round_yuan(target - cur)
            data = self._ensure_shape()
            at_str = fmt_time(now)
            rec = None
            if abs(delta) >= 1e-6:
                data["accounts"][acc]["balance"] = round_yuan(data["accounts"][acc]["balance"] + delta)
                rec = {
                    "id": self._next_rec_id(),
                    "at": at_str,
                    "kind": "adjust",
                    "account": acc,
                    "from_account": "",
                    "to_account": "",
                    "amount": delta,
                    "who": "master",
                    "counterparty": "",
                    "what": clean_text(note, 120) or ("网页把%s调成 %s 元（原 %s 元）" % (data["accounts"][acc]["label"], fmt_yuan(target), fmt_yuan(cur))),
                    "items": [],
                    "debt_id": "",
                    "source": "web",
                    "wl": "",
                    "wl_key": "",
                }
                data["records"].append(rec)
                if len(data["records"]) > MAX_RECORDS:
                    data["records"] = data["records"][-MAX_RECORDS:]
                self._save()
            return {"account": acc, "balance": target, "delta": delta, "record": rec}

    def edit_account_meta(self, account: str, *, label: str | None = None, place: str | None = None, note: str | None = None) -> dict:
        with self._lock:
            acc = normalize_account(account)
            data = self._ensure_shape()
            if acc not in data["accounts"]:
                raise ValueError("找不到账户：%s" % account)
            meta = data["accounts"][acc]
            if label is not None and clean_text(label, 20):
                meta["label"] = clean_text(label, 20)
            if place is not None:
                meta["place"] = clean_text(place, 60)
            if note is not None:
                meta["note"] = clean_text(note, 160)
            self._save()
            return meta

    def add_web_record(self, payload: dict, *, now: datetime.datetime) -> dict:
        with self._lock:
            data = self._ensure_shape()
            accs = data["accounts"]
            debts_map = {str(d["id"]): d for d in data["debts"] if isinstance(d, dict) and d.get("id")}
            kind = normalize_kind(payload.get("kind"))
            if kind not in ("earn", "spend", "transfer", "adjust"):
                raise ValueError("不支持的流水类型：%s" % payload.get("kind"))
            raw_amt = payload.get("amount")
            if raw_amt is None or str(raw_amt).strip() == "":
                raise ValueError("金额不能为空")
            amt = _strict_yuan(raw_amt, "金额")
            if kind != "adjust" and amt <= 0:
                raise ValueError("金额必须大于 0")
            at_str = clean_text(payload.get("at"), 16) or fmt_time(now)
            if parse_time(at_str) is None:
                raise ValueError("时间格式要写成 YYYY-MM-DD HH:MM")
            op = {
                "kind": kind,
                "account": normalize_account(payload.get("account")),
                "from_account": normalize_account(payload.get("from_account")),
                "to_account": normalize_account(payload.get("to_account")),
                "amount": amt,
                "who": clean_text(payload.get("who"), 20) or "master",
                "counterparty": clean_text(payload.get("counterparty"), 40),
                "what": clean_text(payload.get("what"), 120),
            }
            rec = self._apply_op_on_state(accs, debts_map, op, at_str=at_str, source="web", allow_negative=True)
            if rec is None:
                raise ValueError("账户或字段填得不对")
            data["records"].append(rec)
            if len(data["records"]) > MAX_RECORDS:
                data["records"] = data["records"][-MAX_RECORDS:]
            self._save()
            return rec

    def delete_record(self, rec_id: str, *, revert_balance: bool = True, now: datetime.datetime | None = None, world=None) -> bool:
        """删除一条流水：支持删固化流水（按需回退余额）或作废尚未固化的真实世界线流水。"""
        with self._lock:
            rid = str(rec_id or "").strip()
            if not rid:
                return False
            eff_world = self._resolve_world(world)
            data = self._ensure_shape()
            accs = data["accounts"]
            for idx, rec in enumerate(data["records"]):
                if str(rec.get("id") or "") == rid:
                    if revert_balance:
                        self._revert_rec_balance(accs, rec, data.get("debts") or [])
                    data["records"].pop(idx)
                    self._save()
                    return True
            # 检查是否尚未固化的世界线流水（id == wl_key）：先验证该 wl_key 确实存在于世界线才写入 void_wl
            if "/" in rid and rid not in data["void_wl"]:
                if self._find_wl_op(rid, world=eff_world, now=None) is None:
                    return False
                data["void_wl"][rid] = fmt_time(now or timeutil.now())
                self._save()
                return True
            return False

    @staticmethod
    def _revert_rec_balance(accs: dict, rec: dict, debts: list | None = None) -> None:
        kind = rec.get("kind")
        amt = round_yuan(rec.get("amount") or 0.0)
        acc = rec.get("account") or ""
        fa = rec.get("from_account") or ""
        ta = rec.get("to_account") or ""
        if kind == "earn" and acc in accs:
            accs[acc]["balance"] = round_yuan(accs[acc]["balance"] - amt)
        elif kind == "spend" and acc in accs:
            accs[acc]["balance"] = round_yuan(accs[acc]["balance"] + amt)
        elif kind == "transfer" and fa in accs and ta in accs:
            accs[fa]["balance"] = round_yuan(accs[fa]["balance"] + amt)
            accs[ta]["balance"] = round_yuan(accs[ta]["balance"] - amt)
        elif kind == "adjust" and acc in accs:
            accs[acc]["balance"] = round_yuan(accs[acc]["balance"] - amt)
        elif kind == "debt_settle":
            did = str(rec.get("debt_id") or "").strip()
            target = next((d for d in (debts or []) if str(d.get("id") or "") == did), None)
            direction = normalize_direction(rec.get("direction")) or (target or {}).get("direction") or "payable"
            if acc in accs and amt > 0:
                if direction == "payable":
                    accs[acc]["balance"] = round_yuan(accs[acc]["balance"] + amt)
                else:
                    accs[acc]["balance"] = round_yuan(accs[acc]["balance"] - amt)
            if target is not None and amt > 0:
                cap = round_yuan(target.get("amount") or amt)
                rem = round_yuan(target.get("remaining") or 0.0)
                target["remaining"] = min(cap, round_yuan(rem + amt))
                target["status"] = "open"

    def add_debt(self, payload: dict, *, now: datetime.datetime) -> dict:
        with self._lock:
            data = self._ensure_shape()
            direction = normalize_direction(payload.get("direction")) or "payable"
            cp = clean_text(payload.get("counterparty"), 40)
            if not cp:
                raise ValueError("要写清楚对方是谁（如杂货铺、东港福记旧物行）")
            raw_amt = payload.get("amount")
            amt = _strict_yuan(raw_amt if raw_amt not in (None, "") else 0.0, "金额")
            if amt < 0:
                raise ValueError("金额不能为负")
            at_str = fmt_time(now)
            debt = {
                "id": self._next_debt_id(),
                "direction": direction,
                "counterparty": cp,
                "amount": amt,
                "remaining": amt,
                "about": clean_text(payload.get("about"), 120),
                "status": "open",
                "created": at_str,
                "updated": at_str,
                "source": "web",
            }
            data["debts"].append(debt)
            self._save()
            return debt

    def update_debt(self, debt_id: str, payload: dict, *, now: datetime.datetime, world=None) -> dict:
        with self._lock:
            new_amount = None
            if payload.get("amount") is not None:
                new_amount = _strict_yuan(payload.get("amount"), "金额")
                if new_amount < 0:
                    raise ValueError("金额不能为负")
            new_remaining = None
            if payload.get("remaining") is not None:
                new_remaining = _strict_yuan(payload.get("remaining"), "剩余金额")
                if new_remaining < 0:
                    raise ValueError("剩余金额不能为负")
            eff_world = self._resolve_world(world)
            data = self._ensure_shape()
            did = str(debt_id or "").strip()
            target = next((d for d in data["debts"] if str(d.get("id") or "") == did), None)
            if target is None and did.startswith("wl_"):
                target = self._materialize_wl_debt(data, did, now=now, world=eff_world)
            if target is None:
                raise ValueError("找不到账目 #%s" % did)
            if payload.get("direction"):
                nd = normalize_direction(payload.get("direction"))
                if nd:
                    target["direction"] = nd
            if payload.get("counterparty") is not None:
                cp = clean_text(payload.get("counterparty"), 40)
                if cp:
                    target["counterparty"] = cp
            if new_amount is not None:
                target["amount"] = new_amount
            if new_remaining is not None:
                target["remaining"] = new_remaining
            if payload.get("about") is not None:
                target["about"] = clean_text(payload.get("about"), 120)
            if payload.get("status") in ("open", "settled"):
                target["status"] = payload["status"]
                if target["status"] == "settled":
                    target["remaining"] = 0.0
            target["updated"] = fmt_time(now)
            self._save()
            return target

    def settle_debt(
        self,
        debt_id: str,
        *,
        account: str = "",
        amount: float | None = None,
        now: datetime.datetime,
        what: str = "",
        world=None,
    ) -> dict:
        with self._lock:
            explicit_amt = None
            if amount is not None:
                explicit_amt = _strict_yuan(amount, "结清金额")
                if explicit_amt <= 0:
                    raise ValueError("结清金额必须大于 0")
            eff_world = self._resolve_world(world)
            data = self._ensure_shape()
            did = str(debt_id or "").strip()
            target = next((d for d in data["debts"] if str(d.get("id") or "") == did), None)
            if target is None and did.startswith("wl_"):
                target = self._materialize_wl_debt(data, did, now=now, world=eff_world)
            if target is None:
                raise ValueError("找不到账目 #%s" % did)
            amt = explicit_amt if explicit_amt is not None else round_yuan(target.get("remaining", target.get("amount", 0.0)))
            acc = normalize_account(account) if account else ""
            op = {
                "kind": "debt_settle",
                "debt_id": did,
                "account": acc,
                "amount": amt,
                "what": clean_text(what, 120) or ("%s #%s（%s）" % (
                    "还清欠账" if target.get("direction") == "payable" else "收回欠款",
                    did, target.get("counterparty") or "",
                )),
            }
            accs = data["accounts"]
            debts_map = {str(d["id"]): d for d in data["debts"] if isinstance(d, dict) and d.get("id")}
            rec = self._apply_op_on_state(accs, debts_map, op, at_str=fmt_time(now), source="web", who="master", allow_negative=True)
            if rec is None:
                raise ValueError("结清账目失败，请检查金额与状态")
            if acc or amt > 0:
                data["records"].append(rec)
                if len(data["records"]) > MAX_RECORDS:
                    data["records"] = data["records"][-MAX_RECORDS:]
            data["debts"] = list(debts_map.values())
            self._save()
            return {"debt": target, "record": rec}

    def delete_debt(self, debt_id: str, *, now: datetime.datetime | None = None, world=None) -> bool:
        with self._lock:
            eff_world = self._resolve_world(world)
            data = self._ensure_shape()
            did = str(debt_id or "").strip()
            kept = [d for d in data["debts"] if str(d.get("id") or "") != did]
            if len(kept) != len(data["debts"]):
                data["debts"] = kept
                self._save()
                return True
            if did.startswith("wl_"):
                wl_key = did[3:]
                if wl_key and wl_key not in data["void_wl"] and self._find_wl_op(wl_key, world=eff_world, now=None) is not None:
                    data["void_wl"][wl_key] = fmt_time(now or timeutil.now())
                    self._save()
                    return True
            return False

    # ---------- 提示词渲染 ----------

    @staticmethod
    def chat_static_rules() -> str:
        return (
            "船屋里的钱（人民币，分开保管）：\n"
            "- 大头家底在主人自己钱包里（`master`），未经主人亲口答应或同行付现，你不能擅自动用主人的钱；\n"
            "- 日常买菜、买柴炭煤油、小额开支从船舱矮桌抽屉里的「装钱的铁盒（p157）」取用：出门采买要先从盒里拿现钱揣在自己身上，回来要把没花完的零钱和找零放回铁盒；人在外头时手头没带够钱，不能凭空隔空掏出铁盒里的钱（只能少买、回家取，或跟熟摊主记账赊欠）；\n"
            "- 挣到 50 元以上的较大进账或正式打捞费，习惯揣回来交给当家主人收管，十块二十块的零碎外快可进公用铁盒或留作小零花；\n"
            "- " + SEASIDE_PRICE_GUIDE.replace("\n", "\n  ")
        )

    def chat_now_text(self, now: datetime.datetime, world, bot: str, label_fn=str) -> str:
        """聊天/主动消息动态区注入：仅显示她自己口袋与家里公用铁盒余额，及与这两处相关的账目。"""
        snap = self.snapshot(now, world)
        accs = snap["accounts"]
        today = now.replace(tzinfo=None).date().isoformat()
        me_acc = accs.get(bot)
        lines = ["此刻手头账面与船屋钱匣（人民币，真实金额）："]
        if me_acc is not None:
            lines.append("- 你自己身上（%s）：%s 元（%s）" % (me_acc["label"], fmt_yuan(me_acc["balance"]), me_acc.get("place") or ""))
        tin = accs.get("tin_box")
        if tin is not None:
            lines.append("- 家里公用钱盒（%s）：%s 元（%s）" % (tin["label"], fmt_yuan(tin["balance"]), tin.get("place") or ""))
        open_debts = [d for d in snap["debts"] if d.get("status") == "open"]
        if open_debts:
            d_bits = []
            for d in open_debts[:5]:
                tag = "欠着对方" if d.get("direction") == "payable" else "待结回款"
                rem = d.get("remaining") if d.get("remaining") is not None else d.get("amount")
                amt_txt = ("%s 元" % fmt_yuan(rem)) if float(rem or 0) > 0 else "待估"
                d_bits.append("%s %s（%s，%s%s）" % (d.get("id"), d.get("counterparty"), tag, amt_txt,
                                                     "，" + d.get("about") if d.get("about") else ""))
            lines.append("- 账本未结：%s" % "；".join(d_bits))
        visible_accs = {bot, "tin_box"}
        recs = [
            r for r in snap["records"]
            if r.get("kind") != "adjust"
            and ({r.get("account"), r.get("from_account"), r.get("to_account")} & visible_accs or r.get("who") == bot)
        ]
        today_recs = [r for r in recs if str(r.get("at") or "")[:10] == today]
        recent = today_recs[-3:] if today_recs else recs[-2:]
        if recent:
            r_bits = []
            for r in recent:
                r_bits.append("%s %s %s元（%s）" % (
                    str(r.get("at") or "")[5:16], KIND_LABELS.get(r.get("kind"), ""),
                    fmt_yuan(abs(float(r.get("amount") or 0))), r.get("what") or r.get("counterparty") or "",
                ))
            lines.append("- 最近几笔进出：%s" % "；".join(r_bits))
        lines.append("（日常说话不用刻意报余额数字，遇到要买东西、省钱盘算、发愁或主人问起时照真实余额说，绝不凭空多钱）")
        return "\n".join(lines)

    def _account_lines(self, snap: dict) -> list[str]:
        accs = snap["accounts"]
        out = []
        for key in ACCOUNT_ORDER:
            if key not in accs:
                continue
            a = accs[key]
            out.append("- `%s`（%s）：**%s 元**｜放在：%s｜%s" % (
                key, a.get("label") or key, fmt_yuan(a.get("balance")), a.get("place") or "", a.get("note") or "",
            ))
        return out

    def step_block(self, moment: datetime.datetime, world) -> str:
        """给幕间补写（step.py）看的家底账面。"""
        snap = self.snapshot(moment, world)
        lines = ["此刻钱匣与个人账面（人民币；写入段里 `" + 'money' + "` 必填，无变动填 `[]`）："]
        lines.extend(self._account_lines(snap))
        open_debts = [d for d in snap["debts"] if d.get("status") == "open"]
        if open_debts:
            lines.append("未结欠账 / 待收（还清/收款写 `debt_settle` 并填 `debt_id`）：")
            for d in open_debts:
                tag = "欠外人（payable）" if d.get("direction") == "payable" else "待收（receivable）"
                rem = d.get("remaining") if d.get("remaining") is not None else d.get("amount")
                lines.append("  - `%s` [%s] 对方：%s｜余 %s 元｜%s" % (
                    d.get("id"), tag, d.get("counterparty"), fmt_yuan(rem), d.get("about") or "",
                ))
        recent = [r for r in snap["records"][-5:] if r.get("kind") != "adjust"]
        if recent:
            lines.append("最近几笔真实进出（延续现状用，别重复入账）：")
            for r in recent:
                lines.append("  - %s [%s] %s元｜%s" % (
                    r.get("at"), KIND_LABELS.get(r.get("kind"), ""), fmt_yuan(abs(float(r.get("amount") or 0))),
                    r.get("what") or r.get("counterparty") or "",
                ))
        return "\n".join(lines)

    def recorder_block(self, now: datetime.datetime, world) -> str:
        """给记录员（recorder.py）看的账本与规则提示。"""
        snap = self.snapshot(now, world)
        lines = ["【船屋此刻账面（人民币，account 填下列代号）】"]
        lines.extend(self._account_lines(snap))
        open_debts = [d for d in snap["debts"] if d.get("status") == "open"]
        if open_debts:
            lines.append("未结欠账/待收编号：")
            for d in open_debts:
                tag = "欠对方" if d.get("direction") == "payable" else "待收"
                rem = d.get("remaining") if d.get("remaining") is not None else d.get("amount")
                lines.append("  - %s：%s（%s，%s 元，%s）" % (
                    d.get("id"), d.get("counterparty"), tag, fmt_yuan(rem), d.get("about") or "",
                ))
        return "\n".join(lines)


def normalize_step_money_list(raws) -> list[dict]:
    """把补写通过校验后的 segment['money'] 规整成干净的落盘字典列表。"""
    if not isinstance(raws, list):
        return []
    out = []
    for raw in raws:
        if not isinstance(raw, dict):
            continue
        kind = normalize_kind(raw.get("kind"))
        if kind not in ("earn", "spend", "transfer", "debt_add", "debt_settle"):
            continue
        item = {
            "kind": kind,
            "account": normalize_account(raw.get("account")),
            "from_account": normalize_account(raw.get("from_account")),
            "to_account": normalize_account(raw.get("to_account")),
            "amount": round_yuan(raw.get("amount") or 0.0),
            "reason": str(raw.get("reason") or "").strip().lower(),
            "direction": normalize_direction(raw.get("direction")) or "",
            "debt_id": str(raw.get("debt_id") or "").strip(),
            "counterparty": clean_text(raw.get("counterparty"), 40),
            "what": clean_text(raw.get("what"), 120),
            "items": [str(x).strip() for x in (raw.get("items") or []) if str(x).strip()],
        }
        if raw.get("step") is not None:
            item["step"] = raw.get("step")
        out.append(item)
    return out


def validate_step_segments_money(
    book: MoneyBook,
    moment: datetime.datetime,
    world,
    segments_by_bot: dict[str, list[dict]],
    *,
    seaside: bool,
    label_fn=str,
    valid_intents: set[str] | None = None,
    master_replied: bool = False,
) -> list[str]:
    """跨人跨段纯结构化校验与模拟余额追踪（绝不扫描正文猜金额）。"""
    errors: list[str] = []
    snap = book.snapshot(moment, world)
    sim_bal = {k: round_yuan(v.get("balance") or 0.0) for k, v in snap["accounts"].items()}
    sim_debts = {str(d["id"]): copy.deepcopy(d) for d in snap["debts"] if d.get("status") == "open"}
    acc_labels = {k: v.get("label") or k for k, v in snap["accounts"].items()}
    # 近期已记收支/赊账（用于防跨批衔接重复记同一笔账）
    recent_ops: list[tuple[str, str, float, str, datetime.datetime, str]] = []
    for rec in snap.get("records") or []:
        if not isinstance(rec, dict):
            continue
        r_kind = normalize_kind(rec.get("kind"))
        if r_kind not in ("earn", "spend", "debt_add"):
            continue
        r_dt = parse_time(rec.get("at"))
        if r_dt is None:
            continue
        recent_ops.append((
            str(rec.get("who") or "").strip(),
            r_kind,
            round_yuan(rec.get("amount") or 0.0),
            clean_text(rec.get("counterparty"), 40),
            r_dt,
            clean_text(rec.get("what"), 60),
        ))

    # 按时间顺序汇总所有人的做事段来模拟共用铁盒与个人口袋的真实先后变动
    timeline: list[tuple[str, str, int, dict]] = []
    for bot, rows in segments_by_bot.items():
        if not isinstance(rows, list):
            continue
        for idx, raw in enumerate(rows, 1):
            if not isinstance(raw, dict):
                continue
            st_code = "idle" if str(raw.get("state") or "") == "闲着" else ("sleep" if str(raw.get("state") or "") == "睡觉" else "do")
            who_lbl = label_fn(bot) or bot
            tag = f"{who_lbl} 第{idx}段（{raw.get('start') or '?'}）"
            if st_code != "do":
                if raw.get("money"):
                    errors.append(f"{tag}：闲着或睡觉段 money 必须填 []")
                continue
            if "money" not in raw or not isinstance(raw.get("money"), list):
                errors.append(f"{tag}：做事段必须显式填 money 数组（没有动钱填 []）")
                continue
            timeline.append((str(raw.get("start") or ""), bot, idx, raw))

    timeline.sort(key=lambda x: (x[0], x[1], x[2]))

    for _start_s, bot, idx, raw in timeline:
        who_lbl = label_fn(bot) or bot
        tag = f"{who_lbl} 第{idx}段（{raw.get('start') or '?'}）"
        at_home = raw.get("at_home")
        with_list = [str(x) for x in (raw.get("with") or [])]
        intent_id = str(raw.get("intent") or "").strip()
        has_master_intent = bool(intent_id and valid_intents and intent_id in valid_intents)
        ops = raw.get("money") or []

        # 检查是否发生真实购物 / 交付库存物品（纯结构化，只看 what 与 id/new）
        bought_items = []
        new_inbound_count = 0
        used_existing_ids: set[str] = set()
        for eff in (raw.get("things") or []):
            if not isinstance(eff, dict):
                continue
            what_norm = norm_step(eff.get("what"), CAT_FOOD)
            if eff.get("new"):
                new_inbound_count += 1
                if what_norm == "买回来":
                    bought_items.append(str(eff.get("name") or eff.get("new")))
            elif eff.get("id") and what_norm in ("用完了", "用掉一部分", "送人了"):
                used_existing_ids.add(str(eff.get("id")).strip())
        for eff in (raw.get("items") or []):
            if not isinstance(eff, dict):
                continue
            what_norm = norm_step(eff.get("what"), CAT_ITEM)
            if eff.get("new"):
                new_inbound_count += 1
                if what_norm == "买回来":
                    bought_items.append(str(eff.get("name") or eff.get("new")))
            elif eff.get("id") and what_norm in ("用完了", "用了", "送人了"):
                used_existing_ids.add(str(eff.get("id")).strip())

        has_payment_or_barter = bool(used_existing_ids)
        goods_spend_notes: list[str] = []

        for m_i, op in enumerate(ops, 1):
            if not isinstance(op, dict):
                errors.append(f"{tag} money[{m_i}]：必须是对象")
                continue
            kind = normalize_kind(op.get("kind"))
            if kind not in ("earn", "spend", "transfer", "debt_add", "debt_settle"):
                errors.append(f"{tag} money[{m_i}]：kind={op.get('kind')!r} 非法（须填 earn/spend/transfer/debt_add/debt_settle）")
                continue
            raw_amt_val = op.get("amount")
            if isinstance(raw_amt_val, bool):
                errors.append(f"{tag} money[{m_i}]：amount 必须为有效有限数字")
                continue
            try:
                raw_f = float(raw_amt_val if raw_amt_val is not None else -1)
            except Exception:
                raw_f = -1.0
            if not math.isfinite(raw_f):
                errors.append(f"{tag} money[{m_i}]：amount 必须为有效有限数字")
                continue
            amt = round_yuan(raw_f)
            if kind != "debt_add" and amt <= 0:
                errors.append(f"{tag} money[{m_i}]：amount 必须为大于 0 的数字")
                continue
            if kind == "debt_add" and amt < 0:
                errors.append(f"{tag} money[{m_i}]：debt_add 的 amount 不能小于 0")
                continue
            what_s = clean_text(op.get("what"), 120)
            if not what_s:
                errors.append(f"{tag} money[{m_i}]：what 必须一句话写清这笔钱干了什么/打哪来的")
                continue
            op_cp = clean_text(op.get("counterparty"), 40)
            seg_dt = parse_time(_start_s)
            if kind in ("earn", "spend", "debt_add") and op_cp and seg_dt is not None:
                dup_hit = next(
                    (
                        (r_dt, r_what)
                        for r_who, r_kind, r_amt, r_cp, r_dt, r_what in recent_ops
                        if r_who == bot and r_kind == kind and abs(r_amt - amt) <= 1e-6 and r_cp == op_cp
                        and abs((seg_dt - r_dt).total_seconds()) <= 45 * 60
                    ),
                    None,
                )
                if dup_hit is not None:
                    errors.append(
                        f"{tag} money[{m_i}]：近期已记过一笔同对手方「{op_cp}」同金额（{fmt_yuan(amt)} 元）的 {kind}"
                        f"（{fmt_time(dup_hit[0])[11:16]} {dup_hit[1]}），严禁跨段重复记账！"
                    )
                    continue
                if kind == "debt_add":
                    dup_open = next(
                        (
                            d for d in sim_debts.values()
                            if d.get("direction") == normalize_direction(op.get("direction"))
                            and clean_text(d.get("counterparty"), 40) == op_cp
                            and abs(round_yuan(d.get("amount") or 0.0) - amt) <= 1e-6
                            and parse_time(d.get("created")) is not None
                            and abs((seg_dt - parse_time(d.get("created"))).total_seconds()) <= 45 * 60
                        ),
                        None,
                    )
                    if dup_open is not None:
                        errors.append(
                            f"{tag} money[{m_i}]：未结欠账中已有同对手方「{op_cp}」同金额（{fmt_yuan(amt)} 元）的欠账 {dup_open.get('id')}，严禁重复赊账！"
                        )
                        continue

            if seaside and bot == "napcat" and "napcat2" not in with_list and "master" not in with_list:
                if kind in ("earn", "spend", "debt_add", "debt_settle"):
                    errors.append(f"{tag} money[{m_i}]：丛雨外人看不见，不能独自在镇上做交易/收款/花钱（kind={kind}）")
                    continue

            if kind == "spend":
                acc = normalize_account(op.get("account"))
                if acc not in sim_bal:
                    errors.append(f"{tag} money[{m_i}]：account={op.get('account')!r} 无效（须填 tin_box/napcat2/napcat）")
                    continue
                if acc == "master":
                    errors.append(f"{tag} money[{m_i}]：大头归主人管，幕间补写严禁直接从 master 钱包扣钱（只能花公用铁盒/个人口袋，或由主人在聊天里决定拨款）")
                    continue
                if seaside and at_home is False and acc == "tin_box":
                    errors.append(
                        f"{tag} money[{m_i}]：人已不在船屋（at_home=false），船舱抽屉里的公用铁盒(tin_box)没带在身上，"
                        f"不能隔空掏铁盒的钱！请先在出门前的在船屋段加一笔 transfer(tin_box -> napcat2) 取现，或花身上已带的 napcat2 / 赊账(debt_add)"
                    )
                    continue
                if sim_bal[acc] + 1e-6 < amt:
                    errors.append(
                        f"{tag} money[{m_i}]：{acc_labels[acc]}({acc}) 仅余 {fmt_yuan(sim_bal[acc])} 元，不够花 {fmt_yuan(amt)} 元；"
                        f"请先从铁盒拿钱(transfer)、减少花费、或记赊账(debt_add)"
                    )
                    continue
                sim_bal[acc] = round_yuan(sim_bal[acc] - amt)
                has_payment_or_barter = True
                if op_cp and seg_dt is not None:
                    recent_ops.append((bot, kind, amt, op_cp, seg_dt, what_s[:60]))
                _SERVICE_SPEND_HINTS = (
                    "泡澡", "澡堂", "洗澡", "修理费", "修船费", "修补费", "维修费", "代修", "请人修",
                    "缝补费", "裁缝费", "运费", "船费", "渡费", "工钱", "工费",
                    "邮费", "寄信", "寄包裹", "寄件", "快递", "门票", "理发", "诊费", "看病",
                    "茶水费", "茶位", "租金", "租借费", "租船费", "房租", "押金",
                    "打煤油", "灌煤油", "散装煤油", "一壶煤油", "小壶煤油", "打水", "淡水", "小费", "打赏", "捐款", "捐钱",
                )
                _GOODS_SPEND_HINTS = ("买", "购", "称", "置办", "添置", "挑了", "带回", "打回")
                if any(k in what_s for k in _GOODS_SPEND_HINTS) and not any(k in what_s for k in _SERVICE_SPEND_HINTS):
                    goods_spend_notes.append(f"{fmt_yuan(amt)}元「{what_s}」")

            elif kind == "transfer":
                fa = normalize_account(op.get("from_account"))
                ta = normalize_account(op.get("to_account"))
                if fa not in sim_bal or ta not in sim_bal or fa == ta:
                    errors.append(f"{tag} money[{m_i}]：transfer 必须写对不同的 from_account 与 to_account（如 tin_box -> napcat2 或 napcat2 -> master）")
                    continue
                if fa == "master":
                    errors.append(f"{tag} money[{m_i}]：未经主人聊天发话，严禁在补写里自己从 master 拨钱出来")
                    continue
                if seaside and at_home is False and (fa == "tin_box" or ta == "tin_box"):
                    errors.append(f"{tag} money[{m_i}]：人不在船屋（at_home=false），碰不到船舱抽屉里的公用铁盒(tin_box)进行存取！")
                    continue
                if seaside and at_home is False and ta == "master" and "master" not in with_list:
                    errors.append(f"{tag} money[{m_i}]：人不在家也没跟主人在一起，没法隔空把钱交到主人钱包(master)，请先带回家交或放铁盒")
                    continue
                if sim_bal[fa] + 1e-6 < amt:
                    errors.append(f"{tag} money[{m_i}]：{acc_labels[fa]}({fa}) 仅余 {fmt_yuan(sim_bal[fa])} 元，不够拿出 {fmt_yuan(amt)} 元转给 {ta}")
                    continue
                sim_bal[fa] = round_yuan(sim_bal[fa] - amt)
                sim_bal[ta] = round_yuan(sim_bal[ta] + amt)

            elif kind == "earn":
                acc = normalize_account(op.get("account"))
                if acc not in sim_bal:
                    errors.append(f"{tag} money[{m_i}]：earn 必须填合法收款账户 account（一般进经手人 napcat2 或直接上交 master / 放 tin_box）")
                    continue
                if seaside and at_home is False and acc == "tin_box":
                    errors.append(f"{tag} money[{m_i}]：人在外头赚的钱先装进自己兜里(napcat2)，回船屋后再放铁盒或交给主人")
                    continue
                if seaside and at_home is False and acc == "master" and "master" not in with_list:
                    errors.append(f"{tag} money[{m_i}]：人不在家也没跟主人在一起，没法隔空把收入塞进主人钱包(master)，请先装自己兜里(napcat2)")
                    continue
                reason = str(op.get("reason") or "").strip().lower()
                if seaside:
                    if reason not in EARN_REASONS:
                        errors.append(
                            f"{tag} money[{m_i}]：海边赚钱必须填结构化 reason（四选一："
                            f"work=打零工/修小物件，salvage=打捞委托结费，sell=卖手中真实库存实物，consign_payout=寄卖回款）"
                        )
                        continue
                    if reason == "work" and amt > MAX_WORK_EARN:
                        errors.append(f"{tag} money[{m_i}]：零工/修补小活(reason=work)单笔最多 {int(MAX_WORK_EARN)} 元，当前 {fmt_yuan(amt)} 元过高")
                        continue
                    if reason == "salvage":
                        if not (master_replied or has_master_intent or "master" in with_list) and amt > MAX_WORK_EARN:
                            errors.append(
                                f"{tag} money[{m_i}]：较正式的打捞委托收费(reason=salvage > {int(MAX_WORK_EARN)}元)"
                                f"须绑定主人同意的 intent 打算；未立单前只可先收小额探看/登记，不准后台私接大单收费"
                            )
                            continue
                        if amt > MAX_SALVAGE_EARN:
                            errors.append(f"{tag} money[{m_i}]：单次打捞费(reason=salvage)不能超过 {int(MAX_SALVAGE_EARN)} 元")
                            continue
                    if reason == "sell":
                        if not used_existing_ids:
                            errors.append(
                                f"{tag} money[{m_i}]：卖物换钱(reason=sell)必须在同段 things 或 items 里真有对应已有物品编号 id 的去货动作"
                                f"（送人了/用完了/用掉一部分/用了），严禁无库存凭空变废铁卖钱！"
                            )
                            continue
                        if not (master_replied or has_master_intent or "master" in with_list) and amt > AUTONOMOUS_SELL_LIMIT:
                            errors.append(
                                f"{tag} money[{m_i}]：未经主人拍板（未绑定主人确认的 intent 且未与 master 同行），"
                                f"单笔自主变卖(reason=sell)上限 {int(AUTONOMOUS_SELL_LIMIT)} 元，当前 {fmt_yuan(amt)} 元须先 ask_master 请主人定夺"
                            )
                            continue
                        if amt > MAX_ROUTINE_EARN:
                            errors.append(f"{tag} money[{m_i}]：收购站/集市普通变卖单笔上限 {int(MAX_ROUTINE_EARN)} 元")
                            continue
                    if reason == "consign_payout":
                        errors.append(
                            f"{tag} money[{m_i}]：寄卖回款(reason=consign_payout)必须改用 kind=\"debt_settle\" 并填对应的待收 debt_id 核销，不能用 kind=\"earn\" 重复领款"
                        )
                        continue
                sim_bal[acc] = round_yuan(sim_bal[acc] + amt)
                if op_cp and seg_dt is not None:
                    recent_ops.append((bot, kind, amt, op_cp, seg_dt, what_s[:60]))

            elif kind == "debt_add":
                direction = normalize_direction(op.get("direction"))
                cp = clean_text(op.get("counterparty"), 40)
                if direction not in DEBT_DIRECTIONS or not cp:
                    errors.append(f"{tag} money[{m_i}]：debt_add 必须填 direction(\"payable\"|\"receivable\") 与对方名字 counterparty")
                    continue
                if direction == "payable":
                    has_payment_or_barter = True
                    _DEBT_SERVICE_HINTS = (
                        "泡澡", "澡堂", "洗澡", "修理费", "修船费", "修补费", "维修费", "代修", "请人修",
                        "缝补费", "裁缝费", "运费", "船费", "渡费", "工钱", "工费",
                        "邮费", "寄信", "寄包裹", "寄件", "快递", "门票", "理发", "诊费", "看病",
                        "茶水费", "茶位", "租金", "租借费", "租船费", "房租", "押金",
                        "打煤油", "灌煤油", "散装煤油", "一壶煤油", "小壶煤油", "打水", "淡水", "小费", "打赏", "捐款", "捐钱",
                    )
                    _DEBT_GOODS_HINTS = ("买", "购", "称", "置办", "添置", "挑了", "带回", "打回", "赊账买", "赊购")
                    if any(k in what_s for k in _DEBT_GOODS_HINTS) and not any(k in what_s for k in _DEBT_SERVICE_HINTS):
                        goods_spend_notes.append(f"赊账{fmt_yuan(amt)}元「{what_s}」")
                sim_id = f"sim_debt_{bot}_{idx}_{m_i}"
                sim_debts[sim_id] = {
                    "id": sim_id,
                    "direction": direction,
                    "counterparty": cp,
                    "amount": amt,
                    "remaining": amt,
                    "about": what_s,
                    "status": "open",
                    "created": fmt_time(seg_dt) if seg_dt is not None else fmt_time(moment),
                }
                if seg_dt is not None:
                    recent_ops.append((bot, kind, amt, cp, seg_dt, what_s[:60]))

            elif kind == "debt_settle":
                did = str(op.get("debt_id") or "").strip()
                tgt = sim_debts.get(did)
                if tgt is None:
                    errors.append(f"{tag} money[{m_i}]：debt_id={did!r} 在当前未结欠账/待收列表中不存在")
                    continue
                acc = normalize_account(op.get("account"))
                if acc not in sim_bal:
                    errors.append(f"{tag} money[{m_i}]：debt_settle 必须填收/付款账户 account")
                    continue
                if tgt.get("direction") == "payable":
                    if acc == "master":
                        errors.append(f"{tag} money[{m_i}]：补写不能直接从主人钱包(master)代还欠账")
                        continue
                    if seaside and at_home is False and acc == "tin_box":
                        errors.append(f"{tag} money[{m_i}]：人在外头不能直接掏屋里的公用铁盒还账")
                        continue
                    if sim_bal[acc] + 1e-6 < amt:
                        errors.append(f"{tag} money[{m_i}]：{acc_labels[acc]}({acc}) 仅余 {fmt_yuan(sim_bal[acc])} 元，不够还 {fmt_yuan(amt)} 元")
                        continue
                    sim_bal[acc] = round_yuan(sim_bal[acc] - amt)
                else:
                    if seaside and at_home is False and acc == "tin_box":
                        errors.append(f"{tag} money[{m_i}]：在外头收回的欠款先装进自己兜里(napcat2)，回船屋后再交")
                        continue
                    if seaside and at_home is False and acc == "master" and "master" not in with_list:
                        errors.append(f"{tag} money[{m_i}]：人不在家也没跟主人在一起，没法隔空把收回的欠款塞进主人钱包(master)，请先装自己兜里(napcat2)")
                        continue
                    sim_bal[acc] = round_yuan(sim_bal[acc] + amt)
                rem = round_yuan(tgt.get("remaining") if tgt.get("remaining") is not None else tgt.get("amount", 0.0))
                if rem > 0:
                    tgt["remaining"] = max(0.0, round_yuan(rem - amt))
                if amt <= 0 or tgt.get("remaining", 0.0) <= 0.01:
                    sim_debts.pop(did, None)

        if seaside and bought_items and not has_payment_or_barter:
            errors.append(
                f"{tag}：买了 {'、'.join(bought_items)}（what=\"买回来\"），但 money 里既没写付钱(spend)、也没写赊账(debt_add)，"
                f"也没在 things/items 里用已有东西以物易物！必须如实付现或赊账"
            )
        if seaside and goods_spend_notes and new_inbound_count == 0 and not raw.get("kerosene") and not raw.get("water"):
            # 双人同行买东西时，仅兼容对方确实与自己同行且在相近时段记入 what=="买回来" 新实物的情况（防居家做饭误豁免）
            partner_inbound = False
            seg_st_raw = str(raw.get("start") or "")
            for p_st, p_bot, _p_idx, p_raw in timeline:
                if p_bot != bot and p_bot in with_list:
                    p_with = [str(x) for x in (p_raw.get("with") or [])]
                    if bot not in p_with:
                        continue
                    same_or_near = str(p_raw.get("start") or "") == seg_st_raw
                    if not same_or_near and seg_dt is not None and p_st is not None:
                        p_dt = parse_time(p_st) if isinstance(p_st, str) else p_st
                        if p_dt is not None and abs((p_dt - seg_dt).total_seconds()) <= 30 * 60:
                            same_or_near = True
                    if same_or_near and any(
                        isinstance(e, dict) and e.get("new") and str(e.get("what") or "").strip() == "买回来"
                        for e in (p_raw.get("things") or []) + (p_raw.get("items") or [])
                    ):
                        partner_inbound = True
                        break
            if not partner_inbound:
                errors.append(
                    f"{tag}：花了 {'、'.join(goods_spend_notes)} 买实物，但本段 things/items 里没有对应的新物品入库（what=\"买回来\"）！"
                    f"买下的东西必须同步记入 things 或 items，避免扣了钱家里却没多东西"
                )

    return errors

