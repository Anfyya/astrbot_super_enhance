"""记录员：把对话原文里发生过的事合进世界线。

- 触发：某个会话最后一条消息过了 recorder_quiet_seconds 秒、且有没处理过的消息；
- 输入：新消息原文（带时间、标好说话人）+ 相关人物最新条目 + 生效中的约束/打算/约定/在练的事 + 现在时间；
- 输出：JSON（entries / constraints / intentions / promises / trainings / abilities / things）；
- 失败：游标不前进，写错误记录，下次重试。
"""

import asyncio
import copy
import datetime
import hashlib
import re
import time
from dataclasses import dataclass, field

from astrbot.api import logger

from ...common import llm, timeutil
from ...common.text import strip_inline_system
from .companionship import RECORDER_NOTE
from .mood_judge import (
    call_update_master_mood,
    compute_owner_heard_by,
    infer_peer_aware,
    is_interruptible_step_entry,
)
from .homewords import localize
from .facts import fact_batch
from .living_rules import LIFECYCLE_RULES
from . import weathering
from .things import clean_stage, insert_stage
from .models import bot_of_umo
from .things import (CAT_FOOD, CAT_ITEM, KEEPS, KINDS, SAME_NEW_MINUTES, SAME_STEP_MINUTES, SPOTS, STEPS, USE_STEPS, cat_of, spots_for,
                     checked_place, find_same, norm_kind, norm_step, usable)
from .world import (WorldStore, clean_steps, clean_text, doing_text, event_text_at, fit_steps, fmt_time, is_event, is_sleep, parse_time,
                    result_text, step_minutes, why_text, wind_up)
from .things import reached_steps

# 记录员停过一阵时，往前最多翻几天的原始消息（原始消息副本最多也只留这么久）
RAW_BACK_DAYS = 6

SYSTEM_PROMPT = llm.SYSTEM_PROMPT

# 给记录员对照的「后面排好的」：往后看几小时、每人最多几段
# 新打算腾地方：时间段超过这么久就当说得太笼统，不动排好的
MAKE_ROOM_HOURS = 3
PLANNED_HOURS = 6

def same_place(a, b) -> bool:
    """两个地点说的是不是同一处（去掉「穗织·」、空白后一样，或者一个包着另一个）。"""
    x, y = (re.sub(r"\s+|^穗织[·・]", "", str(v or "")) for v in (a, b))
    return bool(x and y) and (x == y or x in y or y in x)


def window_span(window, now: datetime.datetime):
    """打算的时间段「11:25~12:00」→ (开始, 结束)，按 now 那天算；认不出返回 None。"""
    m = re.search(r"(\d{1,2})[:：](\d{2})\s*[~～\-—–到至]\s*(\d{1,2})[:：](\d{2})", str(window or ""))
    if not m:
        return None
    day = now.replace(tzinfo=None).date()
    try:
        lo = datetime.datetime.combine(day, datetime.time(int(m.group(1)), int(m.group(2))))
        hi = datetime.datetime.combine(day, datetime.time(int(m.group(3)) % 24, int(m.group(4))))
    except ValueError:
        return None
    if hi <= lo:
        hi += datetime.timedelta(days=1)
    return lo, hi


RECORDER_SYSTEM_TEMPLATE = """{base_system}

规则（只按这些做，不要推理没说出口的东西）：

1. 只记原文里明确说了的。时间没说的，用那条消息的时间；说不清就先不记。不许记晚于「现在」的事。
2. 条目里的 start / end 一律写成完整的 "YYYY-MM-DD HH:MM"，**日期必须照抄对话原文里的日期**，不许只写时分、也不许自己推成别的日子。
3. 主人说他自己的事（去了哪、在干什么），记成一条 who = "master" 的条目。他说还在外面、还没回来、还得一会才回去，也是他这会儿的情况，照样记一条（人在外面、还没回宿舍），要回来那件事另记打算。他在做一件要做一阵的事（在打游戏、在图书馆、在上课、在吃饭、在家待着），记成 activity，place 写他在哪（说了或聊天里看得出才写），**end 一律留空**：他没说什么时候结束，就一直算他还在那儿，等他后来说了别的再另记一条。别替他估时长——「上课去了」「在图书馆待着」这种本来就没说几点完，估成两三个小时只会记错；他自己说了要多久、几点结束（「上两小时课」「待到五点」）的，才按他说的写 end；他只说了一下子的事、没说在哪（「吃完了」「到了」「好了」「醒了」），记成 kind 是 "event" 的一条，只有一个时刻、不写 end，别记成他一直在做这件事。他说到了哪儿（「我到图书馆了」），是换了地方，照常记 activity。他的反问、抱怨、随口一问（例如「你知道我几点回来？」）不是他的安排，不记。主人说他自己**之后**要干什么（明天有课、晚上要去图书馆、晚饭后陪她去夜市），不记成条目，记成 who = "master" 的打算（intentions）：text 写清楚他要去做什么、在哪、跟谁，date 写哪天，window 写钟点（说了才写）；要带上她一起去的，她那边另记一条 with 是 ["master"] 的打算。主人**答应了她提的事**，同样是主人自己的安排：她提议一起出去、下到码头、晚上去夜市这类，主人回「行」「好」「也行」「可以」「走吧」「听你的」这种明确答应的，照样记一条 who = "master" 的打算（text 写清他要做什么、跟谁，例如「和亚托莉一起下到码头边看小孩摸贝壳」；date 写哪天，没说就是当天；window 写钟点，没说留空），她那边同时按第 6 条记一条 with 是 ["master"] 的打算。主人只回「嗯」「哦」「再说吧」「随便」这种没表态的可不算答应，不记。主人的打算过了那天程序会自己撤；他说不去了、改了主意的，照常按编号撤（第 14 条）。
4. 主人给她定的、往后一直管着她的规矩（不许做什么、往后都要怎么做），记成 constraints（约束）：到期或主人撤了才解除。text 只写主人自己说出口的话；她回话里自己添上的、答应时顺嘴加的，都不是主人的规矩，不写进去。主人叫她去做一件做完就了结的事，不是规矩：叫她过会儿再去做的，按第 6 条记成打算；叫她现在就去做的，按第 5 条办（答应了还没做上的记成 now 的打算）。主人的原话是反问、反话的（例如罚站时说「一边站着一边干别的也叫站着？」），text 写成直说的意思（「站着的时候不许干别的」），不添主人没说的。约束的 since 填主人说这句话的那条消息的时间；主人说了期限的要换算成 until_time（"这几天"按三天算、"今晚"到当天 23:59、"明天之前"到明天 0:00），只说了条件的写进 until。新规矩不要写编号，编号由程序分配。
5. 你只记**现在和过去**：已经发生了的、这会儿正在发生的。还没发生的（将来）交给补写去排，你不排。她的条目分两种，写在 kind 里：
   - "activity"：她说她已经开始做另一件事、已经换了地方、已经把手上的事放下去干别的了。一条只写一件事，她说了接连做了几件的，按先后拆成几条首尾相接。end 写这件事按常理预计做完的时刻（晚于 start）；她说已经做完的，end 写做完的那一刻。主人叫她做的事、她说已经在做了的，再写 asked: true（做完了程序会叫她回来跟主人说一声）。
   - 她答应现在就去做一件事、可还没做上（主人叫她去买、去做饭、去看看，她答应了；或者她自己说「这就回去给主人做饭」）：先对照「今天的简略世界线」——如果她答应的事**本来就是**世界线上标着「正在做」或紧接着下一段「还没到」已经排好的同一件事（例如补写本来就排着紧接着回舱、码柴、做饭，她只是顺口说「这就回舱里」「马上来陪主人」），**什么都不用记**（不记 `now: true` 打算、不新建 activity、也不把当前段写进 stale）；若确实要把这条打算落下来、且世界线上已有标着「正在做 / 还没到」的补写段覆盖它，必须在 `intentions` 里显式填 `"same_as": "<那段编号>"`（如 `"e218"`），程序不会打断或撤排原世界线。只有真正改道去干别的事时，才按下面记成 `now: true` 打算（第 6 条），到了以后怎么做由补写来排，并把现在从手上那件自然接下去（手上本来闲着的除外）：
     · 手上那件写进 stale（第 16 条）定怎么收场，照常理和她自己的本事来：在足汤先擦干脚、穿好鞋，在外面吃东西先赶紧吃完，在做饭先关火，坐着摩天轮得等它转完下来（丛雨能穿墙、能飘，可以直接飘下来；亚托莉不行）；手上闲着、随时能停的写放下。
     · 她人不在做那件事的地方的，再记一段 activity 写她往那边去（例如「从温泉街往宿舍走，回去给主人做饭」）：start 写她答应的那条消息的时间（程序会挪到收完尾以后），end 照路程估，今天清单里走过同一条路的照那个时长，没走过的按常理。人已经在那儿的，不用记这段。
     · 她说已经到了、已经做上了，才照上一条记成她在做那件事。之后她的话要是说明其实没去、改了主意、或者被叫去干别的了，按第 16 条把那段写进 stale，打算按第 14 条撤。
   - "event"：做手上的事当中顺带发生的一件事（递了个东西、被摸了头、答应了什么），只有一个时刻，不写 end，不打断手上那件。主人生她的气、训她、骂她、跟她闹别扭、冷着她这类会让她心里放不下的事，也记一条 event，再写 upset: true（程序会让补写把她后面的安排照这件事重排）；主人说气消了、原谅她了、和好了的，不写 upset。
   她只是边做事边跟人说话，或者说的就是「今天的简略世界线」里标着「正在做」的那件，或者只是顺口提到/复述紧接着下一段「还没到」且距离原定开始时间只差约 10 分钟以内的同一件事，**什么都不用记**（不要新建 activity 覆盖原补写，不要写 stale，也不要记 `now: true` 打算）：聊天本身不算一件事。她说自己做了什么、在做什么，就算做了（哪怕听着像编的）。
   只有和她手上那件真冲突，才记一段新的 activity 把它换掉。真冲突是指：她说人在别的地方；她说那件事停下了、做完了；她说正在做的事和手上那件不可能同时做。只是没提手上那件、只提了其中一部分、或者说了件能同时做的小事，都不算冲突：能记成 event 就记 event，不然什么都不记。没说不等于没做。
   手上那段标着「闲着」的：她说开始做了一件具体的事（看书、出门、做饭……），就记一段新的 activity。
6. 之后要去做的一件具体的事（待会儿、下午、晚饭后、明天去做什么），记成 intentions（打算），不是条目：她自己说要去做的，和主人叫她过会儿去做的，都算。她答应现在就去做的（第 5 条）也记成打算：写 now: true，since 写她答应的那条消息的时间，window、date 留空；她向主人提议、主人还没答应的（「中午咱们去买豆腐呗」「要不要去后山」），不算，不记；主人明确答应了的，按第 3 条把他那条记成 master 的打算，她这边照常记一条 with 是 ["master"] 的打算。text 要写清楚，让没看过这段对话的人也能照着去做：做什么、在哪、跟谁、要带上或用到什么、做到什么样算完；只用对话里有的信息补全，不编没说的。怎么说起的（几点谁说的、因为什么）一句话写进 why，不写进 text：text 她每次聊天都看得到，前因写在里头，等于一遍遍提醒她当时的事。比如不要写「回头把体己数给主人看」，text 要写「把柜角小布袋里攒的铜板拿出来，当着主人的面一枚枚数给他看，说清是哪来的」，why 写「10-17 晚上逛夜市时主人问她钱是哪来的，她说是香火铜板攒的，答应回头数给他」。这件事要和主人一起做、而且是主人提的或主人答应了的，with 写 ["master"]；她一个人做的留空。date 写哪天做（YYYY-MM-DD，「明天」按对话日期换算；半夜 0 点到天亮前说的「明天」「明早」「天亮」指天亮以后的这一天，日期不加一），没说哪天留空；window 写钟点范围，没说就留空。打算可以同时有好几条，做完一条或哪条对不上了，就按编号撤那一条（见第 14 条）。往后都怎么做、往后注意什么（做法、原则），不是一件要去做的事，不记成打算：这话是主人说的，按第 4 条记成规矩；只是她自己说的，什么都不记。
7. 两个角色之间的对话同样处理；另一个角色说的话，不能当成主人说的。认清每句话里的「你」「我」：主人话里的「我」是主人自己，「你」是这段私聊里的她（看上面「这段对话是谁和谁」）；她话里的「我」和她的自称是她自己。记谁做了什么、谁说了什么，照这个认主语，别把她做的事记成主人做的，也别反过来（例如主人说「饭你刚才也做好了」，是她做好的饭，不是主人做的）。拿不准是谁做的，就不写是谁做的。
   条目的 with 里写另一个角色（她和对方一起）之前，先看清单里对方那会儿在干什么：两人本来就在一起、这会儿一起走的，两个人的条目都记上（对方那条 with 写她），对方手上那段也按第 16 条写进 stale；对方那边已经记着别的事（尤其是对方自己说的），就照对方的来，只记她自己：with 不写对方，doing 里也别写成和对方一起。
8. 每条条目写两层：public 是住在一起的人自然会知道的粗粒度信息（人在哪、大概在干哪类事，例如"在藏经阁闭关练功"、"出门吃饭了"），可以为空表示别人完全不知道；doing 是细节，可以写具体在干什么、聊了什么。
9. seen_by 写"谁能看到细节"：当事人自己不用写；当时在场的人、以及这段话本来是在哪段私聊里说的，写进去。
10. 这段时间里发生的事都要记下来（她开始做什么、去了哪、什么时候、主人交代了什么要紧的话），不要只挑一两件；纯寒暄、一句话的应答、边干活边聊天都不用记，同一件事连着说的几句合成一条。
11. 上面「生效中的约束」里，已经不该再管着她的，把编号填进 lift_constraints：到期条件已经达成的；主人撤销或放宽了的；后来发生的事已经和它对不上的（例如被赶出去、禁足之后主人又叫她回来、原谅了她；说好"这几天"的那几天已经过了）；因为某件事才定的规矩，那件事结束了也跟着撤（两个人的都算）。这些不算撤：主人试探她、说气话反话（罚着的时候说「那你走吧」「你直接走吧」「可以不凶你了」，是看她走不走，不是放她）；夸一句「态度还不错」「可以」；主人在原来那条上又补了一句要求（罚站时又说「好好站着」「站着不许干别的」），那是给那条添了要求，原来那条照旧留着，补的要求另记一条。要撤得是主人明说了不用再这样了（「进来吧」「不用站了」「不罚了」「去做别的吧」）。拿不准就不动。
12. 条目里如果是在练「在练的事」里的某件，training 填它的编号，例如 "#2"；不是在练就留空。trainings 里 update / done / drop 的 id 也填这个编号。
13. 她在回答、解释、回忆以前的事（昨天、早上、刚才已经说过的）时，那件事不是这会儿发生的：不要按这条消息的时间再记一条；下面「今天已经记了的」里有的别重复记。确实没记过、要补的，时间写成那件事实际发生的时候。
14. 上面列的「打算」里（每条前面有编号，比如 i3），已经做完的、或者后来的事已经和它对不上的（例如说好在社务所等，后来人已经回了宿舍），把那几条的编号填进 drop_intentions（只撤这几条，别的留着）；这次又说了新打算的，照常写进 intentions；她之前说过、还没做完也没对不上的，不用重写。
15. 她说「今天的简略世界线」里标着「还没到」的某件事已经做了、或者正在做，按两档处理：
    - 只是顺口复述、或者距离原定开始时间只差约 10 分钟以内：什么都不用记（别把补写已经排好的段重写成聊天条目，也不要写进 stale）。
    - 确实明显提前（超过约 10 分钟）已经做完手上那件、正式进入了后面某段「还没到」的补写事：以聊天为准，照第 5 条记成她做了 / 在做那件事（时间写她说这话的那条消息的时间），**并必须在该条目里显式填 `"same_as": "<原补写段编号>"`**（例如 `"e218"`；`same_as` 条目的 `with` 请照原补写段写，避免误判为甩开同行人），手上那段不必再写进 stale；程序会精准承接该补写段（及同行人对应的共同段）并保留后续排程，不会撤掉整天世界线。她说的是「待会儿去」「打算做」且世界线上还没排进去的，照第 6 条记打算。
16. 「今天的简略世界线」里补写排的、主人让做的、你之前从聊天里记的（标着「正在做」「还没到」的都算），都是按当时的情况写的。这段对话里说的事让其中某几段真正对不上了的（例如主人说他回来了、出门了，主人说那件事先不做了，她答应了这会儿改去做别的，人换了地方，另一个人那段跟她有关的事跟着变了；注意：只是顺口复述或按第 15 条提前进入紧邻的已排补写段，不要写进 stale），把对不上的那几段写进 stale：who、entry（编号）、why（一句话写为什么不照原样做了，用她那边发生的事来说，例如「主人叫她去街上看看有什么硬菜」），再写 how：这件事怎么收场。照常人的做法想，手上正做着扔不下的事，不会撂下就走：
    - 做完：快做完了，先把它做完再去；minutes 写还要几分钟。
    - 收尾：手上的事扔不下（灶上炒着菜、锅里煮着东西、手里端着东西），先收个尾再走；wrap 写收尾做了什么（例如「关火，把炒了一半的饭盛出来扣好」），minutes 写几分钟；result 写放下时那件事是什么样（例如「饭炒到一半，盛在盘里扣着」）。这件事算没做完。
    - 交给：交给另一个人接着做；to 写交给谁（napcat / napcat2），doing 写那个人接着做什么，minutes 写交接用几分钟。
    - 放下：随时能停的事（看书、坐着、闲聊），或者还没开始的，直接放下。
    - 没做：其实压根没做（她只是嘴上答应了，后面一直在聊别的、改了主意），整段撤掉；她那段时间实际在干什么，照第 5 条另记一条（时间写实际的）。
    - 接着做：清单里标着「和她一起」的另一个人那段，她走了以后对方一个人接着做；doing 写对方一个人接着做什么，result 写对方一个人做完是什么样（只写对方这一个人，走了的那个不写进去）。
    她接下去的那段（往那边去的路上，或者她说已经做上了的新的那件），从收尾、做完、交接完的时候才开始（start 照常写那条消息的时间，程序会往后挪）。和她一起做这件事的另一个人那段（清单里标着「和她一起」），也写进 stale，按上面挑一样（多半是接着做、放下，或者被交给的那个人接手）。只写对不上的；照样做得了、不受影响的不写。还没开始的段不用写进 stale：谁的现在被你改了，程序会把她后面补写排的段全撤掉，由补写按新的情况从头排。
17. 对话原文里标着「（主动发的）」的，是她自己主动发给主人的消息，不是在回主人的话；里面说她做了什么，沿用已经成立的世界线，勿因复述重复入账。
18. 吃的（家里的存货）：买回来的、收到的、做好的吃的，每件一个编号，从有了那一刻起每一步都记进 things。只记聊天里说已经发生了的：「待会去买」「明天做」不记；说的是能这么做、要是这么做会怎样的假设（「挂在旧货铺能换好几个齿轮」「拿去卖能换不少钱」「这个能做成汤」），也不记，那件东西还在原处，别记成已经换了、卖了、做了；主人接着这句话说的（「铜管换齿轮？」「你要拿它去换？」）也只是在说这个假设，不是她真去换了；世界线里写着、聊天里没说的不记（那些程序会按世界线自己算进清单）。标「（主动发的）」的消息里说已经买了、做了、吃了的，也照记（这一条和第 17 条不一样）。
   - 新的一件（上面「家里的吃的」里没有的）：写 new（这一批里的临时代号 n1、n2……；不要写编号，编号由程序发）、name、kind、origin、from，再加上它的第一步。第一步只能是「买回来」「收到」「做好了」「原来就有」：买回来、收到的，必须是聊天里说了真的买了、收到了；家里一直备着、清单里还没记的（盐、糖、醋、米面油这类），头一回说到用了它，先记一件「原来就有」（amount 写估计还有多少），再记用掉；别的清单里没有的东西被吃了、用了，不记（不知道它是哪来的）。清单里已经有的同一件（同一次买的、同一锅做的）不要再建。旧的还没吃完又买了一份，是新的一件。买了当场就吃完的零嘴不用记。
   - 米面油盐、调料干货、腌菜这些按量用的（kind 是「干货调料粮食」「腌菜酱菜泡菜」，清单里写着「还剩多少克 / 毫升」）：量写准确的数，用克、毫升、斤写，不要写「大半袋」「用掉一点」：买回来、收到、原来就有写 amount（一共多少，例如「5 斤」「500 毫升」）；用掉写 used（这次用了多少，例如「3 克」「15 毫升」；聊天里没说就按这道菜平常的用量估）；聊天里说了还剩多少的（「盐只剩个罐底了」「米还有半袋」），记一步「清点」，amount 写折成克 / 毫升的剩多少。一盘菜、一锅汤照旧写大概（left 写「小半锅」「吃了一半」）；按个数的照旧写个数（「两块」）。
   - kind 从这几类里挑：{kinds}。origin 写哪来的（谁在哪买的、谁送的）；自己做的留空。
   - 做菜：做好的那盘菜是新的一件，第一步「做好了」，from 写用了清单里哪几件（写编号；这一批里新记的写临时代号；用到的米面油盐、调料也写上）；用掉的每一件，各记一步「用掉一部分」或「用完了」，into 写那盘菜，按量记的写上 used。用的料清单里没有的，from 里就不写它，一件都对不上就留空。买来的、别人送的熟菜，from 留空。剩下的菜还是那一件，又吃了就记「吃了一部分」「吃完了」。
   - 已有的那件又发生了一步：写 id（清单里的编号；这一批里新记的写临时代号）和这一步。同样的东西有好几件、说不清是哪件的，记在最早有的那件上。
   - 每一步写：at（发生的时刻 YYYY-MM-DD HH:MM）、what（{steps}）、left（这一步之后还剩多少，比如「两块」「小半锅」；用完、吃完写空——用完、吃完必须单独写一步，what 填「用完了」或「吃完了」，别把「吃完了」塞进 left 里）、amount / used（按量记的才写，见上）、cook（自己刚做出的熟菜才填五选一：boil/fry/steam/roast/cold，其余不填）、place（搁在哪，写法见下一条）、keep（怎么放：{keeps}）、spot（放在哪，管冷热：{spots}；屋里常温的不用写）、state（整的 / 切开了 / 碰伤了 / 没熟透）、by（谁：napcat / napcat2 / master，别人写名字）、into（用掉是做进了哪盘菜）、quote（摘原文里说这件事的那句话）。place、keep、spot、state 跟上一步一样的不用写。剩菜回锅、凉了热一热记一步「热了一下」；端回锅里重新煎透、煮透、再蒸一下、回炉也是「热了一下」（她说已经下锅了就记，不用等做完）；聊天里说出来中间没熟、夹生、没煎透的，在说出来的那一刻给那件记一步「清点」，state 写「没熟透」，left 照原样；保鲜加工直接填明确动作 what：轻腌 / 重盐腌 / 糖渍 / 醋泡 / 酱渍 / 油封 / 灰沙埋 / 晒上了 / 炉烘 / 熏上了（晒、炉烘、熏写上 spot）。
   - place 拆开写，别连成一句：area 是在哪间屋、哪个地方（不在宿舍的写那个地方，例如「镇外梯田的农家灶房」）；at 是搁在那儿的什么上、什么里（矮桌、柜格、案上、灶上、水盆、书架、窗台……）；detail 是再细一点的哪一格、哪个容器里（桌角、第二格、竹筛里、锅里），说不上来就空着。area 和 at 都得有，光写一间屋不算。例如 {{"area": "堂屋", "at": "矮桌", "detail": "桌角"}}、{{"area": "灶房", "at": "案上", "detail": "竹筛里"}}、{{"area": "灶房", "at": "柜格", "detail": ""}}。拿在手上、揣在身上的：area 写谁，at 写手上、袖子里、菜篮里，例如 {{"area": "亚托莉", "at": "手上", "detail": ""}}。聊天里没说搁在哪的，照常理挑一处（剩菜盖好搁灶上或柜格，点心、糖搁堂屋柜里或盒子里，米面调料在灶房柜格）；没挪地方的不写 place。
   - 「记错了」只用在清单里确实记错了的那件（根本没这回事）。
   - 聊天里主人或她说吃了、吃完了、用了、用完了清单上的某件（世界线里做的、算进清单的也算），就给那件记一步（开吃 / 吃了一部分 / 吃完了 / 用掉一部分 / 用完了），别漏：
     · 主人说开始吃/喝、动筷、尝一口了（如「开动」「我吃啦」「真香」「端过来吃了」「喝了口」），记一步 `what: "开吃"`、`by: "master"`（程序从此开始计时，过后无人再动才算吃完）；
     · 主人明确说吃剩一半、吃不下放着，记 `吃了一部分` 并写剩余 `left`；明确说吃光、吃完了，记 `吃完了`；
     · 没点名的（主人只说「开吃」「吃完了」），按前面几句在说哪件认（刚端给他的、刚提到的那件），认不出是哪件的不记。
   - 聊天里说了把清单上的某件端给主人、送到主人跟前、递到他手上了（清单里那件还不在主人跟前的），给那件记一步「挪地方」，place 写 {{"area": "主人", "at": "跟前", "detail": ""}}（at 一律写「跟前」，原话说递到手上、搁在手边也写跟前），by 写端过去的人。程序不会把摆在跟前的东西自动当成吃完，只有主人说开吃记了「开吃」后才开始计时；只端了一部分上去、剩下的还在原处的，不记这一步；摆在桌上大家一起吃的、留给主人还没端过去的，也不记。
19. 家里的东西（吃的以外的：衣裳围巾、书册本子、纸条信、画和窗花、布偶摆件、工具器物、首饰小物……）：每件一个编号（p 开头），写进 items，记它从哪来、经过谁手、搁在哪、变成什么样。只记聊天里说已经发生了的，规矩和第 18 条一样。
   - 新的一件（上面「家里的东西」里没有的）：写 new（这一批里的临时代号 x1、x2……；编号由程序发）、name（叫得出区别的名字，例如「深青围巾」「兔子窗花」「压在茶壶底下的纸条」，别只写「围巾」「纸条」）、kind（它是什么，一个词：围巾、窗花、纸条、布偶、书……；炉柴只准写 燃柴 / 薪柴 / 引火料，闷炭写 燃料，待卖废铜旧铁写 废铜 / 废铁 / 废金属 且可附 purpose="scrap"）、look（样子：颜色、材料、大小、上面有什么，能和同一种的别的那件分清；纸条、信、便笺这种写了字的，聊天里说了写的是什么，就把原话用「」写进 look）、owner（最终归谁所有/谁穿谁用：napcat / napcat2 / master，大家一起用的填「共用」或留空；替主人或别人买的、做给谁穿用的，填实际拥有者或受赠人，别把跑腿买回来的人误填为 owner）、origin（哪来的）、from（用清单里哪几件做的，比如自家柴炉烧硬木后拿火钳夹红炭闷出闷炭，from 写所烧硬柴编号），再加上它的第一步。若是新入库炉柴，amount/used/left 请按重量填「公斤」（如 `15公斤`、`1.5公斤`，勿写「捆」），并附上 `weather: {{"fuel_source": "land_deadwood|pruning|workshop|reclaimed|supplier|drift_unknown", "fuel_size": "kindling|split|log", "fuel_state": "ready|wet|drying"}}`（仅 `fuel_size="log"` 整段原木需加 `"split": false`，劈好可进炉改 `"fuel_size": "split", "split": true`；`kindling` 细枯枝引火料省略 `split`）；若是封口未拆的旧物/上锁铁盒/封口药瓶，请附上 `weather: {{"sealed": true}}`（聊天里当面拆开时再记一步 `weather: {{"sealed": false}}`）。第一步只能是「买回来」「收到」「捡到」「开始做」（起了个头、还没做完）「做好了」「原来就有」（家里本来就有、清单里还没记的）。
   - 已有的那件又发生了一步：写 id（清单里的编号）和这一步，what 从这几样挑：挪地方 / 交给（交到谁手上）/ 用了 / 清点 / 做了一截（没做完的接着做）/ 做好了（没做完的那件做完了）/ 变了样（修好了、弄脏了、洗干净了）/ 送人了 / 丢了 / 坏了扔了 / 用完了 / 记错了。
   - 每一步写：at、what、place（搁在哪，和第 18 条一样拆开写，例如 {{"area": "卧房", "at": "针线笸箩", "detail": ""}}、{{"area": "堂屋", "at": "书架", "detail": "第二格"}}）、holder（在谁手上、身上：napcat / napcat2 / master，别人写名字；放下搁着就留空）、state（这一步之后它什么样，例如「织到一半」「织好了」「补上了耳朵」）、amount / used / left / into（消耗品或做东西用到的材料可顺带写数量）、by（谁弄的）、owner（归属换了才写，例如送给主人了写 master）、quote。跟上一步一样的不用写。
   - 同一种东西有好几件的（清单里标着「长得像，认准编号」），看清是谁做的、什么样、在哪再记；对不上是哪一件就不记，别记到别的那件上，也别为它另建一件。
   - 清单里写着「按天气算，此刻：……」（淋湿了、生锈了、被风吹进海里了）的是程序照天气算的现在的样子：聊天里她说把它晾干了、擦干了、除了锈、洗了、收进来了、下水捞回来了，给那件记一步，state 写成处理后的样子（「晾干了」「锈擦掉了」），place 写现在搁在哪。
   - 聊天里主人或她说用了、交给谁了、挪了地方、用完了清单上的某件，就给那件记一步，别漏；清单里已经有的（世界线里买回来、捡到、做的也算），不管那边记的是「买回来」还是「捡到」，都记在那件上，不要另建。
20. 钱的进出（人民币账本，**职责分工防双扣**）：本处**只记**原文里确实已经发生的以下两类动钱，其余一律留空 `[]`：
   - ① 直接涉及主人钱包（`master`）：主人亲口从自己钱包付钱、主人从钱包给铁盒或女孩拨款、女孩当面把钱交进主人钱包；
   - ② 主人亲口发话让女孩把钱放进铁盒或从铁盒拿出、女孩当场照做的 `transfer`（如「把这八十块放进铁盒里」→ `napcat2 -> tin_box`）；但若只是女孩口头复述世界线里早就拿过/放回过的钱，不重复记。
   - **严禁记录女孩自己的买菜买柴、卖废品、收打捞费、赊账与还账**：这些全部由世界线统一记账，聊天里提起只是汇报，绝不在此重复记账！「待会去买」「要是有钱就能…」这类假设或打算也不记；也别再把现金当成新物件 `items`（`kind="钱"`）新建。
   - account / from_account / to_account 四选一：`master`（主人钱包，只能主人亲口花了或发话拨款才记从 `master` 出）、`tin_box`（船舱抽屉公用铁盒 p157）、`napcat2`（亚托莉口袋）、`napcat`（丛雨零花）。
   - 仅限上述范围选填：
     · `transfer`（主人拨款、上交主人，或主人亲口指挥把钱放进/拿出铁盒）：填 `from_account`、`to_account`、`amount`、`what`、`at`、`quote`；
     · `spend`（主人亲口掏自己钱包买东西/付工费）：填 `account: "master"`、`amount`（正数元）、`counterparty`、`what`、`at`、`quote`；
     · `earn` / `debt_settle`（主人亲自当场收款进 `master` 或用 `master` 结账）：填 `account: "master"`、`amount`（正数元）及对应字段。
{world_notes}
注：`entries` 每条请顺手填 `"at_home"`（布尔值：在住处/船屋内、甲板、储物舱填 `true`；离开住处去码头、商店街、集市、打捞船、水下或走在路上填 `false`；聊天没提地点无法判断时填 `null`）；聊天里若明确提到她换了衣裳、穿上/脱下厚棉衣、换睡衣或换鞋袜，请在对应 entry 里填 `"outfit"`（写清换完后身上完整的实际穿着，没换衣则留空 `""`）。
输出这个 JSON（没有的字段留空数组）：

{{
  "entries": [
    {{"who": "napcat", "kind": "activity", "start": "2026-10-11 09:00", "end": "2026-10-11 09:40", "place": "", "at_home": true, "doing": "",
      "public": "", "with": [], "training": "", "seen_by": [], "stove": "", "outfit": "", "lamp": false, "kerosene": false, "asked": false, "upset": false, "same_as": "", "quote": "摘一句原文"}}
  ],
  "constraints": [
    {{"who": "napcat", "text": "练成收放之前不许出藏经阁", "public": "在藏经阁闭关练功",
      "since": "2026-10-12 00:11", "until": "在练的事 #3 练成", "until_time": ""}}
  ],
  "lift_constraints": ["c3"],
  "intentions": [{{"who": "napcat", "text": "晚饭后跟主人去温泉街夜市，挨个摊子逛一圈、跟着他挤一回人堆", "why": "15:11 她问今晚能不能带她去，主人答应了，说周六人多", "window": "18:40~21:00", "date": "2026-10-17", "with": ["master"]}},
                 {{"who": "napcat2", "text": "去镇上鱼摊买两条黑鱼回来，晚上做水煮鱼", "why": "16:40 主人说想吃水煮鱼，她答应这就去买", "now": true, "since": "2026-10-17 16:40", "window": "", "date": "", "with": [], "same_as": ""}},
                 {{"who": "master", "text": "去鹈茅学院上课", "why": "23:03 睡前他说明天还有课", "window": "", "date": "2026-10-18", "with": []}}],
  "drop_intentions": ["i3"],
  "stale": [{{"who": "napcat2", "entry": "e100", "why": "主人叫她去街上看看有什么硬菜", "how": "收尾", "wrap": "关火，把炒了一半的饭盛出来扣好", "minutes": 2, "result": "饭炒到一半，盛在盘里扣着", "to": "", "doing": ""}}],
  "things": [
    {{"new": "n1", "name": "嫩豆腐", "kind": "鲜水豆腐", "origin": "主人在商店街豆腐店买的", "from": [],
      "at": "2026-10-18 08:30", "what": "买回来", "left": "两块", "place": {{"area": "灶房", "at": "水盆", "detail": ""}}, "keep": "泡在水里", "state": "整的", "by": "master", "quote": "摘一句原文"}},
    {{"new": "n2", "name": "豆腐味噌汤", "kind": "熟汤羹粥", "origin": "", "from": ["n1", "w7"],
      "at": "2026-10-18 11:40", "what": "做好了", "left": "一锅", "place": {{"area": "灶房", "at": "灶台", "detail": "锅里"}}, "keep": "收着放", "by": "napcat2", "quote": "摘一句原文"}},
    {{"id": "n1", "at": "2026-10-18 11:40", "what": "用掉一部分", "left": "一块", "into": "n2", "by": "napcat2", "quote": "摘一句原文"}},
    {{"id": "w7", "at": "2026-10-18 11:40", "what": "用完了", "left": "", "into": "n2", "by": "napcat2", "quote": "摘一句原文"}},
    {{"id": "w4", "at": "2026-10-18 11:40", "what": "用掉一部分", "used": "3 克", "into": "n2", "by": "napcat2", "quote": "摘一句原文"}}
  ],
  "items": [
    {{"new": "x1", "name": "深青围巾", "kind": "围巾", "look": "深青色粗毛线，平针", "owner": "master", "origin": "丛雨给主人织的", "from": [],
      "at": "2026-10-16 13:05", "what": "开始做", "place": {{"area": "卧房", "at": "矮桌", "detail": ""}}, "state": "起了个头", "by": "napcat", "quote": "摘一句原文"}},
    {{"id": "p3", "at": "2026-10-17 20:00", "what": "交给", "holder": "master", "owner": "master", "state": "织好了", "by": "napcat", "quote": "摘一句原文"}}
  ],
  "money": [
    {{"kind": "transfer", "from_account": "master", "to_account": "tin_box", "amount": 200, "what": "主人往公用铁盒放两百块备用", "at": "2026-10-18 08:10", "quote": "摘一句原文"}},
    {{"kind": "spend", "account": "master", "amount": 80, "counterparty": "二楼旧衣铺", "what": "主人掏钱包买两件厚棉衣", "at": "2026-10-18 13:02", "quote": "摘一句原文"}}
  ],
  "promises": [{{"action": "add", "bots": ["napcat"], "date": "2026-10-12", "time": "14:00",
                "what": "陪主人进山看红叶", "with": "主人"}}],
  "trainings": [{{"action": "start", "bot": "napcat", "what": "收放自如", "standard": "十回连着一收一放不漏不散",
                 "need_hours": 12}}],
  "abilities": [{{"bot": "napcat", "text": "已能真正进食"}}],
  "companionship": {{
    "owner": {{}},
    "decisions": [],
    "notes": [],
    "master_mood": {{"target": "none", "level": 0, "reason": "", "peer_aware": false}}
  }}
}}

关于 promises / trainings 的 action：
- promises：add（新约定）/ done（完成了，填 id）/ cancel（取消了，填 id）；
- trainings：start（开始练）/ update（改时长或标准）/ done（练成了）/ drop（不练了），update/done/drop 要填 id。
- abilities 只在主人明确确认她学会了一件新本事、而这件事不是在练的事时才写，很少用。
{lifecycle_rules}
{recorder_note}
{master_mood_note}
"""

MASTER_MOOD_RECORDER_NOTE = """
【主人情绪（放入 companionship.master_mood，无变化可省略或填 target="none"）】
- 格式："master_mood": {"target": "self"|"both"|"owner"|"none", "level": -1..3, "reason": "白话一句", "peer_aware": false}
- 判断主人有没有生气、缓和或情绪变化：target 填 self（冲当前角色）/ both（冲两人）/ owner（主人自身或外部）/ none（无变化）；level 填 0平常、1小不满、2明显生气、3很生气（owner 可填 -1 开心）；peer_aware 为另一位室友是否在场知情；条目里照旧可标 upset: true。
""".strip()

RECORDER_TEMPLATE = """你在给一段对话做记录：把刚刚发生的事，整理成结构化的条目。

说话人的名字对照：{labels}

{things_block}

{today_block}

{interaction}

现在：{now}
这段对话是谁和谁：{where}

对话原文（**每条前面是完整日期和时间**，例如 2026-10-12 06:55；这两天都有，别把昨天的当成今天的）：
{transcript}
"""


REVIEW_TEMPLATE = """请判断下面还在生效的规矩（约束）中，哪些已经不该再管着她们了。

还在生效的规矩：
{constraints}

最近两天发生的事（按时间先后）：
{events}

算「不该再管着」的情况：到期条件已经达成；主人撤销或放宽了；后来发生的事已经和它对不上了
（例如被赶出去、禁足之后主人又叫她回来、原谅了她；说好"这几天"的那几天已经过了）；
规矩是因为某件事才定的（例如因为丛雨被赶走，才让亚托莉别去找她、这几天只陪主人），那件事结束了，规矩也跟着撤。
说话方式这类一直有效的规矩（例如回话不许用某个字）不要动。拿不准就不动。

现在是 {now}。只输出 JSON：{{"lift": [{{"id": "c10", "reason": "一句话说明"}}]}}
"""

REVIEW_MAX_EVENTS = 160


def norm_time(value, date_text: str, fallback: str = "") -> str:
    """把 09:00 / 2026-10-11 09:00 / 空 规整成 2026-10-11 09:00。"""
    text = str(value or "").strip().replace("T", " ")
    if len(text) == 5 and text[2] == ":":
        text = "%s %s" % (date_text, text)
    elif len(text) >= 16:
        text = text[:16]
    elif not text:
        text = str(fallback or "")
        if len(text) >= 16:
            text = text[:16]
    return text if parse_time(text) else ""


def _extract_companionship(data: dict) -> dict | None:
    """读取结构化 companionship 字段，兼容模型把 owner/decisions/notes/master_mood 写在 JSON 顶层。"""
    if not isinstance(data, dict):
        return None
    nested = data.get("companionship")
    out = dict(nested) if isinstance(nested, dict) else {}
    top_owner = data.get("owner")
    if isinstance(top_owner, dict) and not isinstance(out.get("owner"), dict):
        out["owner"] = top_owner
    top_mm = data.get("master_mood")
    if isinstance(top_mm, dict) and not isinstance(out.get("master_mood"), dict):
        out["master_mood"] = top_mm
    for list_key in ("decisions", "notes"):
        top_list = data.get(list_key)
        if isinstance(top_list, list) and top_list:
            base = out.get(list_key)
            out[list_key] = (list(base) if isinstance(base, list) else []) + list(top_list)
    return out or None


def _digest(text: str) -> str:
    return hashlib.md5(str(text or "").encode("utf-8")).hexdigest()[:12]


# 她得先收尾 / 做完 / 交接完才去做新的那件：只挪这么多分钟以内开始的（再早的是在补记以前的事）
FREE_SHIFT_MINUTES = 10
# 两人「一起」做的同一件新事：两边开始的时刻差这么多分钟以内才算是同一件（两边各自收尾的时间可能不一样）
PAIR_NEAR_MINUTES = 15


@dataclass
class RecorderBatch:
    """一次记录员落库期间共享的临时标记，不再散挂到 Recorder 自己身上。"""

    touched: dict[str, datetime.datetime] = field(default_factory=dict)
    mentioned: set[str] = field(default_factory=set)
    fresh: set[str] = field(default_factory=set)
    free_at: dict[str, datetime.datetime] = field(default_factory=dict)
    written: list[dict] = field(default_factory=list)
    moved_map: dict[tuple[str, str], tuple[str, dict]] = field(default_factory=dict)
    same_as_movers: set[str] = field(default_factory=set)
    same_as_targets: set[str] = field(default_factory=set)
    upset_events: list[dict] = field(default_factory=list)


class Recorder:
    """按会话防抖的对话→世界线整理。"""

    def __init__(self, context, cfg, world: WorldStore, raw_log, label_fn, now_fn, tz) -> None:
        self.context = context
        self.cfg = cfg
        self.world = world
        self.raw_log = raw_log
        self.label = label_fn
        self._now = now_fn
        self.tz = tz
        self._running: set[str] = set()
        self._start_proposals: dict[str, list[dict]] = {}
        self._proposal_tasks: dict[str, asyncio.Task] = {}

    # ---------- start_doing 1 分钟延迟裁定队列 ----------

    def _prune_proposals(self, umo: str) -> list[dict]:
        now_ts = time.time()
        lst = [
            p for p in (self._start_proposals.get(umo) or [])
            if (now_ts - float(p.get("ts") or 0)) <= 600.0
        ]
        if lst:
            self._start_proposals[umo] = lst[-5:]
        else:
            self._start_proposals.pop(umo, None)
        return self._start_proposals.get(umo) or []

    def peek_proposals(self, umo: str) -> list[dict]:
        return list(self._prune_proposals(umo))

    def due_proposals(self, umo: str) -> list[dict]:
        now_ts = time.time()
        return [p for p in self._prune_proposals(umo) if now_ts >= float(p.get("due_ts") or 0)]

    def consume_proposals(self, umo: str, pids: set[str]) -> None:
        if not pids or umo not in self._start_proposals:
            return
        left = [p for p in self._start_proposals[umo] if str(p.get("pid") or "") not in pids]
        if left:
            self._start_proposals[umo] = left
        else:
            self._start_proposals.pop(umo, None)

    def queue_start_proposal(self, umo: str, proposal: dict, delay: float = 60.0) -> None:
        """暂存 start_doing 提议，不直接改世界线；delay 秒后自动唤醒记录员结合对话与排程裁定。"""
        now_ts = time.time()
        row = dict(proposal)
        row.setdefault("pid", "sp_%d_%d" % (int(now_ts * 1000), len(self._start_proposals.get(umo) or [])))
        row["ts"] = float(row.get("ts") or now_ts)
        row["due_ts"] = now_ts + max(1.0, float(delay))
        lst = self._prune_proposals(umo)
        lst.append(row)
        self._start_proposals[umo] = lst[-5:]
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        old = self._proposal_tasks.get(umo)
        if old is not None and not old.done():
            old.cancel()
        self._proposal_tasks[umo] = loop.create_task(self._delayed_proposal_run(umo, max(1.0, float(delay))))

    def cancel_proposal_tasks(self) -> list[asyncio.Task]:
        """取消并清理所有待执行的 start_doing 延迟裁定任务（停机/重载时由 service.terminate 调用）。"""
        cancelled: list[asyncio.Task] = []
        for _umo, task in list(self._proposal_tasks.items()):
            if task is not None and not task.done():
                task.cancel()
                cancelled.append(task)
        self._proposal_tasks.clear()
        return cancelled

    async def _delayed_proposal_run(self, umo: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            # 若此刻该会话正在跑记录员，等待本轮跑完后再重检未消费提议，绝不直接丢弃导致饿死
            waited = 0.0
            while umo in self._running and waited < 180.0:
                await asyncio.sleep(0.5)
                waited += 0.5
            if not bool(self.cfg.get("recorder_enable", True)):
                return
            if umo in self._running or not self.peek_proposals(umo):
                return
            logger.info("[super_enhance/world] start_doing 1 分钟定时到点，唤醒记录员裁定（%s）", umo)
            await self.run(umo, self._now(), force_proposal=True)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.warning("[super_enhance/world] start_doing 延迟唤醒记录员出错", exc_info=True)
        finally:
            try:
                cur_task = asyncio.current_task()
                if cur_task is not None and self._proposal_tasks.get(umo) is cur_task:
                    self._proposal_tasks.pop(umo, None)
            except Exception:
                pass

    # ---------- 配置 ----------

    @property
    def batch(self) -> RecorderBatch:
        state = self.__dict__.get("_batch")
        if state is None:
            state = self._batch = RecorderBatch()
        return state

    @batch.setter
    def batch(self, value: RecorderBatch) -> None:
        self._batch = value

    @property
    def quiet_seconds(self) -> float:
        try:
            return float(self.cfg.get("recorder_quiet_seconds", 90) or 90)
        except Exception:
            return 90.0

    @property
    def provider_id(self) -> str:
        return str(self.cfg.get("llm_provider_id") or "").strip()

    @property
    def timeout(self) -> float:
        try:
            return float(self.cfg.get("world_llm_timeout", 120) or 120)
        except Exception:
            return 120.0

    def _resolve_provider(self):
        return llm.resolve_provider(self.context, self.provider_id)

    # ---------- 待处理的消息 ----------

    def _day_texts(self, now: datetime.datetime, since_ts: float = 0.0) -> list[str]:
        """要翻的几天：昨天和今天；游标停在更早（记录员停过一阵）就从那天翻起，最多往前 RAW_BACK_DAYS 天。"""
        first = now.date() - datetime.timedelta(days=1)
        if since_ts:
            try:
                day = datetime.datetime.fromtimestamp(since_ts, now.tzinfo).date()
                first = max(min(first, day), now.date() - datetime.timedelta(days=RAW_BACK_DAYS))
            except Exception:
                pass
        days = (now.date() - first).days
        return [(now.date() - datetime.timedelta(days=i)).isoformat() for i in range(days, -1, -1)]

    def unprocessed_from(self, now: datetime.datetime) -> str:
        """还有没处理的消息的会话里，游标停在的最早那天（原始消息清理时这天以后的先留着）；都处理完了返回空串。"""
        days = []
        for umo in self.sessions():
            if not self.pending(umo, now):
                continue
            ts = float(self.world.cursor(umo).get("ts") or 0)
            if ts:
                days.append(datetime.datetime.fromtimestamp(ts, now.tzinfo).date().isoformat())
        return min(days) if days else ""

    def pending(self, umo: str, now: datetime.datetime) -> list[dict]:
        """这个会话里，游标之后还没处理的原始消息。"""
        cursor = self.world.cursor(umo)
        last_ts = float(cursor.get("ts") or 0)
        last_hash = str(cursor.get("hash") or "")
        out = []
        for date_text in self._day_texts(now, last_ts):
            for item in self.raw_log.read_day(date_text):
                if str(item.get("umo") or "") != str(umo):
                    continue
                ts = float(item.get("ts") or 0)
                if ts < last_ts:
                    continue
                if ts == last_ts and last_hash and _digest(item.get("text")) == last_hash:
                    continue
                out.append(item)
        return out

    def ready(self, umo: str, now: datetime.datetime) -> bool:
        """最后一条消息已经安静够久了，或有到点的 start_doing 提议待裁定。"""
        if umo in self._running:
            return False
        if self.due_proposals(umo):
            return True
        items = self.pending(umo, now)
        if not items:
            return False
        try:
            last_ts = max(float(x.get("ts") or 0) for x in items)
        except Exception:
            return False
        return (now.timestamp() - last_ts) >= self.quiet_seconds

    def sessions(self) -> list[str]:
        out = []
        for key in ("proactive_targets", "session_list"):
            value = self.cfg.get(key)
            if isinstance(value, list):
                for item in value:
                    text = str(item or "").strip()
                    if text and text not in out:
                        out.append(text)
        return out

    # ---------- 提示词 ----------

    def _where_text(self, umo: str) -> str:
        bot = bot_of_umo(umo)
        label = self.label(bot) or bot
        return "%s和主人的私聊（会话 %s）：主人话里的「你」就是%s，「我」是主人自己" % (label, umo, label)

    def _transcript(self, items: list[dict], umo: str) -> str:
        bot = bot_of_umo(umo)
        label = self.label(bot) or bot
        lines = []
        for item in items:
            role = str(item.get("role") or "")
            text = str(item.get("text") or "")
            if str(item.get("src") or "") == "proactive":
                role = "assistant"
            if role == "assistant":
                who = label + ("（主动发的，只代表她自己的话）" if item.get("src") == "proactive" else "")
            else:
                who = "主人"
                # 主人塞给她的一次性提示（<system>……</system>）不是他说的话，不记成规矩
                text = strip_inline_system(text)
                if not text.strip():
                    continue
            lines.append("%s %s %s：%s" % (item.get("date"), item.get("time"), who, text))
        return "\n".join(lines)

    def _today_block(self, now: datetime.datetime, since: datetime.datetime | None = None) -> str:
        if getattr(self, "things", None) is not None:
            self.things.view(now)
        parts = []
        home_scene = str(self.cfg.get("world_home") or "").strip()
        for item in self.world.constraints(active_only=True, scene=home_scene):
            parts.append(
                "- 生效中的约束 %s（%s，%s 起%s）：%s"
                % (
                    item.get("id"),
                    self.label(str(item.get("who"))) or item.get("who"),
                    str(item.get("since") or "")[5:16],
                    "，到期条件：%s" % item["until"] if item.get("until") else "",
                    item.get("text"),
                )
            )
        for item in self.world.intentions():
            tags = [str(item.get("who"))]
            if "master" in (item.get("with") or []):
                tags.append("和主人一起")
            when = " ".join(x for x in (str(item.get("date") or "")[5:], str(item.get("window") or "")) if x)
            if when:
                tags.append(when)
            if item.get("now_at"):
                tags.append("%s 答应现在就去" % timeutil.day_hm(item.get("now_at")))
            parts.append("- 打算 %s（%s）：%s%s" % (item.get("id"), "，".join(tags), item.get("text"), why_text(item)))
        book = getattr(self, "trainings", None)
        if book is not None:
            try:
                for item in book.items():
                    if str(item.get("status")) != "open":
                        continue
                    parts.append(
                        "- 在练的事 #%s（%s）：%s（条目里在练它就把 training 填成 #%s；练了多久由程序按条目算）"
                        % (item.get("id"), item.get("bot"), item.get("what"), item.get("id"))
                    )
            except Exception:
                pass
        promises = getattr(self, "promises", None)
        if promises is not None:
            try:
                for item in promises.items():
                    if str(item.get("status")) != "open":
                        continue
                    parts.append(
                        "- 还没做的约定 #%s：%s %s %s"
                        % (item.get("id"), item.get("date"), item.get("time") or "", item.get("what"))
                    )
            except Exception:
                pass
        flat = now.replace(tzinfo=None)
        ref_since = (since.replace(tzinfo=None) if since is not None else flat)
        old_cutoff = ref_since - datetime.timedelta(hours=3)
        day0 = flat.replace(hour=0, minute=0, second=0, microsecond=0)
        horizon = flat + datetime.timedelta(hours=PLANNED_HOURS)
        bots = list(self.raw_log.enabled_bots())
        current_ids = {}
        for bot in bots:
            cur = self.world.current(bot, flat)
            current_ids[bot] = str((cur or {}).get("id") or "")
        lines = []
        for who in bots + ["master"]:
            name = "主人" if who == "master" else (self.label(who) or who)
            for day in (day0.date(), day0.date() + datetime.timedelta(days=1)):
                for item in self.world.entries(day.isoformat(), who):
                    start = parse_time(item.get("start"))
                    if start is None or start < day0 or start > horizon:
                        continue
                    end = parse_time(item.get("end")) if item.get("end") else None
                    is_cur = who != "master" and str(item.get("id")) == current_ids.get(who)
                    is_old = (not is_cur) and (start <= flat) and (end is not None and end <= old_cutoff)
                    if is_old and item.get("idle") and not item.get("asked"):
                        continue
                    hm = timeutil.span_text(item.get("start"), item.get("end"), date_prefix=not is_old, ongoing="")
                    flags = []
                    if is_cur:
                        flags.append("正在做")
                    if start > flat:
                        flags.append("还没到")
                    if item.get("idle"):
                        flags.append("闲着")
                    if is_sleep(item):
                        flags.append("睡觉")
                    if item.get("asked"):
                        flags.append("主人让做的")
                    partners = [self.label(x) or x for x in (item.get("with") or []) if x in bots and x != who]
                    if "master" in (item.get("with") or []):
                        partners.append("主人")
                    if partners:
                        flags.append("和%s一起" % "、".join(partners))
                    head = "- %s %s %s%s" % (item.get("id"), hm, name,
                                              "（%s）" % "、".join(flags) if flags else "")
                    # event_text_at 内部 detail_of 已带 place，不再在 head 重复拼 place
                    body = event_text_at(item, flat)
                    if is_old and len(body) > 76:
                        body = body[:75] + "…"
                    lines.append((start, head + "：" + body))
        lines.sort(key=lambda x: x[0])
        if lines:
            parts.append("- 今天的简略世界线（一行一条，从早到晚；标「还没到」的是还没发生的）：")
            parts.extend(text for _, text in lines)
        return "今天到目前为止（供你对照，别重复记）：\n" + ("\n".join(parts) if parts else "（空）")

    def _things_block(self, now: datetime.datetime) -> str:
        """家里的吃的：现有的一件一行（编号、剩多少、在哪、哪来的、新不新鲜），附最近一天没了的。"""
        store = getattr(self, "things", None)
        parts = []
        flat = now.replace(tzinfo=None)
        if store is not None:
            shelf = store.here(store.view(flat), flat)
            parts.append("家里的吃的（存货，按第 18 条记；每件前面是程序发的编号）：\n" + store.food_list(flat, shelf, self.label))
            parts.append("家里的东西（吃的以外，按第 19 条记；每件前面是程序发的编号）：\n" + store.item_list(flat, shelf, self.label))
        book = getattr(self, "money", None)
        if book is not None and str(self.cfg.get("world_home") or "").strip() == "海边小镇":
            try:
                parts.append(book.recorder_block(flat, self.world))
            except Exception:
                logger.warning("[super_enhance/money] 记录员拼账面失败", exc_info=True)
        return "\n\n".join(parts)

    def _since(self, items, now: datetime.datetime) -> datetime.datetime:
        """这批消息里最早那条的时刻（不带时区的本地时间）；拿不到就用现在。"""
        stamps = []
        for item in items or []:
            try:
                stamps.append(float(item.get("ts")))
            except Exception:
                pass
        if not stamps:
            return now.replace(tzinfo=None)
        return datetime.datetime.fromtimestamp(min(stamps), self.tz).replace(tzinfo=None)

    @property
    def world_notes(self) -> str:
        """配置里写的故事固定事实（优先读精简版 chat_world_notes，保留全部地点与船屋规矩并省缓存开销）。"""
        text = str(self.cfg.get("chat_world_notes") or self.cfg.get("world_notes") or "").strip()
        lines = [x.strip().lstrip("-• ").strip() for x in text.splitlines() if x.strip()]
        lines = [x for x in lines if "写地点从这里挑" not in x]
        if not lines:
            return ""
        return "\n故事里的固定事实（照着记，不要写出和它冲突的事）：\n" + "\n".join("- " + x for x in lines)

    def build_system_prompt(self) -> str:
        """纯静态系统提示（规则、固定事实、JSON 示例、常识与记录规范），供跨批复用 KV 缓存。"""
        home = str(self.cfg.get("world_home") or "")
        return localize(RECORDER_SYSTEM_TEMPLATE.format(
            base_system=SYSTEM_PROMPT,
            kinds=" / ".join(KINDS),
            steps=" / ".join(STEPS),
            keeps=" / ".join(KEEPS),
            spots=" / ".join(spots_for(home)),
            world_notes=self.world_notes,
            lifecycle_rules=LIFECYCLE_RULES.strip(),
            recorder_note=RECORDER_NOTE,
            master_mood_note=MASTER_MOOD_RECORDER_NOTE,
        ), home)

    def _format_proposals(self, proposals: list[dict]) -> str:
        if not proposals:
            return ""
        lines = [
            "【待你裁定的「现在去做(start_doing)」提议（尚未写入世界线，也未撤掉原有排程）】：",
            "请结合上方完整对话、主人后续回话和「今天已记下的世界线」，逐条判断是否真要落盘：",
            "1) 若世界线上马上（如 15 分钟内）本来就排好了同一件事，直接丢弃忽略、别打断原排程；",
            "2) 若对话里主人没同意、改口、叫停，或提议与实际对话对不上，直接丢弃忽略；",
            "3) 若确属主人临时改主意或确实需要此刻新开一段，再照常写进 entries（填清 at_home / with / stove / same_as / stale，不要重复记同一件事）。",
        ]
        for p in proposals:
            who_lbl = self.label(p.get("bot")) or p.get("bot") or ""
            with_lbls = [
                "主人" if x == "master" else (self.label(x) or x)
                for x in (p.get("with") or [])
            ]
            bits = [
                "%s–%s %s提议：%s" % (str(p.get("at") or "")[11:16], str(p.get("end") or "")[11:16], who_lbl, p.get("what") or ""),
            ]
            if p.get("place"):
                bits.append("地点=%s" % p["place"])
            bits.append("at_home=%s" % ("true" if p.get("at_home") else "false"))
            if with_lbls:
                bits.append("同行=%s" % "、".join(with_lbls))
            if p.get("stove"):
                bits.append("stove=%s" % p["stove"])
            if p.get("intent"):
                bits.append("intent=%s" % p["intent"])
            lines.append("- " + "，".join(bits))
        return "\n".join(lines)

    def build_prompt(
        self, umo: str, items: list[dict], now: datetime.datetime, proposals: list[dict] | None = None
    ) -> str:
        labels = "、".join(
            "%s=%s" % (bot, self.label(bot) or bot) for bot in self.raw_log.enabled_bots()
        )
        companion = getattr(self, "companionship", None)
        bot = bot_of_umo(umo)
        interaction = (companion.safe("render", bot, now, recorder=True, default="")
                       if companion is not None else "")
        if companion is not None and bot:
            mm_line = self._master_mood_prompt_line(bot, now)
            if mm_line:
                interaction = (interaction + "\n" + mm_line) if interaction else mm_line
        previous = companion.safe("reply_context", umo, items, now, default=[]) if companion is not None else []
        transcript = self._transcript(items, umo) if items else "（本批暂无新增聊天消息，请直接裁定下方提议）"
        if previous:
            transcript = ("此前实际发出的两句话（只用来理解本批回应，不重复记成新发生的事实）：\n"
                          + self._transcript(previous, umo) + "\n\n本批待记录消息：\n" + transcript)
        prop_block = self._format_proposals(proposals or [])
        if prop_block:
            transcript = transcript + "\n\n" + prop_block
        return localize(RECORDER_TEMPLATE.format(
            now=fmt_time(now),
            where=self._where_text(umo),
            labels=labels or "（未配置）",
            transcript=transcript,
            today_block=self._today_block(now, since=self._since(items, now) if items else now - datetime.timedelta(minutes=30)),
            things_block=self._things_block(now),
            interaction=interaction,
        ), str(self.cfg.get("world_home") or ""))

    # ---------- 执行 ----------

    async def run(self, umo: str, now: datetime.datetime | None = None, *, force_proposal: bool = False) -> bool:
        moment = now or self._now()
        items = self.pending(umo, moment)
        has_due = bool(self.due_proposals(umo))
        proposals = self.peek_proposals(umo) if (force_proposal or has_due or items) else []
        if not items and not ((force_proposal or has_due) and proposals):
            return False
        if items and not proposals and all(str(x.get("src") or "") == "proactive" for x in items):
            # 单纯主动消息先轻量登记邀请，不额外调用模型或让她自己的话改写事实。
            companion = getattr(self, "companionship", None)
            if companion is not None:
                for item in items:
                    companion.safe("observe_message", item)
            last = items[-1]
            self.world.set_cursor(umo, float(last.get("ts") or 0), _digest(last.get("text")))
            logger.info("[super_enhance/life] %s 这批 %d 条全是主动消息，不喂记录员", bot_of_umo(umo),
                        len(items))
            return False
        # 一次别喂太多：积压就分几轮处理，每轮游标往前走一点
        try:
            batch = int(self.cfg.get("recorder_batch", 60) or 60)
        except Exception:
            batch = 60
        batch = max(10, min(batch, 200))
        if len(items) > batch:
            items = items[:batch]
        provider = self._resolve_provider()
        if provider is None:
            self.world.note_error("recorder", "没有可用的模型")
            return False
        self._running.add(umo)
        prop_ids = {str(p.get("pid") or "") for p in proposals if p.get("pid")}
        try:
            prompt = self.build_prompt(umo, items, moment, proposals=proposals)
            text, _thought = await llm.text_chat(
                self.context,
                prompt,
                provider_id=self.provider_id,
                timeout=self.timeout,
                session_id="super_enhance_recorder",
                system_prompt=self.build_system_prompt(),
                attempts=2,
                log_label="记录员(含start_doing裁定)" if proposals else "记录员",
            )
            data = llm.extract_json(text)
            if data is None:
                self.world.note_error("recorder", "返回内容不是 JSON")
                return False
            with fact_batch(self):
                self.apply(umo, data, moment, items)
                if items:
                    last = items[-1]
                    last_ts = float(last.get("ts") or moment.timestamp())
                    self.world.set_cursor(umo, last_ts, _digest(last.get("text")))
            self.consume_proposals(umo, prop_ids)
            self.world.clear_error("recorder")
            logger.info(
                "[super_enhance/world] 记录员处理了 %d 条%s（%s）",
                len(items),
                (" + 裁定 %d 条 start_doing 提议" % len(proposals)) if proposals else "",
                umo,
            )
            return True
        except Exception as e:
            logger.error("[super_enhance/world] 记录员失败：%s", e)
            self.world.note_error("recorder", "%s: %s" % (type(e).__name__, e))
            return False
        finally:
            self._running.discard(umo)

    async def run_all(self, now: datetime.datetime | None = None) -> int:
        moment = now or self._now()
        count = 0
        # 积压时一轮里连着跑几批，不然要等很久（每批之间的 30 秒节流在 _world_loop 里）
        for _ in range(3):
            did = 0
            for umo in self.sessions():
                if self.ready(umo, moment):
                    if await self.run(umo, moment, force_proposal=bool(self.due_proposals(umo))):
                        count += 1
                        did += 1
            if not did:
                break
        return count

    def skip_pending(self, now: datetime.datetime | None = None) -> int:
        """记录员关着的时候用（2026-11-12）：还没处理的消息不喂模型，游标直接挪到最后一条。
        不然再打开时，会把关着那几天的聊天一口气补记进世界线。返回挪了几个会话。"""
        moment = now or self._now()
        moved = 0
        for umo in self.sessions():
            if umo in self._running:
                continue
            items = self.pending(umo, moment)
            if not items:
                continue
            last = items[-1]
            self.world.set_cursor(umo, float(last.get("ts") or 0), _digest(last.get("text")))
            moved += 1
        return moved

    # ---------- 约束复核（每天一次） ----------

    def _review_prompt(self, active: list, now: datetime.datetime) -> str:
        flat = now.replace(tzinfo=None)
        rows = []
        for item in active:
            rows.append(
                "- %s（%s，%s 起%s%s）：%s"
                % (
                    item.get("id"),
                    self.label(str(item.get("who"))) or item.get("who"),
                    str(item.get("since") or "")[5:16],
                    "，到期条件：%s" % item["until"] if item.get("until") else "",
                    "，到 %s 为止" % item["until_time"] if item.get("until_time") else "",
                    item.get("text"),
                )
            )
        events = []
        for back in (1, 0):
            date_text = (flat.date() - datetime.timedelta(days=back)).isoformat()
            for item in self.world.entries(date_text, ""):
                start = parse_time(item.get("start"))
                if start is None or start > flat:
                    continue
                who = str(item.get("who") or "")
                name = "主人" if who == "master" else (self.label(who) or who)
                text = clean_text(item.get("doing") or item.get("public") or item.get("place"), 90)
                events.append("- %s %s %s：%s" % (str(item.get("start"))[5:16], name, item.get("place") or "", text))
        events = events[-REVIEW_MAX_EVENTS:]
        return REVIEW_TEMPLATE.format(
            now=fmt_time(flat),
            constraints="\n".join(rows),
            events="\n".join(events) or "（没有记录）",
        )

    async def review_constraints(self, now: datetime.datetime | None = None) -> list:
        """对照最近两天的世界线，把已经过时的约束撤掉。返回撤掉的编号。"""
        moment = now or self._now()
        home_scene = str(self.cfg.get("world_home") or "").strip()
        active = self.world.constraints(active_only=True, scene=home_scene)
        if not active:
            return []
        provider = self._resolve_provider()
        if provider is None:
            raise RuntimeError("没有可用的模型")
        text, _thought = await llm.text_chat(
            self.context,
            self._review_prompt(active, moment),
            provider_id=self.provider_id,
            timeout=self.timeout,
            session_id="super_enhance_constraint_review",
            system_prompt=SYSTEM_PROMPT,
            attempts=1,
            log_label="约束复核",
        )
        data = llm.extract_json(text)
        if not isinstance(data, dict):
            raise RuntimeError("返回内容不是 JSON")
        ids = {str(x.get("id")) for x in active}
        lifted = []
        for raw in data.get("lift") or []:
            if not isinstance(raw, dict):
                continue
            cid = str(raw.get("id") or "").strip()
            if cid in ids and cid not in lifted:
                self.world.lift_constraint(cid, note="复核解除：%s" % clean_text(raw.get("reason"), 60))
                lifted.append(cid)
        return lifted

    # ---------- 落库 ----------

    def _seen_by(self, entry: dict, umo: str) -> list[str]:
        who = str(entry.get("who") or "")
        out = []
        for value in [who] + [str(x) for x in (entry.get("seen_by") or [])] + [bot_of_umo(umo)]:
            if value and value not in out:
                out.append(value)
        return out

    def training_id(self, value, bot: str = "") -> str:
        """training 字段：「#2」「2」直接认；写成名字的按在练的事对上编号；对不上、或那件事已经不在练就留空。"""
        text = str(value or "").strip().lstrip("#＃").strip()
        if not text:
            return ""
        book = getattr(self, "trainings", None)
        if book is None:
            return ""
        open_items = [
            x for x in book.items()
            if str(x.get("status")) == "open" and (not bot or str(x.get("bot")) == str(bot))
        ]
        if text.isdigit():
            return text if any(str(x.get("id")) == text for x in open_items) else ""
        key = re.sub(r"[\s，。,.!！?？、：:「」（）()]+", "", text)
        for item in open_items:
            what = re.sub(r"[\s，。,.!！?？、：:「」（）()]+", "", str(item.get("what") or ""))
            if what and (what in key or key in what):
                return str(item.get("id"))
        return ""

    def apply(self, umo: str, data: dict, now: datetime.datetime, items=None) -> None:
        """一次批改受 fact_batch 整批原子保护；world.day_transaction 仅包裹世界线写入段，释放 WorldStore._lock 后再写 things/money，确保全库锁序单向无 AB-BA 死锁。"""
        with fact_batch(self):
            self._apply(umo, data, now, items)

    def _apply(self, umo: str, data: dict, now: datetime.datetime, items=None) -> None:
        batch = list(items or [])
        flat_now = now.replace(tzinfo=None)
        bots = set(self.raw_log.enabled_bots())
        state = self.batch = RecorderBatch()
        companion = getattr(self, "companionship", None)
        comp_raw = _extract_companionship(data)
        extra_plans = []
        # 阶段 1：世界线全量更新（持 WorldStore._lock 批处理并在退出时释放，避免持 WorldStore._lock 跨调 ThingStore/MoneyBook 形成 AB-BA 死锁）
        with self.world.day_transaction():
            if companion is not None:
                for item in batch:
                    companion.safe("observe_message", item)
                previous = companion.safe("reply_context", umo, batch, now, default=[])
                extra_plans = companion.safe("apply_record", umo, comp_raw, previous + batch, now, default=[])
                if extra_plans:
                    data = dict(data, intentions=list(data.get("intentions") or []) + extra_plans)
            # 预扫本批合法 same_as 承接，防止前面 _apply_stale 误删待承接段或无条件 _touch 把精准刹车打穿
            self._prescan_same_as(data.get("entries") or [], bots, flat_now)
            # 先定对不上的那几段怎么收场（收尾、做完、交接要几分钟），再记这次的新条目：免得新条目先把手上的事一刀截断
            state.free_at = self._apply_stale(data.get("stale") or [], now)
            self._apply_entries(data.get("entries") or [], umo, now, items, bots, state.free_at, flat_now)
            self._apply_master_mood(umo, comp_raw, batch, now, bots)
            if batch and not any(str(x.get("role") or "") == "user" for x in batch):
                # 这批全是她主动发的、主人一句没说：规矩和打算都不可能从这里来（主动消息里的不记）
                if data.get("constraints") or data.get("intentions"):
                    logger.info("[super_enhance/world] 这批只有她主动发的消息，丢掉记录员给的 %d 条规矩、%d 条打算",
                                len(data.get("constraints") or []), len(data.get("intentions") or []))
                data = dict(data, constraints=[], intentions=[])
            self._apply_constraints(data, umo, now, batch)
            self._apply_intentions(data, umo, now, bots, flat_now, batch)
            # 改到了谁的现在：她后面补写排的全撤掉，补写从头排；最后核对两人「一起」对得上
            for who, since in sorted(state.touched.items()):
                self._redo_after(who, since, now)
            self._unpair(now)
        # 阶段 2：已释放 WorldStore._lock，仍在 fact_batch(staged_writes + 全 Store 回滚) 保护下更新其他账本与存货
        self._apply_promises(data.get("promises") or [], now, umo)
        self._apply_trainings(data.get("trainings") or [], now)
        self._apply_abilities(data.get("abilities") or [], now)
        self._apply_things(data.get("things") or [], umo, batch, now)
        self._apply_things(data.get("items") or [], umo, batch, now, cat=CAT_ITEM)
        self._apply_money(data.get("money") or [], umo, batch, now)

    def _apply_entries(self, raws, umo: str, now: datetime.datetime, items, bots: set,
                       free_at: dict, flat_now: datetime.datetime) -> None:
        records = []
        for raw in raws:
            if not isinstance(raw, dict):
                continue
            who = str(raw.get("who") or "").strip()
            if not who:
                continue
            start = norm_time(raw.get("start"), now.date().isoformat(), fallback=fmt_time(now))
            if not start:
                continue
            if parse_time(start) > flat_now + datetime.timedelta(minutes=5):
                # 不可能发生在「现在」之后：多半是日期猜错了，宁可不记
                logger.warning("[super_enhance/world] 记录员给了晚于现在的条目，丢掉：%s %s", start, raw.get("doing"))
                continue
            date_text = start[:10]
            kind = "event" if str(raw.get("kind") or "") == "event" else "activity"
            end = "" if kind == "event" else norm_time(raw.get("end"), date_text)
            if end and parse_time(end) is not None and parse_time(end) <= parse_time(start):
                end = ""
            ready = free_at.get(who)
            if kind == "activity" and ready is not None and parse_time(start) < ready \
                    and parse_time(start) >= flat_now - datetime.timedelta(minutes=FREE_SHIFT_MINUTES):
                # 她得先收完尾、做完、交接完手上的事才去做这件：开始往后挪到那时（结束跟着挪；跨午夜同步更新 date_text）
                shift = ready - parse_time(start)
                start = fmt_time(ready)
                date_text = start[:10]
                if end:
                    end = fmt_time(parse_time(end) + shift)
            mine = self._by_start_doing(who, start, end) if kind == "activity" and who != "master" else None
            if mine is not None:
                # 她用「现在去做」自己写过了：记录员不另记，也不盖掉它（几点做完、做完叫她都照那段）
                logger.info("[super_enhance/world] 记录员记的「%s」撞上%s用现在去做写的 %s，不另记",
                            clean_text(raw.get("doing"), 30), self.label(who) or who, mine.get("id"))
                continue
            stove_val = str(raw.get("stove") or "").strip().lower()
            outfit_val = clean_text(raw.get("outfit"), 100) if str(raw.get("outfit") or "").strip() else ""
            entry = {
                "who": who,
                "kind": kind,
                "start": start,
                "end": end,
                "place": raw.get("place"),
                "at_home": raw.get("at_home"),
                "doing": raw.get("doing"),
                "public": raw.get("public"),
                "with": raw.get("with") or [],
                "training": self.training_id(raw.get("training"), who),
                "seen_by": self._seen_by(raw, umo),
                "source": "chat",
                "src_umo": umo,
                "src_quote": raw.get("quote"),
                "created": fmt_time(now),
                "stove": stove_val if kind == "activity" and stove_val in ("charcoal", "wood") else "",
                **({"outfit": outfit_val} if outfit_val else {}),
                **({"lamp": True} if kind == "activity" and (raw.get("lamp") is True or str(raw.get("lamp") or "").lower() == "true") else {}),
                **({"kerosene": True} if raw.get("kerosene") is True or str(raw.get("kerosene") or "").lower() == "true" else {}),
                **({"water": True} if kind == "activity" and (raw.get("water") is True or str(raw.get("water") or "").lower() == "true") else {}),
                # 主人叫她现在去做的：做完叫她回来说一声；被打断照实写；后来发现其实没去做，记录员能撤
                "asked": bool(raw.get("asked")) and kind == "activity" and who != "master",
            }
            moved = self._take_planned(raw.get("same_as"), who, start, end, flat_now) if kind == "activity" else None
            if moved:
                entry.update(moved)
                if outfit_val:
                    entry["outfit"] = outfit_val
            target_day = str(entry.get("start") or start)[:10] or date_text
            record = self.world.add_entry(target_day, entry)
            if record is None and moved:
                mf = moved.get("moved_from") if isinstance(moved.get("moved_from"), dict) else None
                orig_entry = mf.get("entry") if isinstance(mf, dict) else None
                orig_date = str((mf or {}).get("date") or "")
                if isinstance(orig_entry, dict) and orig_date:
                    self.world.add_entry(orig_date, copy.deepcopy(orig_entry))
                    logger.warning(
                        "[super_enhance/world] same_as 挪段写入失败（%s %s），已恢复原补写段 %s/%s",
                        who, start, orig_date, orig_entry.get("id"),
                    )
            if record is not None and kind == "event" and outfit_val and who in bots:
                # 聊天里当场换衣若被记成 event，同步刷新她此刻正在进行的活动段 outfit，确保聊天/生图即时生效
                ev_t = parse_time(start) or flat_now
                cur_act = self.world.current(who, ev_t)
                if cur_act is not None and cur_act.get("id"):
                    cur_day = str(cur_act.get("start") or "")[:10] or date_text
                    self.world.update(cur_day, str(cur_act["id"]), outfit=outfit_val)
            if record is not None:
                self.batch.written.append(record)
            if record is not None and moved:
                self._fit_moved(record)
                logger.info("[super_enhance/world] 记录员把%s排好的 %s 挪到 %s（%s），分步和标的吃的照留",
                            self.label(who) or who, raw.get("same_as"), start[11:16], clean_text(record.get("doing"), 30))
            if record is not None and who == "master":
                at_m = parse_time(record.get("start"))
                stop_m = parse_time(record.get("end")) if record.get("end") else None
                if at_m is not None and (stop_m is None or stop_m > flat_now - datetime.timedelta(minutes=FREE_SHIFT_MINUTES)):
                    if record.get("at_home") is False or (kind == "activity" and record.get("at_home") is True):
                        self._clear_future_step_for_master_shift(max(at_m, flat_now), flat_now, clean_text(record.get("doing") or record.get("place"), 30))
            if record is not None and kind == "activity" and who in bots:
                records.append(record)
                stop = parse_time(record.get("end")) if record.get("end") else None
                if moved:
                    self._finish_same_as_move(record, moved, raws, umo, now, bots, flat_now)
                elif stop is None or stop > flat_now - datetime.timedelta(minutes=FREE_SHIFT_MINUTES):
                    # 补记很早以前的事不算；记的是现在、刚才的事才算改了她的现在
                    self._touch(who, parse_time(record.get("start")))
            if record is not None and kind == "event" and who in bots and self._upset(raw):
                # 主人训她、生她气：记入 upset_events 供 _apply_master_mood 做严格时间戳校对的 legacy 兼容，
                # 避免批内先吵后和好时把旧 upset 倒推覆盖回「刚发火」
                at = parse_time(record.get("start"))
                if at is not None and at >= self._since(items, now) - datetime.timedelta(minutes=FREE_SHIFT_MINUTES):
                    self.batch.upset_events.append({
                        "who": who,
                        "reason": clean_text(record.get("doing") or raw.get("doing"), 60),
                        "at": at,
                        "start": str(record.get("start") or ""),
                    })
                    if self._should_touch_legacy_upset(who, at, now, bots):
                        logger.info("[super_enhance/world] 记录员记了%s的 %s「%s」，影响心情：后面排好的撤掉重排",
                                    self.label(who) or who, record.get("id"), clean_text(record.get("doing"), 30))
                        self._touch(who, max(at, flat_now))
        # 记录员写了她和对方一起、却只给她记了：两人本来就在一起的，给对方也记上
        self._pair_up(records, umo, now)

    @staticmethod
    def _upset(raw: dict) -> bool:
        """只认记录员显式标注的结构化 upset 字段，不再用正则猜测中文句子。"""
        val = raw.get("upset")
        return val is True or str(val or "").strip().lower() == "true"

    def _master_mood_prompt_line(self, bot: str, now: datetime.datetime) -> str:
        """向记录员提供当前主人情绪基线，便于慢通道准确校准。"""
        companion = getattr(self, "companionship", None)
        if companion is None:
            return ""
        bots = list(self.raw_log.enabled_bots())
        mv = companion.safe("mood_view_for", bot, now, bots=bots, default=None)
        if not isinstance(mv, dict):
            return ""
        owner_lvl = int(mv.get("owner_level") or 0)
        self_lvl = int(mv.get("self_level") or 0)
        self_phase = str(mv.get("self_phase") or "")
        self_reason = str(mv.get("self_reason") or "")
        owner_reason = str(mv.get("owner_reason") or "")
        parts = []
        if owner_lvl != 0:
            parts.append("主人自身/外部=%d档%s" % (owner_lvl, "（%s）" % owner_reason if owner_reason else ""))
        else:
            parts.append("主人自身/外部=0档（平常）")
        if self_lvl > 0:
            parts.append("对%s=%d档%s%s" % (
                self.label(bot) or bot,
                self_lvl,
                "（缓和中）" if self_phase == "easing" else "（气头上）",
                "「%s」" % self_reason if self_reason else "",
            ))
        else:
            parts.append("对%s=0档（平常）" % (self.label(bot) or bot))
        return "当前主人情绪记录（供校准参考）：" + "；".join(parts)

    def _batch_mentions_peer_aware(self, bot: str, batch: list[dict], bots: set[str]) -> bool:
        """判断本批对话或条目是否体现了两人同场或通过 tell/转告让另一位室友知情。"""
        others = [b for b in bots if b and b != bot]
        if not others:
            return False
        # 1) 本批写入的条目里有同时牵涉另一位室友和主人的段
        for rec in self.batch.written:
            w = [str(x) for x in (rec.get("with") or [])]
            if any(o in w for o in others) and ("master" in w or str(rec.get("who") or "") == "master"):
                return True
        # 2) 本批对话原文里显式提到转告/tell 给另一人
        other_labels = [self.label(o) or o for o in others]
        for item in batch:
            txt = strip_inline_system(str(item.get("text") or ""))
            if not txt:
                continue
            if "tell" in txt.lower():
                return True
            if any(lbl and lbl in txt for lbl in other_labels) and any(
                k in txt for k in ("告诉", "转告", "跟她说", "和她说", "也在", "一起听", "都在")
            ):
                return True
        return False

    def _fallback_mood_transition(self, res: dict, now: datetime.datetime, bots: set[str]) -> None:
        """无 service 回调时的内置兜底：截停现行欢脱 step 段并撤排未来段。"""
        flat = now.replace(tzinfo=None)
        need_int = bool(res.get("need_interrupt"))
        need_replan = bool(res.get("need_replan"))
        if not need_int and not need_replan:
            return
        affected = [str(b) for b in (res.get("affected_bots") or []) if str(b) in bots]
        if not affected:
            affected = sorted(bots)
        for who in affected:
            if need_int:
                cur = self.world.current(who, flat)
                if is_interruptible_step_entry(cur, flat):
                    self.world.interrupt(cur, flat, by="主人生气了，顾不上继续")
                    logger.info(
                        "[super_enhance/world] 主人生气升级，截停%s现行段 %s「%s」",
                        self.label(who) or who,
                        cur.get("id"),
                        clean_text(cur.get("doing"), 24),
                    )
            self.world.clear_planned(who, flat)
            self._touch(who, flat)

    def _event_ts(self, value) -> float:
        """把事件自身的时刻（datetime 或 YYYY-MM-DD HH:MM）按本地时区转为 unix ts；无法解析返回 0.0。"""
        dt = value if isinstance(value, datetime.datetime) else parse_time(value)
        if dt is None:
            return 0.0
        if dt.tzinfo is None and self.tz is not None:
            dt = dt.replace(tzinfo=self.tz)
        try:
            return float(dt.timestamp())
        except Exception:
            return 0.0

    def _should_touch_legacy_upset(self, who: str, at: datetime.datetime, now: datetime.datetime, bots: set[str]) -> bool:
        """已有比该 upset 事件更晚的情绪记录（例如已和好）时，不再无谓撤排未来世界线。"""
        companion = getattr(self, "companionship", None)
        if companion is None:
            return True
        evt_ts = self._event_ts(at)
        if evt_ts <= 0.0:
            return False
        snap = companion.safe("get_master_mood", now, default={}) or {}
        row = (snap.get("per_bot") or {}).get(who) if isinstance(snap, dict) else None
        inc = (snap.get("incidents") or {}).get(who) if isinstance(snap, dict) else None
        existing_ts = max(
            float((row or {}).get("msg_ts") or 0.0),
            float((inc or {}).get("resolved_ts") or 0.0),
        )
        if existing_ts >= evt_ts:
            return False
        return True

    def _apply_master_mood(
        self,
        umo: str,
        comp_raw: dict | None,
        batch: list[dict],
        now: datetime.datetime,
        bots: set[str],
    ) -> None:
        """慢通道情绪校准：msg_ts 取本批最后一条主人消息时间戳，is_calibration=True 防纠偏误锁 easing，
        严格以事件自身时刻做 legacy upset:true 兼容防和好后倒流，并在 need_interrupt / need_replan 时联动截停与撤排。"""
        companion = getattr(self, "companionship", None)
        if companion is None:
            return
        bot = bot_of_umo(umo)
        if not bot or bot not in bots:
            return
        # 非目标私聊不写主人情绪
        if hasattr(companion, "_target") and not companion._target(umo):
            return
        master_ts_list = []
        for item in batch:
            if str(item.get("role") or "") != "user":
                continue
            if not strip_inline_system(str(item.get("text") or "")).strip():
                continue
            try:
                ts_val = float(item.get("ts") or 0.0)
            except Exception:
                ts_val = 0.0
            if ts_val > 0:
                master_ts_list.append(ts_val)
        if not master_ts_list:
            return
        msg_ts = max(master_ts_list)
        min_master_ts = min(master_ts_list)
        bot_list = sorted(bots)
        mv_before = companion.safe("mood_view_for", bot, now, bots=bot_list, default={}) or {}
        existing_self_lvl = int(mv_before.get("self_level") or 0)

        # 物理同场或对话提及 tell/同场
        phys_or_mentioned = infer_peer_aware(self, bot, now, bots=bot_list) or self._batch_mentions_peer_aware(bot, batch, bots)
        # 仅在异步快裁判开启时走慢校准（防纠偏误锁 easing）；快裁判关闭时记录员本身是主通道，必须正常推导 easing 与翻篇余波
        is_cal = bool(self.cfg.get("mood_judge_enabled", False))
        transitions: list[dict] = []
        covered_bots: set[str] = set()
        any_mood_changed = False

        mm = comp_raw.get("master_mood") if isinstance(comp_raw, dict) else None
        if isinstance(mm, dict):
            target = str(mm.get("target") or "none").strip().lower()
            raw_pa = mm.get("peer_aware")
            explicit_pa = raw_pa is True or str(raw_pa or "").strip().lower() == "true"
            explicit_pa_false = raw_pa is False or str(raw_pa or "").strip().lower() == "false"
            if target in ("self", "both", "owner"):
                try:
                    level = int(mm.get("level"))
                except (TypeError, ValueError):
                    level = None
                if level is not None:
                    reason = clean_text(mm.get("reason"), 80)
                    heard_by: list[str] | None = None
                    if target == "owner":
                        level = max(-1, min(3, level))
                        scope = "owner"
                        peer_aware = None
                        heard_by = [] if level == 0 else compute_owner_heard_by(
                            self, bot, now, bots=bot_list, peer_present=bool(explicit_pa or phys_or_mentioned)
                        )
                    else:
                        if level < 0:
                            level = 0
                        level = max(0, min(3, level))
                        scope = "both" if target == "both" else bot
                        if target == "both":
                            peer_aware = True
                            covered_bots.update(bot_list)
                        else:
                            covered_bots.add(bot)
                            if level > 0:
                                if explicit_pa or phys_or_mentioned:
                                    peer_aware = True
                                elif explicit_pa_false and existing_self_lvl == 0:
                                    peer_aware = False
                                elif existing_self_lvl == 0:
                                    peer_aware = False
                                else:
                                    peer_aware = None
                            else:
                                peer_aware = None
                    update_kwargs = dict(
                        scope=scope,
                        level=level,
                        reason=reason,
                        msg_ts=msg_ts,
                        source="recorder",
                        now=now,
                        peer_aware=peer_aware,
                        is_calibration=is_cal,
                        bots=bot_list,
                    )
                    if scope == "owner":
                        update_kwargs["heard_by"] = heard_by
                    res = call_update_master_mood(companion, **update_kwargs)
                    if isinstance(res, dict):
                        transitions.append(res)
                        if res.get("changed"):
                            any_mood_changed = True
                    if target == "self" and level > 0 and (explicit_pa or phys_or_mentioned):
                        if companion.safe("mark_peer_aware", bot, aware=True, now=now, default=False):
                            any_mood_changed = True
            elif existing_self_lvl > 0 and (explicit_pa or self._batch_mentions_peer_aware(bot, batch, bots)):
                # 档位没变，但本批里另一人通过 tell / 同场获知了正在进行的冲突
                if companion.safe("mark_peer_aware", bot, aware=True, now=now, default=False):
                    any_mood_changed = True

        # 兼容老格式 upset: true：严格以该条 event 自身时刻作为 msg_ts（绝不用整批最后一条 msg_ts），
        # 若该 bot 已有更新的 msg_ts / 和好记录 / 本批已被 master_mood 或新裁判处理过，直接跳过防和好后倒流。
        snap_now = companion.safe("get_master_mood", now, default={}) or {}
        per_bot_map = snap_now.get("per_bot") if isinstance(snap_now, dict) else {}
        inc_map = snap_now.get("incidents") if isinstance(snap_now, dict) else {}
        for evt in self.batch.upset_events:
            who = str(evt.get("who") or "")
            if not who or who not in bots or who in covered_bots:
                continue
            evt_ts = self._event_ts(evt.get("at") or evt.get("start"))
            if evt_ts <= 0.0:
                continue
            row = (per_bot_map or {}).get(who) if isinstance(per_bot_map, dict) else None
            inc = (inc_map or {}).get(who) if isinstance(inc_map, dict) else None
            existing_msg_ts = float((row or {}).get("msg_ts") or 0.0)
            resolved_ts = float((inc or {}).get("resolved_ts") or 0.0)
            if max(existing_msg_ts, resolved_ts) >= evt_ts:
                continue
            # 若新裁判已处理过本批该 bot 消息，跳过 legacy 映射
            if str((row or {}).get("source") or "") == "judge" and existing_msg_ts >= min_master_ts - 5.0:
                continue
            cur_mv = companion.safe("mood_view_for", who, now, bots=bot_list, default={}) or {}
            cur_lvl = int(cur_mv.get("self_level") or 0)
            target_lvl = max(2, cur_lvl)
            pa = True if (infer_peer_aware(self, who, now, bots=bot_list) or self._batch_mentions_peer_aware(who, batch, bots)) else (False if cur_lvl == 0 else None)
            res = call_update_master_mood(
                companion,
                scope=who,
                level=target_lvl,
                reason=str(evt.get("reason") or ""),
                msg_ts=evt_ts,
                source="recorder",
                now=now,
                peer_aware=pa,
                is_calibration=is_cal,
                bots=bot_list,
            )
            if isinstance(res, dict):
                transitions.append(res)
                if res.get("changed"):
                    any_mood_changed = True
            covered_bots.add(who)

        handler = getattr(self, "on_mood_transition", None)
        transition_dispatched = False
        for res in transitions:
            if not (res.get("need_interrupt") or res.get("need_replan")):
                continue
            transition_dispatched = True
            if callable(handler):
                try:
                    handler(res, now)
                except Exception:
                    logger.warning("[super_enhance/world] 记录员触发情绪联动截停失败", exc_info=True)
            else:
                self._fallback_mood_transition(res, now, bots)

        if any_mood_changed and not transition_dispatched:
            notifier = getattr(self, "on_mood_changed", None)
            if callable(notifier):
                try:
                    notifier()
                except Exception:
                    pass

    def _apply_constraints(self, data: dict, umo: str, now: datetime.datetime, batch: list) -> None:
        """记新规矩、撤规矩。谁的规矩变了，两个人后面补写排的、还没开始的段都撤掉，由补写按新规矩重排
        （不然已经排好的段照旧走，比如罚站「什么都不许干」定下以后，之前排好的一起吃午饭照样开吃；
        另一个人排的段也会牵着她，比如丛雨排着「把午饭端到门口，招呼亚托莉一块吃」，补写就顺着给她排上一起吃）。"""
        changed = False
        flat = now.replace(tzinfo=None, second=0, microsecond=0)
        home_scene = str(self.cfg.get("world_home") or "").strip()
        for raw in data.get("constraints") or []:
            if not isinstance(raw, dict):
                continue
            # 记过的规矩又记一遍：add_constraint 并到原来那条上，这里不当新规矩（不然每回都撤掉排好的段重排）
            existed = self.world.same_constraint(str(raw.get("who") or "").strip(), raw.get("text"), scene=home_scene)
            before = (existed.get("until"), existed.get("until_time")) if existed is not None else None
            added = self.world.add_constraint({
                "who": raw.get("who"),
                "text": raw.get("text"),
                "public": raw.get("public"),
                "scene": home_scene,
                "until": raw.get("until"),
                "until_time": raw.get("until_time"),
                "since": norm_time(
                    raw.get("since"),
                    (batch[0].get("date") if batch else now.date().isoformat()),
                    fallback=fmt_time(now),
                )
                or fmt_time(now),
                "source": "chat",
                "src_umo": umo,
            })
            if added and existed is not None:
                if before != (added.get("until"), added.get("until_time")):
                    logger.info("[super_enhance/world] 记录员改了%s的规矩 %s 的期限：%s", self.label(added["who"]) or added["who"],
                                added["id"], clean_text(added.get("until") or added.get("until_time"), 40))
                    changed = changed or added["who"] in set(self.raw_log.enabled_bots())
                continue
            if added:
                logger.info("[super_enhance/world] 记录员记了%s的规矩 %s：%s", self.label(added["who"]) or added["who"],
                            added["id"], clean_text(added["text"], 40))
                changed = changed or added["who"] in set(self.raw_log.enabled_bots())
        rules = {str(x.get("id")): x for x in self.world.constraints() or []} if data.get("lift_constraints") else {}
        for cid in data.get("lift_constraints") or []:
            if self.world.lift_constraint(str(cid), note="对话里解除"):
                old = rules.get(str(cid)) or {}
                logger.info("[super_enhance/world] 记录员撤了%s的规矩 %s：%s", self.label(old.get("who")) or old.get("who"), cid,
                            clean_text(old.get("text"), 40))
                changed = changed or str(old.get("who") or "") in set(self.raw_log.enabled_bots())
        if changed:
            for bot in self.raw_log.enabled_bots():
                self._touch(bot, flat)

    def _apply_intentions(self, data: dict, umo: str, now: datetime.datetime, bots: set,
                          flat_now: datetime.datetime, batch=None) -> None:
        # 先撤掉过时的打算（按编号），再记这次新说的；
        # 当天未过期且本轮无主人亲口发言时，保护主人确认的约定/带 master 打算防误撤；
        # 若已跨天过期，或本轮包含主人亲口发言（取消/改口/确认已做完），正常允许撤回避免永久僵尸死锁。
        companion = getattr(self, "companionship", None)
        existing_map = {str(x.get("id")): x for x in self.world.intentions()}
        has_master = any(str(x.get("role") or "") == "user" for x in (batch or []))
        today_str = flat_now.date().isoformat()
        for token in data.get("drop_intentions") or []:
            token = str(token or "").strip()
            if not token:
                continue
            if re.match(r"^i\d+$", token):
                old_int = existing_map.get(token) or {}
                int_date = str(old_int.get("date") or "")[:10]
                is_past_expired = bool(int_date and int_date < today_str)
                if not has_master and not is_past_expired:
                    if companion is not None and companion.safe("is_protected_intent", token, default=False):
                        logger.info("[super_enhance/world] 保护主人已确认的约定/拍板 %s，忽略无主人发言时的自动撤回", token)
                        continue
                    if "master" in (old_int.get("with") or []):
                        logger.info("[super_enhance/world] 保护与主人绑定的打算 %s，忽略无主人发言时的自动撤回", token)
                        continue
                self.world.drop_intention(token)
            else:
                # 编号对不上就不动（宁可少撤一条，也不误清这个人的全部打算）
                logger.warning("[super_enhance/world] drop_intentions 收到无法识别的编号：%s，忽略", token)
        known = {str(x.get("id")) for x in self.world.intentions()}
        heard = bot_of_umo(umo)
        for raw in data.get("intentions") or []:
            if not isinstance(raw, dict):
                continue
            who = str(raw.get("who") or "")
            together = [str(x) for x in (raw.get("with") or []) if str(x) == "master"]
            same_as = str(raw.get("same_as") or "").strip()
            covered = None
            same_as_invalid = False
            if same_as and who in bots:
                covered = self.batch.moved_map.get((who, same_as)) or self._movable_step_ref(same_as, who, flat_now)
                if covered is None:
                    same_as_invalid = True
                    logger.warning(
                        "[super_enhance/world] %s 的打算「%s」填了 same_as=%s 但找不到对应进行中/未开始补写段，仅记打算不设 now_at、不触发撤排",
                        self.label(who) or who, clean_text(raw.get("text"), 30), same_as,
                    )
            if covered is not None:
                cov_day, cov_entry = covered
                if cov_entry.get("intent"):
                    logger.info(
                        "[super_enhance/world] %s 的打算「%s」已由段 %s（已挂打算 %s）覆盖（same_as），跳过重复建打算",
                        self.label(who) or who, clean_text(raw.get("text"), 30), cov_entry.get("id"), cov_entry.get("intent"),
                    )
                    continue
                record = self.world.set_intention(
                    who, raw.get("text"), raw.get("window") or "", "chat",
                    with_list=together, date=raw.get("date") or "",
                    heard_by=[heard] if who == "master" and heard else None, now_at="", why=raw.get("why") or "",
                )
                companion = getattr(self, "companionship", None)
                if companion is not None:
                    companion.safe("bind_plan", raw, record)
                if record:
                    self.world.update(cov_day, str(cov_entry.get("id")), intent=str(record.get("id")))
                logger.info(
                    "[super_enhance/world] %s 的打算「%s」已由段 %s 覆盖（same_as），不打断也不撤排",
                    self.label(who) or who, clean_text(raw.get("text"), 30), cov_entry.get("id"),
                )
                continue
            now_at = ""
            if raw.get("now") and who in bots and not same_as_invalid:
                # 答应现在就去做的：从她答应的那一刻起排（说不清就按现在；不会晚于现在）
                moment = parse_time(norm_time(raw.get("since"), flat_now.date().isoformat(), fallback=fmt_time(now)))
                now_at = fmt_time(min(moment, flat_now) if moment is not None else flat_now)
            record = self.world.set_intention(
                who, raw.get("text"), raw.get("window") or "", "chat",
                with_list=together, date=raw.get("date") or "",
                heard_by=[heard] if who == "master" and heard else None, now_at=now_at, why=raw.get("why") or "",
            )
            companion = getattr(self, "companionship", None)
            if companion is not None:
                companion.safe("bind_plan", raw, record)
            # 只有新记的才腾地方；同一条再说一遍不动（补写可能已经把它排进去了）；主人自己的安排不腾她们的地方
            if record and str(record.get("id")) not in known and who != "master" and not same_as_invalid:
                if record.get("now_at"):
                    self._go_now(record, now, same_as=same_as)
                else:
                    self._make_room(record, now)

    def _make_room(self, intention: dict, now: datetime.datetime) -> None:
        """新记了一条带时间段的打算：她那段时间里补写排好的事让开——没开始的撤掉，跨进来的截到打算开始的那一刻；
        对方写着和她一起的那几段也一样。空出来的由补写把打算排进去（顺带上对方）。
        时间段太长（超过 MAKE_ROOM_HOURS）或已经过了的不动，免得撤掉一大片。"""
        scheduled = parse_time(str(intention.get("date") or "") + " 00:00")
        reference = now.replace(year=scheduled.year, month=scheduled.month, day=scheduled.day) if scheduled else now
        span = window_span(intention.get("window"), reference)
        if span is None:
            return
        flat = now.replace(tzinfo=None)
        lo, hi = max(span[0], flat), span[1]
        if hi <= lo or hi - lo > datetime.timedelta(hours=MAKE_ROOM_HOURS):
            return
        who = str(intention.get("who") or "")
        bots = set(self.raw_log.enabled_bots())
        done = []
        for date_text in sorted({lo.date().isoformat(), hi.date().isoformat()}):
            for item in list(self.world.entries(date_text, "")):
                owner = str(item.get("who") or "")
                if owner not in bots or str(item.get("source") or "") != "step" or is_event(item):
                    continue
                if owner != who and who not in (item.get("with") or []):
                    continue
                if str(item.get("intent") or "") == str(intention.get("id")):
                    continue
                start = parse_time(item.get("start"))
                end = parse_time(item.get("end")) if item.get("end") else None
                if start is None or end is None or not (start < hi and end > lo):
                    continue
                if is_sleep(item) and start <= flat:
                    # 睡觉不打断：已经睡下的那段照旧睡到起床，打算等她起来再排
                    done.append("%s 已经睡下，不动" % item.get("id"))
                    continue
                changed = self.world.interrupt(
                    item, lo, by="到了说好去做的事：%s" % clean_text(intention.get("text"), 30)
                )
                if changed is None:
                    continue
                if start < lo:
                    done.append("%s %s 到 %s 为止" % (item.get("id"), clean_text(item.get("doing"), 20), fmt_time(lo)[11:]))
                else:
                    done.append("%s %s 撤掉" % (item.get("id"), clean_text(item.get("doing"), 20)))
        if done:
            logger.info("[super_enhance/world] 新打算 %s（%s %s）腾地方：%s", intention.get("id"),
                        self.label(who) or who, intention.get("window"), "；".join(done))

    def _touch(self, who: str, moment) -> None:
        """记下这次改到了她的现在、从几点起（取最早的）：apply 最后据此把她后面补写排的段撤掉重排。"""
        if moment is None or who not in set(self.raw_log.enabled_bots()):
            return
        touched = self.batch.touched
        old = touched.get(who)
        touched[who] = moment if old is None else min(old, moment)

    def _go_now(self, intention: dict, now: datetime.datetime, same_as: str = "") -> None:
        """新记了一条「现在就去」的打算：那件事交给补写马上排。她手上那段要是补写排的、记录员这次没定怎么收场（多半是闲着），
        到她答应的那一刻为止；算改了她的现在，apply 最后把她后面补写排的段撤掉。已经睡下的觉不动。
        若结构化 same_as 指向当前/未来仍在排的补写段，说明补写已经覆盖该打算，不打断也不撤排。"""
        who = str(intention.get("who") or "")
        flat = now.replace(tzinfo=None)
        same_as_id = str(same_as or intention.get("same_as") or "").strip()
        if same_as_id:
            covered = self._movable_step_ref(same_as_id, who, flat)
            if covered is not None:
                cov_day, cov_entry = covered
                if intention.get("id") and not cov_entry.get("intent"):
                    self.world.update(cov_day, str(cov_entry.get("id")), intent=str(intention.get("id")))
                logger.info(
                    "[super_enhance/world] %s 的 now 打算「%s」已由补写段 %s 覆盖（same_as），不打断也不撤排",
                    self.label(who) or who, clean_text(intention.get("text"), 30), cov_entry.get("id"),
                )
                return
        at = parse_time(intention.get("now_at")) or flat
        if self.world.sleep_at(who, at) is not None:
            logger.info("[super_enhance/world] %s答应现在就去做「%s」，可她已经睡下了，睡觉不打断，只记打算",
                        self.label(who) or who, clean_text(intention.get("text"), 30))
            return
        cur = self.world.current(who, at)
        if cur is not None and not is_event(cur) and str(cur.get("source") or "") == "step" \
                and str(cur.get("id")) not in self.batch.mentioned:
            entry_id = str(cur.get("id"))
            self.world.interrupt(cur, at, by="改去%s" % clean_text(intention.get("text"), 30))
            logger.info("[super_enhance/world] %s答应现在就去做「%s」：手上补写排的 %s %s 到 %s 为止",
                        self.label(who) or who, clean_text(intention.get("text"), 30), entry_id,
                        clean_text(cur.get("doing"), 20), fmt_time(at)[11:])
        self._touch(who, at)

    def _joint_entry(self, other: str, who: str, start, end, steps: bool = False, near: bool = False, place: str = ""):
        """对方那边和她同一时候、写着和她一起的段；没有返回 None。steps=False 时补写排的不算（那些会撤掉重排）；
        near=True 时只算同一件事：差不多同一时候开始的，或者（给了 place）就在同一个地方的
        （两人本来一起在足汤、她说两人往回走了，对方那段足汤就不算）。"""
        stop = end or (start + datetime.timedelta(minutes=1))
        for day in sorted({start.date().isoformat(), stop.date().isoformat()}):
            for item in self.world.entries(day, other):
                if is_event(item) or who not in (item.get("with") or []):
                    continue
                if not steps and str(item.get("source") or "") == "step":
                    continue
                s0 = parse_time(item.get("start"))
                s1 = parse_time(item.get("end")) if item.get("end") else None
                if s0 is None or not (s0 < stop and (s1 is None or s1 > start)):
                    continue
                if near and abs((s0 - start).total_seconds()) > PAIR_NEAR_MINUTES * 60 \
                        and not (place and same_place(place, item.get("place"))):
                    continue
                return item
        return None

    def _own_entry(self, other: str, who: str, start, end):
        """对方自己那边同一时候记着的、没写和她一起的事（聊天里记的、手动写的，补写排的不算）；没有返回 None。"""
        stop = end or (start + datetime.timedelta(minutes=1))
        for day in sorted({start.date().isoformat(), stop.date().isoformat()}):
            for item in self.world.entries(day, other):
                if is_event(item) or str(item.get("source") or "") == "step" or who in (item.get("with") or []):
                    continue
                s0 = parse_time(item.get("start"))
                s1 = parse_time(item.get("end")) if item.get("end") else None
                if s0 is not None and s0 < stop and (s1 is None or s1 > start):
                    return item
        return None

    def _pair_up(self, records, umo: str, now: datetime.datetime) -> None:
        """记录员写了她和对方一起（with 里有对方），却没给对方记：两人本来就在一起的（对方那会儿手上那段写着和她一起），
        照她这条给对方也记一条（时候、地点一样，doing 换成对方的口吻），对方手上那段到这条开始时为止。
        对方那会儿在做别的的不管，最后由 _unpair 把对方从她这条里去掉。"""
        bots = set(self.raw_log.enabled_bots())
        for record in records:
            who = str(record.get("who") or "")
            start = parse_time(record.get("start"))
            end = parse_time(record.get("end")) if record.get("end") else None
            if start is None:
                continue
            for other in [x for x in (record.get("with") or []) if x in bots and x != who]:
                if self._joint_entry(other, who, start, end, near=True) is not None or self._own_entry(other, who, start, end) is not None:
                    continue  # 对方已经有一条和她一起的；或者对方自己那边记着别的事（照对方的，最后由 _unpair 去掉「一起」）
                before = self.world.current(other, start - datetime.timedelta(minutes=1))
                if before is None or is_event(before) or who not in (before.get("with") or []) \
                        or str(before.get("source") or "") not in ("step", "chat") or str(before.get("id")) in self.batch.mentioned:
                    continue
                name, mine = self.label(who) or who, self.label(other) or other
                doing = str(record.get("doing") or "")
                doing = doing.replace(mine, name) if mine and mine in doing else "和%s一起%s" % (name, doing)
                b_end = parse_time(before.get("end")) if before.get("end") else None
                if b_end is None or b_end > start:
                    self.world.interrupt(before, start, by="改去%s" % clean_text(doing, 30))
                seen = sorted((set(record.get("seen_by") or []) | {who}) - {other})
                twin = self.world.add_entry(str(record.get("start"))[:10], {
                    "who": other, "kind": "activity", "start": record.get("start"), "end": record.get("end") or "",
                    "place": record.get("place"), "at_home": record.get("at_home"), "doing": doing, "public": record.get("public"),
                    "with": [who] + [x for x in (record.get("with") or []) if x == "master"], "seen_by": seen,
                    "source": "chat", "src_umo": umo, "src_quote": record.get("src_quote"), "created": fmt_time(now),
                })
                if twin is not None:
                    self._touch(other, start)
                    logger.info("[super_enhance/world] 记录员写了%s和%s一起，只记了她那边：给%s也记上 %s %s–%s %s",
                                name, mine, mine, twin.get("id"), str(twin.get("start"))[11:16],
                                str(twin.get("end") or "")[11:16], clean_text(doing, 30))

    def _still_with(self, who: str, other: str, since, until) -> bool:
        """since 到 until 这段时间里，她的世界线一直写着和对方一起（中间没断、没换成一个人去做别的）。"""
        moment = since
        for _ in range(24):
            if moment >= until:
                return True
            cur = self.world.current(who, moment)
            if cur is None or other not in (cur.get("with") or []):
                return False
            end = parse_time(cur.get("end")) if cur.get("end") else None
            if end is None or end <= moment:
                return True
            moment = end
        return True

    def _redo_after(self, who: str, since, now: datetime.datetime) -> None:
        """这次改到了她的现在（记了她现在的事、手上那段收了场、答应了现在就去做）：她 since 以后补写排的段全撤掉，
        由补写按新的情况从头排。另一个人补写排的、写着和她一起的段：还没开始的撤掉，对方从那一段起往后补写排的也撤掉
        （那些是接着两人一起的事排的）；正在做的，她真走了（改完以后她那边不再一直写着和对方一起）只把她去掉、
        写一句她中途走了，对方照旧接着做。手动写的、聊天里记的、这次刚记的、已经睡下的觉都不动。"""
        flat = now.replace(tzinfo=None)
        bots = set(self.raw_log.enabled_bots())
        fresh = self.batch.fresh
        name = self.label(who) or who
        removed, freed = [], []
        days = sorted({since.date().isoformat(), flat.date().isoformat(), (flat.date() + datetime.timedelta(days=1)).isoformat()})

        def movable():
            """补写排的、这次能撤的段：(哪天, 段, 谁, 编号, 开始)。"""
            for day in days:
                for item in list(self.world.entries(day, "")):
                    owner, iid = str(item.get("who") or ""), str(item.get("id") or "")
                    if owner not in bots or str(item.get("source") or "") != "step" or is_event(item):
                        continue
                    if iid in self.batch.mentioned or iid in fresh:
                        continue
                    start = parse_time(item.get("start"))
                    if start is None or (is_sleep(item) and start <= flat):
                        continue
                    yield day, item, owner, iid, start

        cutoff = {}
        for day, item, owner, iid, start in movable():
            end = parse_time(item.get("end")) if item.get("end") else None
            if owner == who:
                if start >= since:
                    for partner in (item.get("with") or []):
                        if partner in bots and partner != who:
                            cutoff[partner] = min(cutoff.get(partner, start), start)
                    self.world.delete_entry(day, iid)
                    removed.append("%s %s" % (iid, clean_text(item.get("doing"), 16)))
                continue
            if who not in (item.get("with") or []):
                continue
            if start >= since:
                cutoff[owner] = min(cutoff.get(owner, start), start)
            elif (end is None or end > since) and not self._still_with(
                    who, owner, since, end or since + datetime.timedelta(hours=1)):
                note = "（%s中途走了）" % name
                doing = str(item.get("doing") or "")
                self.world.update(day, iid, doing=doing if note in doing else doing + note,
                                        **{"with": [x for x in (item.get("with") or []) if x != who]})
                self._drop_partner_from_stopped(day, who, owner, start, end or since)
                freed.append("%s的 %s 只去掉%s" % (self.label(owner) or owner, iid, name))
        # 对方和她一起的段还没开始：对方从那段起往后补写排的都撤掉重排
        for day, item, owner, iid, start in movable():
            if owner in cutoff and start >= cutoff[owner]:
                self.world.delete_entry(day, iid)
                freed.append("%s的 %s 撤掉" % (self.label(owner) or owner, iid))
        if removed or freed:
            logger.info("[super_enhance/world] %s的现在改了（%s 起）：后面补写排的撤掉重排：%s%s", name, fmt_time(since)[11:],
                        "；".join(removed) or "（没有）", "；对方：" + "；".join(freed) if freed else "")

    def _clear_future_step_for_master_shift(self, since: datetime.datetime, now: datetime.datetime, reason: str = "") -> None:
        """主人离家/回家等动向变化时，撤掉两只 bot 尚未开始的 step 预排段，避免继续按主人在场写互动或等门。"""
        flat = now.replace(tzinfo=None)
        bots = set(self.raw_log.enabled_bots())
        fresh = self.batch.fresh
        days = sorted({since.date().isoformat(), flat.date().isoformat(), (flat.date() + datetime.timedelta(days=1)).isoformat()})
        removed = []
        for day in days:
            for item in list(self.world.entries(day, "")):
                owner, iid = str(item.get("who") or ""), str(item.get("id") or "")
                if owner not in bots or str(item.get("source") or "") != "step" or is_event(item):
                    continue
                if iid in self.batch.mentioned or iid in fresh:
                    continue
                start = parse_time(item.get("start"))
                if start is None or start <= flat or start < since or is_sleep(item):
                    continue
                self.world.delete_entry(day, iid)
                removed.append("%s:%s" % (self.label(owner) or owner, iid))
        if removed:
            logger.info("[super_enhance/world] 主人动向变化（%s），撤掉 %d 段未开始的补写预排：%s",
                        reason or fmt_time(since)[11:16], len(removed), "、".join(removed[:8]))

    def _drop_partner_from_stopped(self, day: str, who: str, partner: str,
                                   start: datetime.datetime, end: datetime.datetime) -> None:
        """当把 partner 从 ongoing 段里去掉 who 时，同步清理 who 同一时段已 stopped 的 step 段里的 partner，避免遗留单边 with。"""
        for item in list(self.world.entries(day, who)):
            if is_event(item) or str(item.get("source") or "") != "step" or not item.get("stopped"):
                continue
            wlist = list(item.get("with") or [])
            if partner not in wlist:
                continue
            s2 = parse_time(item.get("start"))
            e2 = parse_time(item.get("end"))
            if s2 is None or e2 is None or max(start, s2) >= min(end, e2):
                continue
            self.world.update(day, str(item.get("id")), **{"with": [x for x in wlist if x != partner]})

    def _unpair(self, now: datetime.datetime) -> None:
        """两人「一起」两边要对得上：聊天里记的、还没结束的段写着和对方一起，对方那边同一时候却没有写着和她一起的段
        （对方在做别的），就照对方的来：把对方从这段里去掉，doing 里「和对方」那几个字也去掉。"""
        flat = now.replace(tzinfo=None)
        bots = set(self.raw_log.enabled_bots())
        for day in sorted({(flat.date() - datetime.timedelta(days=1)).isoformat(), flat.date().isoformat()}):
            for item in list(self.world.entries(day, "")):
                who = str(item.get("who") or "")
                if who not in bots or is_event(item) or str(item.get("source") or "") != "chat":
                    continue
                start = parse_time(item.get("start"))
                end = parse_time(item.get("end")) if item.get("end") else None
                if start is None or (end is not None and end <= flat - datetime.timedelta(minutes=FREE_SHIFT_MINUTES)):
                    continue
                gone = [x for x in (item.get("with") or []) if x in bots and x != who
                        and self._joint_entry(x, who, start, end, steps=True, near=True, place=str(item.get("place") or "")) is None]
                if not gone:
                    continue
                doing = str(item.get("doing") or "")
                for other in gone:
                    doing = re.sub(r"(和|跟|与|同)%s(一起|一道|一块儿?)?[，,、]?" % re.escape(self.label(other) or other), "", doing)
                self.world.update(day, str(item.get("id")), doing=doing.strip("，, ") or str(item.get("doing") or ""),
                                        **{"with": [x for x in (item.get("with") or []) if x not in gone]})
                logger.info("[super_enhance/world] %s的 %s 写着和%s一起，对方那边同一时候在做别的：照对方的，去掉「一起」",
                            self.label(who) or who, item.get("id"), "、".join(self.label(x) or x for x in gone))
                # 对方那边原来写着和她一起、这会儿还在接着的段：她已经走了，只把她去掉、写一句她中途走了
                for other in gone:
                    self._drop_partner(other, who, start, end)

    def _drop_partner(self, other: str, who: str, start, end) -> None:
        """她 start 起已经不和对方在一起了：对方那边写着和她一起、跟这段时间撞上的段，把她去掉，写一句她中途走了 / 不来了。"""
        name = self.label(who) or who
        stop = end or (start + datetime.timedelta(minutes=1))
        for day in sorted({start.date().isoformat(), stop.date().isoformat()}):
            for item in list(self.world.entries(day, other)):
                if is_event(item) or who not in (item.get("with") or []) or str(item.get("source") or "") not in ("step", "chat"):
                    continue
                s0 = parse_time(item.get("start"))
                s1 = parse_time(item.get("end")) if item.get("end") else None
                if s0 is None or not (s0 < stop and (s1 is None or s1 > start)):
                    continue
                note = "（%s中途走了）" % name if s0 < start else "（%s不来了）" % name
                doing = str(item.get("doing") or "")
                self.world.remove_partner(item, who, doing=doing if note in doing else doing + note)

    def _movable(self, entry, now: datetime.datetime) -> bool:
        """补写排的、还没做完的段（不是睡觉、不是顺带的事）：她提前做了能整段挪过来。"""
        if not entry or str(entry.get("source") or "") != "step" or is_event(entry) or is_sleep(entry) or entry.get("stopped"):
            return False
        end = parse_time(entry.get("end")) if entry.get("end") else None
        return end is None or end > now

    @staticmethod
    def _carry(planned: dict, start: str, end: str) -> dict:
        """挪过来时接着用的：分步（带分钟）、标的吃的、结果、做的哪条打算……；没写结束就按原来的时长往后算。"""
        texts, mins = clean_steps(planned.get("steps")), step_minutes(planned)
        out = {
            "steps": [{"text": t, "minutes": m} for t, m in zip(texts, mins)] if mins else texts,
            "things": [dict(x) for x in planned.get("things") or [] if isinstance(x, dict)],
            "result": planned.get("result") or "",
            "importance": planned.get("importance") or 0,
            "send_msg": bool(planned.get("send_msg")),
            "msg_at": planned.get("msg_at") or "",
            "msg_purpose": planned.get("msg_purpose") or "",
            "msg_kind": planned.get("msg_kind") or "",
            "meal": planned.get("meal"),
            "at_home": planned.get("at_home"),
            "stove": planned.get("stove") or "",
            **({"lamp": True} if planned.get("lamp") else {}),
            **({"kerosene": True} if planned.get("kerosene") else {}),
            **({"water": True} if planned.get("water") else {}),
            **({"outfit": planned["outfit"]} if planned.get("outfit") else {}),
            **({"mood": planned["mood"]} if planned.get("mood") else {}),
            "intent": planned.get("intent") or "",
            "origin": planned.get("origin"),
        }
        if mins:
            out["step_minutes"] = list(mins)
        begin, old_start, old_end = parse_time(start), parse_time(planned.get("start")), parse_time(planned.get("end"))
        if not end and begin is not None and old_start is not None and old_end is not None and old_end > old_start:
            out["end"] = fmt_time(begin + (old_end - old_start))
        return out

    def _by_start_doing(self, who: str, start: str, end: str) -> dict | None:
        """她用「现在去做」自己写的、和这条条目时间撞上的那段（没有返回 None）。end 空着算开口。"""
        begin = parse_time(start)
        stop = parse_time(end) if end else None
        if begin is None:
            return None
        for day in sorted({(begin.date() - datetime.timedelta(days=1)).isoformat(), begin.date().isoformat()}):
            for item in self.world.entries(day, who):
                if item.get("tool") != "start_doing" or is_event(item):
                    continue
                s0 = parse_time(item.get("start"))
                s1 = parse_time(item.get("end")) if item.get("end") else None
                if s0 is not None and (stop is None or s0 < stop) and (s1 is None or s1 > begin):
                    return item
        return None

    def _prescan_same_as(self, raws: list, bots: set, flat_now: datetime.datetime) -> None:
        """在 _apply_stale 之前预扫本批 entries 里合法的 same_as 承接，登记承接人与目标段编号。"""
        for raw in raws:
            if not isinstance(raw, dict) or str(raw.get("kind") or "") == "event":
                continue
            who = str(raw.get("who") or "").strip()
            same_as = str(raw.get("same_as") or "").strip()
            if who not in bots or not same_as:
                continue
            ref = self._movable_step_ref(same_as, who, flat_now)
            if ref is None:
                continue
            _, planned = ref
            planned_partners = {x for x in (planned.get("with") or []) if x in bots and x != who}
            kept_partners = {x for x in (raw.get("with") or []) if x in bots and x != who}
            if planned_partners - kept_partners:
                continue
            self.batch.same_as_movers.add(who)
            self.batch.same_as_targets.add(same_as)
            old_start = parse_time(planned.get("start"))
            old_end = parse_time(planned.get("end")) if planned.get("end") else None
            if old_start is not None:
                for other in planned_partners:
                    self.batch.same_as_movers.add(other)
                    p_planned = self._joint_entry(other, who, old_start, old_end, steps=True, near=True)
                    if self._movable(p_planned, flat_now):
                        self.batch.same_as_targets.add(str(p_planned.get("id") or ""))

    def _movable_step_ref(self, token, who: str, now: datetime.datetime) -> tuple[str, dict] | None:
        """校验结构化 same_as 编号：必须是 who 名下尚未结束的补写段（step），返回 (date_text, entry)。"""
        entry_id = str(token or "").strip()
        if not entry_id or not who:
            return None
        date_text = self.world.date_of(entry_id, who, now)
        if not date_text:
            return None
        planned = next((x for x in self.world.entries(date_text, who) if str(x.get("id")) == entry_id), None)
        if not self._movable(planned, now):
            return None
        return date_text, planned

    def _rebind_moved_decisions(self, old_date: str, old_entry_id: str, new_date: str, new_entry_id: str) -> None:
        companion = getattr(self, "companionship", None)
        if companion is None or not old_entry_id or not new_entry_id:
            return
        rebound = companion.safe("rebind_decision_entry", old_date, old_entry_id, new_date, new_entry_id, default=0)
        if rebound:
            logger.info(
                "[super_enhance/world] same_as 承接换号（%s/%s -> %s/%s）：已同步改绑 %d 条未发请示",
                old_date, old_entry_id, new_date, new_entry_id, rebound,
            )

    def _drop_skipped_steps(
        self,
        who: str,
        new_start: datetime.datetime | None,
        old_start: datetime.datetime | None,
        flat_now: datetime.datetime,
        carried_partners: set[str] | None = None,
    ) -> list[str]:
        """same_as 精准刹车：只删 who 在 [new_start, old_start) 之间被跳过的中间 step 段，保留 old_start 之后后续排程。"""
        if new_start is None or old_start is None or new_start >= old_start:
            return []
        bots = set(self.raw_log.enabled_bots())
        skip_partners = set(carried_partners or ())
        days = sorted({new_start.date().isoformat(), old_start.date().isoformat()})
        removed: list[str] = []
        for day in days:
            for item in list(self.world.entries(day, who)):
                iid = str(item.get("id") or "")
                if str(item.get("source") or "") != "step" or is_event(item):
                    continue
                if iid in self.batch.fresh or iid in self.batch.mentioned:
                    continue
                s0 = parse_time(item.get("start"))
                if s0 is None or (is_sleep(item) and s0 <= flat_now):
                    continue
                if new_start <= s0 < old_start:
                    e0 = parse_time(item.get("end")) if item.get("end") else None
                    for partner in (item.get("with") or []):
                        if partner in bots and partner != who and partner not in skip_partners:
                            self._drop_partner(partner, who, s0, e0)
                    self.world.delete_entry(day, iid)
                    removed.append("%s %s" % (iid, clean_text(item.get("doing"), 16)))
        return removed

    def _carry_partner_planned(
        self,
        other: str,
        who: str,
        record: dict,
        planned: dict,
        raws: list,
        umo: str,
        now: datetime.datetime,
        flat_now: datetime.datetime,
    ) -> dict | None:
        """same_as 承接共同段时，对称承接同行人 other 在原时段的共同 step 段，防止 _pair_up 误触发整天撤排。"""
        old_start = parse_time(planned.get("start"))
        old_end = parse_time(planned.get("end")) if planned.get("end") else None
        new_start = parse_time(record.get("start"))
        new_end = parse_time(record.get("end")) if record.get("end") else None
        if old_start is None or new_start is None:
            return None
        # 本批 entries 里如果记录员已经显式给 other 写了同时段条目，留给它自己处理
        for r in raws:
            if not isinstance(r, dict) or str(r.get("who") or "").strip() != other:
                continue
            if str(r.get("kind") or "") == "event":
                continue
            r_start = parse_time(norm_time(r.get("start"), now.date().isoformat(), fallback=fmt_time(now)))
            if r_start is not None and abs((r_start - new_start).total_seconds()) <= PAIR_NEAR_MINUTES * 60:
                return None
        partner_planned = self._joint_entry(other, who, old_start, old_end, steps=True, near=True)
        if not self._movable(partner_planned, flat_now):
            return None
        p_id = str(partner_planned.get("id") or "")
        p_old_date = self.world.date_of(p_id, other, flat_now)
        if not p_old_date:
            return None
        p_old_start = parse_time(partner_planned.get("start"))
        p_backup = copy.deepcopy(partner_planned)
        self.world.delete_entry(p_old_date, p_id)
        carried = self._carry(partner_planned, str(record.get("start") or ""), str(record.get("end") or ""))
        carried["moved_from"] = {"date": p_old_date, "entry": p_backup}
        seen = sorted((set(record.get("seen_by") or []) | {who}) - {other})
        partner_with = [who] + [x for x in (record.get("with") or []) if x not in (who, other)]
        p_date = str(record.get("start") or "")[:10]
        twin_entry = {
            "who": other,
            "kind": "activity",
            "start": record.get("start"),
            "end": record.get("end") or carried.get("end") or "",
            "place": partner_planned.get("place") or record.get("place"),
            "at_home": partner_planned.get("at_home") if partner_planned.get("at_home") is not None else record.get("at_home"),
            "doing": partner_planned.get("doing") or record.get("doing"),
            "public": partner_planned.get("public") or record.get("public"),
            "with": partner_with,
            "training": partner_planned.get("training") or "",
            "seen_by": seen,
            "source": "chat",
            "src_umo": umo,
            "src_quote": record.get("src_quote"),
            "created": fmt_time(now),
            "asked": bool(partner_planned.get("asked")),
        }
        twin_entry.update(carried)
        twin = self.world.add_entry(p_date, twin_entry)
        if twin is None:
            self.world.add_entry(p_old_date, copy.deepcopy(p_backup))
            logger.warning(
                "[super_enhance/world] same_as 对称承接写入失败（%s %s），已恢复原补写段 %s/%s",
                other, p_date, p_old_date, p_id,
            )
            return None
        t_id = str(twin.get("id") or "")
        self.batch.written.append(twin)
        self.batch.fresh.add(t_id)
        self.batch.mentioned.add(t_id)
        self.batch.moved_map[(other, p_id)] = (p_date, twin)
        self._fit_moved(twin)
        self._rebind_moved_decisions(p_old_date, p_id, p_date, t_id)
        skipped = self._drop_skipped_steps(other, new_start, p_old_start, flat_now, carried_partners={who})
        logger.info(
            "[super_enhance/world] same_as 对称承接%s的共同段 %s -> %s（%s–%s）%s",
            self.label(other) or other,
            p_id,
            t_id,
            str(twin.get("start") or "")[11:16],
            str(twin.get("end") or "")[11:16],
            "；跳过中间段：" + "；".join(skipped) if skipped else "",
        )
        return twin

    def _finish_same_as_move(
        self,
        record: dict,
        moved: dict,
        raws: list,
        umo: str,
        now: datetime.datetime,
        bots: set,
        flat_now: datetime.datetime,
    ) -> None:
        """same_as 命中后的精准刹车：改绑请示、对称承接同行人、只删被跳过的中间 step 段，不触发整天 _redo_after。"""
        who = str(record.get("who") or "")
        new_id = str(record.get("id") or "")
        new_date = str(record.get("start") or "")[:10]
        new_start = parse_time(record.get("start"))
        mf = moved.get("moved_from") or {}
        old_date = str(mf.get("date") or "")
        planned = mf.get("entry") or {}
        old_id = str(planned.get("id") or "")
        old_start = parse_time(planned.get("start"))
        if new_id:
            self.batch.fresh.add(new_id)
            self.batch.mentioned.add(new_id)
        if old_id:
            self.batch.moved_map[(who, old_id)] = (new_date, record)
        self._rebind_moved_decisions(old_date, old_id, new_date, new_id)
        planned_partners = {x for x in (planned.get("with") or []) if x in bots and x != who}
        kept_partners = {x for x in (record.get("with") or []) if x in bots and x != who}
        dropped_partners = planned_partners - kept_partners
        if dropped_partners:
            # 原计划是两人一起做，但实际甩开了同行人：属于真改道，交回 _redo_after 统一收场
            logger.info(
                "[super_enhance/world] %s 的 same_as=%s 未保留原同行人 %s，按改道交由 _redo_after 收场",
                self.label(who) or who, old_id, sorted(dropped_partners),
            )
            self._touch(who, new_start)
            return
        carried_partners: set[str] = set()
        for other in sorted(planned_partners):
            twin = self._carry_partner_planned(other, who, record, planned, raws, umo, now, flat_now)
            if twin is not None:
                carried_partners.add(other)
        skipped = self._drop_skipped_steps(who, new_start, old_start, flat_now, carried_partners=carried_partners)
        if skipped:
            logger.info(
                "[super_enhance/world] %s 的 same_as 精准承接跳过中间段（后续排程保留）：%s",
                self.label(who) or who,
                "；".join(skipped),
            )

    def _take_planned(self, token, who: str, start: str, end: str, now: datetime.datetime) -> dict | None:
        """记录员说她这会儿做的就是补写排在后面的那段（same_as）：把那段撤下来，返回要接着用的字段。"""
        entry_id = str(token or "").strip()
        if not entry_id:
            return None
        ref = self._movable_step_ref(entry_id, who, now)
        if ref is None:
            logger.info("[super_enhance/world] 记录员说%s这会儿做的是 %s，可那段不是还没做完的补写段，照常另记", who, entry_id)
            return None
        date_text, planned = ref
        backup = copy.deepcopy(planned)
        self.world.delete_entry(date_text, entry_id)
        out = self._carry(planned, start, end)
        out["moved_from"] = {"date": date_text, "entry": backup}
        return out

    def _fit_moved(self, record: dict) -> None:
        """挪过来的段：分步分钟按新的起止对上（多的加在最后一步、少的从后往前截）。"""
        if fit_steps(record):
            self.world._save_day(str(record.get("start"))[:10])

    HOWS = (("没做", "没做"), ("压根", "没做"), ("做完", "做完"), ("收尾", "收尾"), ("收个尾", "收尾"), ("交给", "交给"), ("交接", "交给"),
            ("接手", "交给"), ("接着做", "接着做"), ("一个人", "接着做"), ("继续", "接着做"), ("放下", "放下"), ("撂下", "放下"), ("停", "放下"))

    @staticmethod
    def _minutes(value) -> int:
        """「2」「2分钟」「两三分钟」都认成分钟数；认不出是 0（按 2 分钟算）。"""
        hit = re.search(r"\d+", str(value or ""))
        if hit:
            return int(hit.group())
        for word, number in (("一", 1), ("两", 2), ("二", 2), ("三", 3), ("五", 5), ("十", 10)):
            if word in str(value or ""):
                return number
        return 0

    @classmethod
    def _how(cls, raw: dict) -> str:
        text = str(raw.get("how") or "").strip()
        if raw.get("never"):
            return "没做"
        return next((v for k, v in cls.HOWS if k in text), "放下")

    def _apply_stale(self, raws, now: datetime.datetime) -> dict:
        """记录员说对不上了的段（补写排的、主人让做的、之前从聊天里记的；手动写的不动），照它定的收场办（how）：
        做完（先做完再走，算做完）、收尾（先关火盛出来这种再走，算没做完）、交给（交给另一个人接着做）、放下（到此刻为止）、
        没做（压根没做，整段撤掉；要是从补写排的那段挪过来的，原来那段恢复）、接着做（一起做的另一个人一个人接着做）。
        还没开始的段（接着做以外）撤掉。之后跟着处理（_follow_up）：靠没做成的东西的、接着没做成的事往下做的、还没开始的补写段撤掉。
        一起做的另一个人那段记录员没提的，等这次全记完再看（_redo_after：她真走了才只把她去掉、写一句她中途走了）。
        改到的段记进 _touched（谁、几点起改的），apply 最后把她后面补写排的段全撤掉重排。
        返回 {谁: 什么时候腾出手来}（收尾、做完、交接完的时刻），这次的新条目从那时开始。"""
        flat = now.replace(tzinfo=None)
        bots = set(self.raw_log.enabled_bots())
        mentioned = {str(x.get("entry") or "").strip() for x in raws if isinstance(x, dict)}
        self.batch.mentioned |= mentioned
        free_at, pending, orphans = {}, [], []
        later = [x for x in raws if isinstance(x, dict) and self._how(x) == "接着做"]
        for raw in [x for x in raws if isinstance(x, dict) and self._how(x) != "接着做"] + later:
            who = str(raw.get("who") or "").strip()
            entry_id = str(raw.get("entry") or "").strip()
            if who not in bots or not entry_id:
                continue
            if entry_id in self.batch.same_as_targets:
                logger.info("[super_enhance/world] %s 的 %s 本批将由 same_as 精准承接，跳过 stale 删除", who, entry_id)
                continue
            how = self._how(raw)
            why = clean_text(raw.get("why"), 60)
            date_text = self.world.date_of(entry_id, who, flat)
            entry = next((x for x in self.world.entries(date_text, who) if str(x.get("id")) == entry_id), None) if date_text else None
            start = parse_time(entry.get("start")) if entry else None
            end = parse_time(entry.get("end")) if entry and entry.get("end") else None
            source = str(entry.get("source") or "") if entry else ""
            if entry is None or start is None or is_event(entry) or source not in ("step", "chat"):
                logger.info("[super_enhance/world] 记录员说%s的 %s 对不上了，但它是手动写的或已经不在了，不动", who, entry_id)
                continue
            if end is not None and end <= flat and how != "没做":
                logger.info("[super_enhance/world] 记录员说%s的 %s 对不上了，可那段已经做完了，不动（%s）", who, entry_id, why)
                continue
            if is_sleep(entry) and start <= flat:
                logger.info("[super_enhance/world] 记录员说%s的 %s 对不上了，可那是她已经睡下的觉，睡觉不打断，不动（%s）",
                            who, entry_id, why)
                continue
            name = self.label(who) or who
            if how == "接着做":
                was_with = list(entry.get("with") or [])
                together = [x for x in was_with if x not in bots]  # 一起的另一个人走了：只留主人
                fields = {"with": together}
                alone = clean_text(raw.get("doing"), 120)
                if alone:
                    fields["doing"] = alone
                    fields.update(self._alone_steps(entry, alone, start, end, flat))
                # 结果原来是按两个人写的：换成记录员写的一个人的；没写就清掉，别留着两个人的说法
                fields["result"] = clean_text(raw.get("result"), 80)
                self.world.update(date_text, entry_id, mode="patch", **fields)
                for leaver in was_with:
                    if leaver in bots and leaver != who:
                        self._drop_partner_from_stopped(date_text, leaver, who, start, end or flat)
                # 走了的那个人段里没做到的那几步标的吃的、东西（两人一起的饭常只标在一个人那段里）：跟到这段上，她做完就算
                moved = []
                for leaver, tags in orphans:
                    if leaver in was_with and tags:
                        moved.extend({k: v for k, v in tag.items() if k != "step"} for tag in tags)
                        tags.clear()
                if moved:
                    current = next((x for x in self.world.entries(date_text, who) if str(x.get("id")) == entry_id), entry)
                    self.world.update(date_text, entry_id, mode="patch", things=list(current.get("things") or []) + moved)
                logger.info("[super_enhance/world] %s的 %s 改成一个人接着做：%s%s（%s）", name, entry_id,
                            clean_text(fields.get("doing") or entry.get("doing"), 30),
                            "，接过 %d 样吃的 / 东西" % len(moved) if moved else "", why)
                self._touch(who, flat)
                continue
            if how == "没做" or start > flat:
                self.world.delete_entry(date_text, entry_id)
                logger.info("[super_enhance/world] 记录员撤掉%s的 %s %s–%s %s（%s%s）",
                            name, entry_id, str(entry.get("start"))[11:16], str(entry.get("end") or "")[11:16],
                            clean_text(entry.get("doing"), 30), "压根没做；" if how == "没做" else "", why)
                if self._restore_moved(entry, flat):
                    continue
                if not (who in self.batch.same_as_movers and how != "没做"):
                    self._touch(who, flat)
                orphans.append((who, [dict(x) for x in entry.get("things") or [] if isinstance(x, dict)]))
                pending.append((who, entry, None, why))
                continue
            if how == "收尾":
                wrap = raw.get("wrap")
            elif how == "交给":
                wrap = raw.get("wrap") or "把手上的活交给%s" % (self.label(str(raw.get("to") or "")) or "别人")
            else:
                wrap = ""
            fields = wind_up(entry, flat, how, self._minutes(raw.get("minutes")), wrap)
            rest = fields.pop("rest", [])
            if how != "做完":
                fields["stop_why"] = why
                result = clean_text(raw.get("result"), 80)
                if how == "交给":
                    result = result or "交给%s接着做" % (self.label(str(raw.get("to") or "")) or "别人")
                if result:
                    fields["stop_result"] = result
            before = dict(entry)  # 改之前的样子（原来几点结束、标了什么）：后面跟着处理要用
            after = self.world.update(date_text, entry_id, mode="patch", **fields) or entry
            if how != "做完" and fields.get("stopped"):
                self.world._fire_drop({who: {f"{date_text}/{entry_id}"}})
            if not (who in self.batch.same_as_movers and how in ("做完", "收尾", "放下")):
                self._touch(who, flat)
            ready = parse_time(after.get("end"))
            if ready is not None and ready > flat:
                free_at[who] = max(free_at.get(who, ready), ready)
            logger.info("[super_enhance/world] %s的 %s %s：%s，到 %s（%s）", name, entry_id, how, clean_text(entry.get("doing"), 30),
                        str(after.get("end"))[11:16], why)
            if how == "交给":
                self._hand_over(who, after, rest, raw, flat)
            elif how != "做完":
                tags = [x for x in before.get("things") or [] if isinstance(x, dict)]
                reached = reached_steps(after, tags)
                orphans.append((who, [dict(x) for x in tags if x not in reached]))
                pending.append((who, before, after, why))
        # 没做成的东西：接着做的人接过去的不算没做成；靠剩下那些的后续段跟着撤
        for who, entry, after, why in pending:
            left = set()
            for leaver, tags in orphans:
                if leaver == who:
                    left |= {str(x.get("new")) for x in tags if x.get("new")}
            self._follow_up(who, entry, after, flat, why, only=left)
        return free_at

    @staticmethod
    def _alone_steps(entry: dict, alone: str, start, end, flat: datetime.datetime) -> dict:
        """一起做的另一个人走了、这个人一个人接着做：分步里她走之前做到的那几步留着（那会儿确实两个人一起），
        后面的换成一步「一个人接着做的事」，占剩下的时间；还没开始就整段换成这一步。标的吃的写的第几步跟着落在这一步上。"""
        if end is None or end <= flat:
            return {}
        if start is None or start >= flat:
            total = max(1, int(round((end - (start or flat)).total_seconds() / 60)))
            return {"steps": [alone], "step_minutes": [total], "steps_planned": None}
        cut = wind_up(entry, flat, "放下")
        done, mins = list(cut.get("steps") or []), list(cut.get("step_minutes") or [])
        left = max(1, int(round((end - flat).total_seconds() / 60)))
        if not done or len(mins) != len(done):
            return {"steps": [alone], "step_minutes": [max(1, int(round((end - start).total_seconds() / 60)))],
                    "steps_planned": None}
        return {"steps": done + [alone], "step_minutes": mins + [left], "steps_planned": None}

    def _hand_over(self, who: str, after: dict, rest: list, raw: dict, now: datetime.datetime) -> None:
        """交给另一个人接着做：那个人从交接完的时候起接着做没做完的那截（剩下的分步、要用到 / 做出来的吃的跟过去）。"""
        to = str(raw.get("to") or "").strip()
        if to not in set(self.raw_log.enabled_bots()) or to == who:
            return
        start = parse_time(after.get("end")) or now
        minutes = sum(int(x.get("minutes") or 0) for x in rest) or 15
        done = after.get("stop_at_step") or 0
        steps = []
        for item in after.get("things") or []:
            if not isinstance(item, dict):
                continue
            number = item.get("step") if isinstance(item.get("step"), int) else 0
            if number > done:
                steps.append(dict(item, step=number - done))
        name = self.label(who) or who
        record = self.world.add_entry(str(start.date()), {
            "who": to, "kind": "activity", "start": fmt_time(start), "end": fmt_time(start + datetime.timedelta(minutes=minutes)),
            "place": after.get("place"), "doing": clean_text(raw.get("doing"), 120) or "接着把%s做完（%s交过来的）" % (
                clean_text(after.get("doing"), 40), name),
            "public": after.get("public"), "with": [], "seen_by": [to],
            "steps": [x for x in rest if x.get("minutes")] or [x.get("text") for x in rest],
            "things": steps, "source": "step", "at_home": after.get("at_home"), "importance": after.get("importance"),
            "stove": after.get("stove") or "",
            **({"lamp": True} if after.get("lamp") else {}),
            "send_msg": bool(after.get("send_msg")) and after.get("msg_at") == "end",
            "msg_at": "end" if bool(after.get("send_msg")) and after.get("msg_at") == "end" else "",
            "msg_purpose": (after.get("msg_purpose") or "") if bool(after.get("send_msg")) and after.get("msg_at") == "end" else "",
            "msg_kind": (after.get("msg_kind") or "") if bool(after.get("send_msg")) and after.get("msg_at") == "end" else "",
            "origin": {"kind": "continue", "text": str(after.get("doing") or "")}, "created": fmt_time(now),
        })
        if record is not None:
            # 这次刚交过去的那段：撤后面补写排的段时不算在里面
            self.batch.fresh.add(str(record.get("id")))
            logger.info("[super_enhance/world] %s交给%s接着做：%s %s–%s", name, self.label(to) or to, record.get("id"),
                        str(record.get("start"))[11:16], str(record.get("end"))[11:16])

    def _restore_moved(self, entry: dict, now: datetime.datetime) -> bool:
        """撤掉的这段是从补写排的那段挪过来的（她说马上去做，其实没去）：原来那段还没到时候的，照原样放回去。"""
        snap = entry.get("moved_from") if isinstance(entry.get("moved_from"), dict) else None
        planned = snap.get("entry") if snap else None
        if not isinstance(planned, dict):
            return False
        start = parse_time(planned.get("start"))
        if start is None or start <= now:
            logger.info("[super_enhance/world] %s 原来那段（%s）已经过了时候，不恢复，让补写重排", entry.get("id"), planned.get("id"))
            return False
        record = self.world.add_entry(str(snap.get("date") or str(planned.get("start"))[:10]), dict(planned))
        if record is None:
            return False
        logger.info("[super_enhance/world] 她其实没去做：原来排的 %s %s–%s %s 恢复", record.get("id"),
                    str(record.get("start"))[11:16], str(record.get("end") or "")[11:16], clean_text(record.get("doing"), 30))
        return True

    def _follow_up(self, who: str, before: dict, after, now: datetime.datetime, why: str, only=None) -> None:
        """一段被撤掉（after 是 None）或没做完就放下以后：靠这段做出来的东西的（段里标的吃的、东西用到、吃到它），
        和接着这段往下做的（来源是「接着前面」这段，只在整段撤掉时算），还没开始的补写段撤掉，一层层往下找。
        正在做的不动；一起做的另一个人那段由记录员定（见 _apply_stale）。"""
        bots = list(self.raw_log.enabled_bots())
        steps = [x for x in before.get("things") or [] if isinstance(x, dict)]
        kept = {str(x.get("new")) for x in reached_steps(after, steps) if x.get("new")} if after is not None else set()
        lost = {str(x.get("new")) for x in steps if x.get("new")} - kept
        if only is not None:
            lost &= set(only)  # 一起做的另一个人接着做、接过去的那几样不算没做成
        doings = {str(before.get("doing") or "")} if after is None else set()
        if not lost and not doings:
            return
        days = sorted({now.date().isoformat(), (now.date() + datetime.timedelta(days=1)).isoformat()})
        done, changed, gone, gone_ids = [], True, [], set()

        def _overlap(a: dict, b: dict) -> bool:
            a0, b0 = parse_time(a.get("start")), parse_time(b.get("start"))
            a1 = parse_time(a.get("end")) if a.get("end") else None
            b1 = parse_time(b.get("end")) if b.get("end") else None
            return a0 is not None and b0 is not None and (a1 is None or b0 < a1) and (b1 is None or a0 < b1)

        while changed:
            changed = False
            for bot in bots:
                for day in days:
                    for item in list(self.world.entries(day, bot)):
                        if item.get("source") != "step" or is_event(item):
                            continue
                        s0 = parse_time(item.get("start"))
                        if s0 is None or s0 <= now:
                            continue
                        uses = set()
                        for step in item.get("things") or []:
                            if isinstance(step, dict):
                                uses |= {str(step.get("id") or ""), str(step.get("into") or "")}
                                uses |= {str(x) for x in step.get("from") or []}
                        origin = item.get("origin") if isinstance(item.get("origin"), dict) else {}
                        text = str(origin.get("text") or "")
                        follows = origin.get("kind") == "continue" and bool(text) and any(
                            len(d) >= 4 and (d in text or text in d) for d in doings)
                        partner = any(str(x.get("id")) in gone_ids and x["who"] in (item.get("with") or []) and _overlap(x, item)
                                      for x in gone)
                        if not (uses & lost) and not follows and not partner:
                            continue
                        self.world.delete_entry(day, str(item.get("id")))
                        done.append("%s %s 撤掉" % (item.get("id"), clean_text(item.get("doing"), 20)))
                        lost |= {str(x.get("new")) for x in item.get("things") or [] if isinstance(x, dict) and x.get("new")}
                        doings.add(str(item.get("doing") or ""))
                        gone.append(dict(item))
                        gone_ids.add(str(item.get("id")))
                        changed = True
        if done:
            logger.info("[super_enhance/world] %s的 %s 没做成，靠它的后续段跟着撤：%s（%s）", self.label(who) or who,
                        before.get("id"), "、".join(done), why)

    def _apply_promises(self, items, now: datetime.datetime, umo: str = "") -> None:
        book = getattr(self, "promises", None)
        if book is None:
            return
        for raw in items:
            if not isinstance(raw, dict):
                continue
            action = str(raw.get("action") or "add")
            try:
                if action == "add":
                    book.add(
                        [str(x) for x in (raw.get("bots") or [])],
                        str(raw.get("date") or ""),
                        str(raw.get("time") or ""),
                        raw.get("what"),
                        str(raw.get("with") or ""),
                        # 「谁答应的」是这段对话的机器人，不是写死的丛雨
                        bot_of_umo(umo) or "napcat",
                        "recorder",
                    )
                elif action in ("done", "cancel"):
                    book.set_status(
                        str(raw.get("id") or ""),
                        "done" if action == "done" else "cancelled",
                        note=clean_text(raw.get("note"), 80),
                    )
            except Exception as e:
                logger.warning("[super_enhance/world] 记录员写约定失败：%s", e)

    def _apply_trainings(self, items, now: datetime.datetime) -> None:
        book = getattr(self, "trainings", None)
        if book is None:
            return
        for raw in items:
            if not isinstance(raw, dict):
                continue
            action = str(raw.get("action") or "start")
            try:
                if action == "start":
                    book.add(
                        str(raw.get("bot") or ""),
                        raw.get("what"),
                        raw.get("standard") or "",
                        float(raw.get("need_hours") or 12),
                        arrangement=str(raw.get("arrangement") or ""),
                        ability_after=str(raw.get("ability_after") or ""),
                        replaces=raw.get("replaces"),
                        source="recorder",
                        created=fmt_time(now),
                    )
                elif action == "update":
                    fields = {}
                    for key in ("what", "standard", "arrangement", "ability_after"):
                        if raw.get(key):
                            fields[key] = raw[key]
                    if raw.get("need_hours"):
                        fields["need_hours"] = raw["need_hours"]
                    if fields:
                        book.update(str(raw.get("id") or ""), **fields)
                elif action == "done":
                    item = book.get(str(raw.get("id") or ""))
                    if item is None or str(item.get("status")) != "open":
                        continue
                    finish = getattr(self, "finish_fn", None)
                    if finish is not None:
                        # 和练够时长自动练成走同一条路：标练成、写进能力清单
                        finish(str(item.get("bot")), item, now, "对话里练成")
                    else:
                        book.set_status(item["id"], "done", note=clean_text(raw.get("note"), 80))
                elif action == "drop":
                    book.set_status(
                        str(raw.get("id") or ""), "cancelled", note=clean_text(raw.get("note"), 80)
                    )
            except Exception as e:
                logger.warning("[super_enhance/world] 记录员写在练的事失败：%s", e)

    def _apply_abilities(self, items, now: datetime.datetime) -> None:
        book = getattr(self, "abilities", None)
        if book is None:
            return
        for raw in items:
            if not isinstance(raw, dict):
                continue
            try:
                book.add(
                    str(raw.get("bot") or ""),
                    raw.get("text"),
                    "recorder",
                    since=now.date().isoformat(),
                )
            except Exception as e:
                logger.warning("[super_enhance/world] 记录员写能力清单失败：%s", e)

    # ---------- 家里的吃的（第 18 条） ----------

    def _by(self, value) -> str:
        """谁：napcat / napcat2 / master 原样；写成名字的对回去；别人照写。"""
        text = clean_text(value, 12)
        if text in ("master", "主人"):
            return "master"
        for bot in self.raw_log.enabled_bots():
            if text and text in (bot, self.label(bot)):
                return bot
        return text

    def _said_item(self, items, said):
        """记录员摘的那句原话是哪条消息：那条原始消息；找不到就 None。"""
        probe = "".join(str(said or "").split())
        if len(probe) < 4:
            return None
        for item in items or []:
            text = "".join(str(item.get("text") or "").split())
            if text and (probe in text or text in probe):
                return item
        return None

    def _thing_stage(self, raw: dict, umo: str, items, day: str, now: datetime.datetime, new: bool = False) -> dict:
        """记录员写的一步 → 存货里的一步；src 对回原话是哪条消息、谁说的。"""
        hit = self._said_item(items, raw.get("quote"))
        said = norm_time("%s %s" % (hit.get("date"), hit.get("time")), day) if hit else ""
        src = {"umo": umo, "quote": clean_text(raw.get("quote"), 80)}
        at = norm_time(raw.get("at"), day, fallback=said or fmt_time(now))
        if hit:
            src["at"] = said
            if str(hit.get("role") or "") == "assistant":
                bot = bot_of_umo(umo)
                src["who"] = (self.label(bot) or bot) + ("（主动发的）" if str(hit.get("src") or "") == "proactive" else "")
            else:
                src["who"] = "主人"
        try:
            place = checked_place(raw.get("place"), new, raw.get("name") or raw.get("id"))
        except Exception:
            place = ""
        cook_val = str(raw.get("cook") or "").strip().lower()
        return {
            "at": at,
            "what": raw.get("what"),
            "left": raw.get("left"),
            "place": place,
            "keep": raw.get("keep"),
            "state": raw.get("state"),
            "cook": cook_val if cook_val in ("boil", "fry", "steam", "roast", "cold") else "",
            "amount": raw.get("amount"),
            "used": raw.get("used"),
            "holder": self._by(raw.get("holder")) if raw.get("holder") else "",
            "owner": self._by(raw.get("owner")) if raw.get("owner") else "",
            "by": self._by(raw.get("by")),
            "note": raw.get("note"),
            "src": src,
            "recorded": fmt_time(now),
            "purpose": raw.get("purpose"),
            "weather": raw.get("weather"),
            "spot": raw.get("spot"),
        }

    def _apply_things(self, raws, umo: str, items, now: datetime.datetime, cat: str = CAT_FOOD) -> None:
        """新的一件先建（临时代号换成程序发的编号），再给已有的加步；做菜用掉的料两头都记上（菜的 from、料的 into）。
        单条不合法只跳过该条，绝不阻断整批消息记录与游标推进。"""
        store = getattr(self, "things", None)
        if store is None or not isinstance(raws, list) or not raws:
            return
        flat = now.replace(tzinfo=None)
        day = str((items[0].get("date") if items else "") or flat.date().isoformat())
        view = store.here(store.view(flat), flat)
        keys: dict[str, str] = {}
        dishes = []

        def resolve(token) -> str:
            """临时代号 / 编号 → 存下来的那件的编号（世界线里现算的先存下来）；对不上返回空串。"""
            text = str(token or "").strip()
            if text in keys:
                return keys[text]
            item = view.find(text)
            if item is None:
                return ""
            if item.get("virtual"):
                store.materialize(item, view)
            return str(item.get("id"))

        def bad_time(stage) -> bool:
            moment = parse_time(stage.get("at"))
            return moment is None or moment > flat + datetime.timedelta(minutes=5)

        for raw in raws:
            if not isinstance(raw, dict) or not str(raw.get("new") or "").strip():
                continue
            key = str(raw["new"]).strip()
            stage = self._thing_stage(raw, umo, items, day, now, new=True)
            if bad_time(stage):
                logger.debug("[super_enhance/things] 跳过取得时间不对的新物品：%s (%s)", raw.get("name"), stage.get("at"))
                continue
            moment = parse_time(stage["at"])
            gap = datetime.timedelta(minutes=SAME_NEW_MINUTES)
            same = find_same([x for x in view.values() if cat_of(x) == cat], raw.get("name"),
                             norm_step(stage.get("what"), cat), moment - gap, moment + gap,
                             kind=raw.get("kind") if cat == CAT_ITEM else norm_kind(raw.get("kind")), cat=cat,
                             skip=set(keys.values()))
            if same is not None:
                keys[key] = resolve(same.get("id"))
                logger.info("[super_enhance/things] %s 已经有了（%s），不再新建", raw.get("name"), same.get("id"))
                continue
            sources = []
            for token in raw.get("from") or []:
                text = str(token or "").strip()
                source = store.get(keys[text]) if text in keys else view.find(text)
                if source is None or not usable(source, stage["at"]):
                    logger.debug("[super_enhance/things] %s 的用料 %s 当时不可用，忽略该关联", raw.get("name"), token)
                    continue
                sid = resolve(text)
                if sid and sid not in sources:
                    sources.append(sid)
            item, why = store.create(raw.get("name"), raw.get("kind"), raw.get("origin"), sources, stage, fmt_time(now),
                                     cat=cat, look=raw.get("look") or "", owner=self._by(raw.get("owner")) if raw.get("owner") else "")
            if item is None:
                logger.debug("[super_enhance/things] 新建物品跳过 %s：%s", raw.get("name"), why)
                continue
            keys[key] = str(item["id"])
            view[item["id"]] = copy.deepcopy(item)
            if sources and cat == CAT_FOOD:
                dishes.append(item)
            first = item["stages"][0]
            logger.info("[super_enhance/things] 新记 %s %s（%s，%s %s）", item["id"], item["name"], item["kind"],
                        first["at"][5:], first["what"])
        for raw in raws:
            if not isinstance(raw, dict) or str(raw.get("new") or "").strip():
                continue
            tid = resolve(raw.get("id"))
            if not tid:
                logger.debug("[super_enhance/things] 跳过不存在的物品变化：%s (%s)", raw.get("id"), raw.get("what"))
                continue
            stage = self._thing_stage(raw, umo, items, day, now)
            if bad_time(stage):
                logger.debug("[super_enhance/things] 跳过时间不对的物品变化：%s (%s)", tid, stage.get("at"))
                continue
            into = resolve(raw.get("into")) if raw.get("into") else ""
            if into and into != tid:
                stage["into"] = into
            existing = view.find(tid) or store.get(tid)
            if existing is None:
                continue
            trial = copy.deepcopy(existing)
            why = insert_stage(trial, clean_stage(stage, cat))
            if not why:
                why = store.add_stage(tid, stage)
            if why:
                if not why.startswith("重复"):
                    logger.debug("[super_enhance/things] 跳过物品变化 %s：%s", tid, why)
                continue
            view[tid] = trial
            if stage.get("into"):
                store.link_source(into, tid)
            logger.info("[super_enhance/things] %s 记一步：%s %s%s", tid, stage["at"][5:], norm_step(stage.get("what"), cat),
                        "（做进了 %s）" % into if stage.get("into") else "")
        # 菜的 from 里写了、料那边没记「用掉」的：补一步，剩多少没说
        for dish in dishes:
            first = dish["stages"][0]
            made_at = parse_time(first.get("at"))
            for sid in dish.get("from") or []:
                source = store.get(sid)
                if source is None or any(str(x.get("into") or "") == dish["id"] for x in source.get("stages") or []):
                    continue
                if made_at is not None and any(
                    x.get("what") in USE_STEPS and parse_time(x.get("at")) is not None
                    and not x.get("into")
                    and abs((parse_time(x.get("at")) - made_at).total_seconds()) <= SAME_STEP_MINUTES * 60
                    for x in source.get("stages") or []
                ):
                    continue
                why = store.add_stage(sid, {
                    "at": first["at"], "what": "用掉一部分", "by": first.get("by"), "into": dish["id"],
                    "note": "做这盘菜时用了，聊天里没说剩多少", "src": first.get("src"), "recorded": fmt_time(now),
                })
                if why and not why.startswith("重复"):
                    logger.debug("[super_enhance/things] 补记用料跳过 %s -> %s：%s", sid, dish["id"], why)

    def _apply_money(self, raws, umo: str, items, now: datetime.datetime) -> None:
        """把对话原文中已实际发生的收支、拨款/拿取与欠账写入人民币账本。"""
        book = getattr(self, "money", None)
        if book is None or not isinstance(raws, list) or not raws:
            return
        from .money import normalize_account, normalize_kind
        flat = now.replace(tzinfo=None)
        has_master = any(str(x.get("role") or "") == "user" for x in (items or []))
        fallback_at = fmt_time(flat)
        if items:
            last = items[-1]
            if last.get("date") and last.get("time"):
                fallback_at = "%s %s" % (last["date"], last["time"])
        ops = []
        for raw in raws:
            if not isinstance(raw, dict):
                continue
            kind = normalize_kind(raw.get("kind"))
            if not kind:
                continue
            acc = normalize_account(raw.get("account"))
            fa = normalize_account(raw.get("from_account"))
            ta = normalize_account(raw.get("to_account"))
            # 职责分工：对话记录员记涉及主人钱包(master)的当场动钱，以及主人亲口指挥的账户间挪钱(transfer)；
            # 女孩自己买菜买柴、卖废品、挂账结账等全归世界线，彻底杜绝双扣（且不触发撤排未来计划）
            involves_master = (acc == "master" or fa == "master" or ta == "master")
            allow_master_transfer = (kind == "transfer" and has_master)
            if not involves_master and not allow_master_transfer:
                logger.info("[super_enhance/money] 记录员跳过非主人钱包账目（归世界线）：%s %s", kind, raw.get("what"))
                continue
            # 防止纯机器人消息私自扣动、凭空刷钱或单方面往主人钱包塞钱（须主人当面聊到）
            if not has_master:
                if (
                    (kind in ("spend", "earn", "debt_settle") and acc == "master")
                    or (kind == "transfer" and (fa == "master" or ta == "master"))
                ):
                    logger.info("[super_enhance/money] 本批无主人发言，拦截私自动用或改写主人钱包：%s", raw.get("what"))
                    continue
            op = dict(raw)
            op["who"] = str(raw.get("who") or bot_of_umo(umo) or "").strip()
            ops.append(op)
        if ops:
            book.apply_record_ops(
                ops, now=now, default_at=fallback_at, source="recorder", world=self.world, has_master=has_master
            )
