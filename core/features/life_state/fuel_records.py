import re

from .world import clean_steps, parse_time, step_finished_at

WOOD = re.compile(r"柴炉|柴火|薪柴|主柴|干柴|湿柴|细柴|碎柴|木柴|漂流木|木料|木头|引火细料|引火料|引火柴|实木边料|枯枝|添柴|烧柴")
FIRE = re.compile(r"烧起来|烧着|燃烧|点燃|点着|点火|引着|引燃|引火(?!细|料|柴)|烧柴|添柴|补火|续火|(?:添|补入).{0,8}(?:柴|木)|续.{0,8}(?:柴|木)|(?:柴|木).{0,10}(?:烧|燃)|试火|试烧")
REFUEL = re.compile(r"添柴|补入|续火|添.{0,8}(?:柴|木)")
DEFERRED = re.compile(r"明天|明日|明早|明晚|后天|昨天|昨日|回忆|说起|说着|说到|讨论|商量|商定|说明|聊|打算|准备|计划|如果|假如|尚未|还没|没有|没能|不能|暂不|暂缓|取消|停止|不再|不继续|不把|不拿|(?:等|待).{0,16}(?:再|后|才)")
PURPOSE = re.compile(r"(?:供|用于|用来|留作|留着|备作|留给).{0,24}$|(?:检查|查看|清点|挑选|备好).{0,12}$")


def actions(text: str, pattern):
    # 只检视动作前的意图/否定，避免“点着后没有漏烟”抹掉已经执行的点火。
    text = re.sub(r"(?<!引)火柴", "点火用品", str(text or ""))
    clauses = re.split(r"[，。；;!?！？\n]|然后|随后|接着|但(?:是)?|却", text)
    for clause in clauses:
        for match in pattern.finditer(clause):
            before, after = clause[:match.start()], clause[match.end():]
            if (DEFERRED.search(before + match.group())
                    or re.search(r"(?:不|未|没)(?:能|曾|要|会|再|继续)?$", before)
                    or re.search(r"(?:不|未|没)(?:能|曾|要|会|再|继续)?(?:烧|燃|点|添)", match.group())):
                continue
            if PURPOSE.search(before) or re.match(r"(?:用(?:的)?|所需|所用|用品|材料|燃料|燃柴|安排|计划|之前|前|时再)", after):
                continue
            yield clause


def burning(text: str, wood_context: bool = False) -> bool:
    return any(wood_context or WOOD.search(clause) for clause in actions(text, FIRE))


def refueling(text: str) -> bool:
    return any(actions(text, REFUEL))


def requirements(entry: dict, until) -> list:
    steps = clean_steps(entry.get("steps"))
    context = re.sub(r"(?<!引)火柴", "点火用品", "，".join([str(entry.get("doing") or "")] + steps))
    wood_context = bool(WOOD.search(context))
    out = []
    for number, text in enumerate(steps, 1):
        at = step_finished_at(entry, number)
        if at is not None and at <= until and burning(text, wood_context):
            out.append((number, at))
    if not out and not entry.get("stopped"):
        at = step_finished_at(entry) or (parse_time(entry.get("start")) if entry.get("kind") == "event" else None)
        summary = "，".join(str(entry.get(key) or "") for key in ("doing", "result", "public"))
        if at is not None and at <= until and burning(summary, wood_context):
            out.append((0, at))
    return out


def shared(first: dict, second: dict) -> bool:
    return (first.get("id") != second.get("id") and first.get("who") != second.get("who")
            and first.get("who") in (second.get("with") or []) and second.get("who") in (first.get("with") or [])
            and all(first.get(key) == second.get(key) for key in ("start", "end", "place")))
