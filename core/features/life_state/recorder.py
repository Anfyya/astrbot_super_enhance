"""记录员：把对话原文里发生过的事合进世界线。

- 触发：某个会话最后一条消息过了 recorder_quiet_seconds 秒、且有没处理过的消息；
- 输入：新消息原文（带时间、标好说话人）+ 相关人物最新条目 + 生效中的约束/打算/约定/在练的事 + 现在时间；
- 输出：JSON（entries / constraints / intentions / promises / trainings / abilities / things）；
- 失败：游标不前进，写错误记录，下次重试。
"""

import asyncio
import datetime
import hashlib
import json
import re

from astrbot.api import logger

from ..message_text import strip_inline_system
from .models import bot_of_umo, extract_json
from .things import (CAT_FOOD, CAT_ITEM, KEEPS, KINDS, SAME_NEW_MINUTES, SAME_STEP_MINUTES, SPOTS, STEPS, USE_STEPS, cat_of,
                     find_same, norm_step, usable)
from .world import (WorldStore, clean_steps, clean_text, doing_text, fit_steps, fmt_time, is_event, is_sleep, parse_time, progress_of,
                    result_text, step_minutes, steps_text, wind_up)
from .things import reached_steps

# 记录员停过一阵时，往前最多翻几天的原始消息（原始消息副本最多也只留这么久）
RAW_BACK_DAYS = 6

SYSTEM_PROMPT = (
    "你只输出一个可以被 JSON 解析的对象，不要输出解释、Markdown 代码块或任何多余文本。"
)

# 给记录员对照的「后面排好的」：往后看几小时、每人最多几段
# 新打算腾地方：时间段超过这么久就当说得太笼统，不动排好的
MAKE_ROOM_HOURS = 3
PLANNED_HOURS = 6
PLANNED_MAX = 8

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


RECORDER_TEMPLATE = """你在给一段对话做记录：把刚刚发生的事，整理成结构化的条目。

现在：{now}
这段对话是谁和谁：{where}
说话人的名字对照：{labels}

对话原文（**每条前面是完整日期和时间**，例如 2026-10-12 06:55；这两天都有，别把昨天的当成今天的）：
{transcript}

{today_block}

{things_block}

规则（只按这些做，不要推理没说出口的东西）：

1. 只记原文里明确说了的。时间没说的，用那条消息的时间；说不清就先不记。不许记晚于「现在」的事。
2. 条目里的 start / end 一律写成完整的 "YYYY-MM-DD HH:MM"，**日期必须照抄对话原文里的日期**，不许只写时分、也不许自己推成别的日子。
3. 主人说他自己的事（去了哪、在干什么），记成一条 who = "master" 的条目。他说还在外面、还没回来、还得一会才回去，也是他这会儿的情况，照样记一条（人在外面、还没回宿舍），要回来那件事另记打算。他的反问、抱怨、随口一问（例如「你知道我几点回来？」）不是他的安排，不记。主人说他自己**之后**要干什么（明天有课、晚上要去图书馆、晚饭后陪她去夜市），不记成条目，记成 who = "master" 的打算（intentions）：text 写清楚他要去做什么、在哪、跟谁，date 写哪天，window 写钟点（说了才写）；要带上她一起去的，她那边另记一条 with 是 ["master"] 的打算。主人的打算过了那天程序会自己撤；他说不去了、改了主意的，照常按编号撤（第 14 条）。
4. 主人给她定的、往后一直管着她的规矩（不许做什么、往后都要怎么做），记成 constraints（约束）：到期或主人撤了才解除。text 只写主人自己说出口的话；她回话里自己添上的、答应时顺嘴加的，都不是主人的规矩，不写进去。主人叫她去做一件做完就了结的事，不是规矩：叫她过会儿再去做的，按第 6 条记成打算；叫她现在就去做的，按第 5 条办（答应了还没做上的记成 now 的打算）。约束的 since 填主人说这句话的那条消息的时间；主人说了期限的要换算成 until_time（"这几天"按三天算、"今晚"到当天 23:59、"明天之前"到明天 0:00），只说了条件的写进 until。新规矩不要写编号，编号由程序分配。
5. 你只记**现在和过去**：已经发生了的、这会儿正在发生的。还没发生的（将来）交给补写去排，你不排。她的条目分两种，写在 kind 里：
   - "activity"：她说她已经开始做另一件事、已经换了地方、已经把手上的事放下去干别的了。一条只写一件事，她说了接连做了几件的，按先后拆成几条首尾相接。end 写这件事按常理预计做完的时刻（晚于 start）；她说已经做完的，end 写做完的那一刻。主人叫她做的事、她说已经在做了的，再写 asked: true（做完了程序会叫她回来跟主人说一声）。
   - 她答应现在就去做一件事、可还没做上（主人叫她去买、去做饭、去看看，她答应了；或者她自己说「这就回去给主人做饭」）：那件事是将来，不记成她在做那件事的条目，记成一条打算（第 6 条，写 now: true），到了以后怎么做由补写来排。你要记的是现在：她从手上那件怎么自然地接下去。不管她在哪、在做什么，都不可能上一刻还在做这件、下一刻就在做那件（手上本来闲着的除外）：
     · 手上那件写进 stale（第 16 条）定怎么收场，照常理和她自己的本事来：在足汤先擦干脚、穿好鞋，在外面吃东西先赶紧吃完，在做饭先关火，坐着摩天轮得等它转完下来（丛雨能穿墙、能飘，可以直接飘下来；亚托莉不行）；手上闲着、随时能停的写放下。
     · 她人不在做那件事的地方的，再记一段 activity 写她往那边去（例如「从温泉街往宿舍走，回去给主人做饭」）：start 写她答应的那条消息的时间（程序会挪到收完尾以后），end 照路程估，今天清单里走过同一条路的照那个时长，没走过的按常理。人已经在那儿的，不用记这段。
     · 她说已经到了、已经做上了，才照上一条记成她在做那件事。之后她的话要是说明其实没去、改了主意、或者被叫去干别的了，按第 16 条把那段写进 stale，打算按第 14 条撤。
   - "event"：做手上的事当中顺带发生的一件事（递了个东西、被摸了头、答应了什么），只有一个时刻，不写 end，不打断手上那件。
   她只是边做事边跟人说话，或者说的就是「今天的简略世界线」里标着「正在做」的那件，什么都不用记：聊天本身不算一件事。她说自己做了什么、在做什么，就算做了（哪怕听着像编的）。
   只有和她手上那件真冲突，才记一段新的 activity 把它换掉。真冲突是指：她说人在别的地方；她说那件事停下了、做完了；她说正在做的事和手上那件不可能同时做。只是没提手上那件、只提了其中一部分、或者说了件能同时做的小事，都不算冲突：能记成 event 就记 event，不然什么都不记。没说不等于没做。
   手上那段标着「闲着」的：她说开始做了一件具体的事（看书、出门、做饭……），就记一段新的 activity。
6. 之后要去做的一件具体的事（待会儿、下午、晚饭后、明天去做什么），记成 intentions（打算），不是条目：她自己说要去做的，和主人叫她过会儿去做的，都算。她答应现在就去做的（第 5 条）也记成打算：写 now: true，since 写她答应的那条消息的时间，window、date 留空；她向主人提议、主人还没答应的（「中午咱们去买豆腐呗」「要不要去后山」），不算，不记。text 要写清楚，让没看过这段对话的人也能照着去做：做什么、在哪、跟谁、要带上或用到什么、做到什么样算完，最后用一句话交代是怎么说起的（几点谁说的、因为什么）；只用对话里有的信息补全，不编没说的。比如不要写「回头把体己数给主人看」，要写「把柜角小布袋里攒的铜板拿出来，当着主人的面一枚枚数给他看，说清是哪来的（10-17 晚上逛夜市时主人问她钱是哪来的，她说是香火铜板攒的，答应回头数给他）」。这件事要和主人一起做、而且是主人提的或主人答应了的，with 写 ["master"]；她一个人做的留空。date 写哪天做（YYYY-MM-DD，「明天」按对话日期换算；半夜 0 点到天亮前说的「明天」「明早」「天亮」指天亮以后的这一天，日期不加一），没说哪天留空；window 写钟点范围，没说就留空。打算可以同时有好几条，做完一条或哪条对不上了，就按编号撤那一条（见第 14 条）。往后都怎么做、往后注意什么（做法、原则），不是一件要去做的事，不记成打算：这话是主人说的，按第 4 条记成规矩；只是她自己说的，什么都不记。
7. 两个角色之间的对话同样处理；另一个角色说的话，不能当成主人说的。认清每句话里的「你」「我」：主人话里的「我」是主人自己，「你」是这段私聊里的她（看上面「这段对话是谁和谁」）；她话里的「我」和她的自称是她自己。记谁做了什么、谁说了什么，照这个认主语，别把她做的事记成主人做的，也别反过来（例如主人说「饭你刚才也做好了」，是她做好的饭，不是主人做的）。拿不准是谁做的，就不写是谁做的。
   条目的 with 里写另一个角色（她和对方一起）之前，先看清单里对方那会儿在干什么：两人本来就在一起、这会儿一起走的，两个人的条目都记上（对方那条 with 写她），对方手上那段也按第 16 条写进 stale；对方那边已经记着别的事（尤其是对方自己说的），就照对方的来，只记她自己：with 不写对方，doing 里也别写成和对方一起。
8. 每条条目写两层：public 是住在一起的人自然会知道的粗粒度信息（人在哪、大概在干哪类事，例如"在藏经阁闭关练功"、"出门吃饭了"），可以为空表示别人完全不知道；doing 是细节，可以写具体在干什么、聊了什么。
9. seen_by 写"谁能看到细节"：当事人自己不用写；当时在场的人、以及这段话本来是在哪段私聊里说的，写进去。
10. 这段时间里发生的事都要记下来（她开始做什么、去了哪、什么时候、主人交代了什么要紧的话），不要只挑一两件；纯寒暄、一句话的应答、边干活边聊天都不用记，同一件事连着说的几句合成一条。
11. 上面「生效中的约束」里，已经不该再管着她的，把编号填进 lift_constraints：到期条件已经达成的；主人撤销或放宽了的；后来发生的事已经和它对不上的（例如被赶出去、禁足之后主人又叫她回来、原谅了她；说好"这几天"的那几天已经过了）；因为某件事才定的规矩，那件事结束了也跟着撤（两个人的都算）。拿不准就不动。
12. 条目里如果是在练「在练的事」里的某件，training 填它的编号，例如 "#2"；不是在练就留空。trainings 里 update / done / drop 的 id 也填这个编号。
13. 她在回答、解释、回忆以前的事（昨天、早上、刚才已经说过的）时，那件事不是这会儿发生的：不要按这条消息的时间再记一条；下面「今天已经记了的」里有的别重复记。确实没记过、要补的，时间写成那件事实际发生的时候。
14. 上面列的「打算」里（每条前面有编号，比如 i3），已经做完的、或者后来的事已经和它对不上的（例如说好在社务所等，后来人已经回了宿舍），把那几条的编号填进 drop_intentions（只撤这几条，别的留着）；这次又说了新打算的，照常写进 intentions；她之前说过、还没做完也没对不上的，不用重写。
15. 她把「今天的简略世界线」里标着「还没到」的某件事，说成已经做了、或者正在做：这件事不记成条目，也不要为它改别的条目，写进 early：who、entry（那段的编号）、said（她原话里说的那句）。只算那份清单里标了「还没到」的事；清单里没有的、已经做过或正在做的事，照上面的规则记（那件事前面已经做过一截、现在只是「接着」往下做的，不算说早了）。她说的是「待会儿去」「打算做」，不算，不写 early。
16. 「今天的简略世界线」里补写排的、主人让做的、你之前从聊天里记的（标着「正在做」「还没到」的都算），都是按当时的情况写的。这段对话里说的事让其中某几段对不上了的（例如主人说他回来了、出门了，主人说那件事先不做了，她答应了这会儿去做别的，人换了地方，那段写的事已经做过了，另一个人那段跟她有关的事跟着变了），把对不上的那几段写进 stale：who、entry（编号）、why（一句话写为什么不照原样做了，用她那边发生的事来说，例如「主人叫她去街上看看有什么硬菜」），再写 how：这件事怎么收场。照常人的做法想，手上正做着扔不下的事，不会撂下就走：
    - 做完：快做完了，先把它做完再去；minutes 写还要几分钟。
    - 收尾：手上的事扔不下（灶上炒着菜、锅里煮着东西、手里端着东西），先收个尾再走；wrap 写收尾做了什么（例如「关火，把炒了一半的饭盛出来扣好」），minutes 写几分钟；result 写放下时那件事是什么样（例如「饭炒到一半，盛在盘里扣着」）。这件事算没做完。
    - 交给：交给另一个人接着做；to 写交给谁（napcat / napcat2），doing 写那个人接着做什么，minutes 写交接用几分钟。
    - 放下：随时能停的事（看书、坐着、闲聊），或者还没开始的，直接放下。
    - 没做：其实压根没做（她只是嘴上答应了，后面一直在聊别的、改了主意），整段撤掉；她那段时间实际在干什么，照第 5 条另记一条（时间写实际的）。
    - 接着做：清单里标着「和她一起」的另一个人那段，她走了以后对方一个人接着做；doing 写对方一个人接着做什么。
    她接下去的那段（往那边去的路上，或者她说已经做上了的新的那件），从收尾、做完、交接完的时候才开始（start 照常写那条消息的时间，程序会往后挪）。和她一起做这件事的另一个人那段（清单里标着「和她一起」），也写进 stale，按上面挑一样（多半是接着做、放下，或者被交给的那个人接手）。只写对不上的；照样做得了、不受影响的不写。还没开始的段不用写进 stale：谁的现在被你改了，程序会把她后面补写排的段全撤掉，由补写按新的情况从头排。
17. 对话原文里标着「（主动发的）」的，是她自己主动发给主人的消息，不是在回主人的话：里面说的打算、提议、规矩一概不记（不写 intentions，也不写 constraints）；里面说她做了什么、在做什么，照常按第 5 条记。
18. 吃的（家里的存货）：买回来的、收到的、做好的吃的，每件一个编号，从有了那一刻起每一步都记进 things。只记聊天里说已经发生了的：「待会去买」「明天做」不记；世界线里写着、聊天里没说的不记（那些程序会按世界线自己算进清单）。标「（主动发的）」的消息里说已经买了、做了、吃了的，也照记（这一条和第 17 条不一样）。
   - 新的一件（上面「家里的吃的」里没有的）：写 new（这一批里的临时代号 n1、n2……；不要写编号，编号由程序发）、name、kind、origin、from，再加上它的第一步。第一步只能是「买回来」「收到」「做好了」「原来就有」：买回来、收到的，必须是聊天里说了真的买了、收到了；家里一直备着、清单里还没记的（盐、糖、醋、米面油这类），头一回说到用了它，先记一件「原来就有」（amount 写估计还有多少），再记用掉；别的清单里没有的东西被吃了、用了，不记（不知道它是哪来的）。清单里已经有的同一件（同一次买的、同一锅做的）不要再建。旧的还没吃完又买了一份，是新的一件。买了当场就吃完的零嘴不用记。
   - 米面油盐、调料干货、腌菜这些按量用的（kind 是「干货调料粮食」「腌菜酱菜泡菜」，清单里写着「还剩多少克 / 毫升」）：量写准确的数，用克、毫升、斤写，不要写「大半袋」「用掉一点」：买回来、收到、原来就有写 amount（一共多少，例如「5 斤」「500 毫升」）；用掉写 used（这次用了多少，例如「3 克」「15 毫升」；聊天里没说就按这道菜平常的用量估）；聊天里说了还剩多少的（「盐只剩个罐底了」「米还有半袋」），记一步「清点」，amount 写折成克 / 毫升的剩多少。一盘菜、一锅汤照旧写大概（left 写「小半锅」「吃了一半」）；按个数的照旧写个数（「两块」）。
   - kind 从这几类里挑：{kinds}。origin 写哪来的（谁在哪买的、谁送的）；自己做的留空。
   - 做菜：做好的那盘菜是新的一件，第一步「做好了」，from 写用了清单里哪几件（写编号；这一批里新记的写临时代号；用到的米面油盐、调料也写上）；用掉的每一件，各记一步「用掉一部分」或「用完了」，into 写那盘菜，按量记的写上 used。用的料清单里没有的，from 里就不写它，一件都对不上就留空。买来的、别人送的熟菜，from 留空。剩下的菜还是那一件，又吃了就记「吃了一部分」「吃完了」。
   - 已有的那件又发生了一步：写 id（清单里的编号；这一批里新记的写临时代号）和这一步。同样的东西有好几件、说不清是哪件的，记在最早有的那件上。
   - 每一步写：at（发生的时刻 YYYY-MM-DD HH:MM）、what（{steps}）、left（这一步之后还剩多少，比如「两块」「小半锅」；用完、吃完写空——用完、吃完必须单独写一步，what 填「用完了」或「吃完了」，别把「吃完了」塞进 left 里）、amount / used（按量记的才写，见上）、place（放在哪个地方，例如「灶房·柜格」）、keep（怎么放：{keeps}）、spot（放在哪，管冷热：{spots}；屋里常温的不用写）、state（整的 / 切开了 / 碰伤了 / 没熟透）、by（谁：napcat / napcat2 / master，别人写名字）、into（用掉是做进了哪盘菜）、quote（摘原文里说这件事的那句话）。place、keep、spot、state 跟上一步一样的不用写。剩菜回锅、凉了热一热记一步「热了一下」；端回锅里重新煎透、煮透、再蒸一下、回炉也是「热了一下」（她说已经下锅了就记，不用等做完）；聊天里说出来中间没熟、夹生、没煎透的，在说出来的那一刻给那件记一步「清点」，state 写「没熟透」，left 照原样；开始腌、晒、熏记「腌上了」「晒上了」「熏上了」（晒、熏写上 spot，比如太阳底下、屋外）。
   - 「记错了」只用在清单里确实记错了的那件（根本没这回事）。
19. 家里的东西（吃的以外的：衣裳围巾、书册本子、纸条信、画和窗花、布偶摆件、工具器物、首饰小物……）：每件一个编号（p 开头），写进 items，记它从哪来、经过谁手、搁在哪、变成什么样。只记聊天里说已经发生了的，规矩和第 18 条一样。
   - 新的一件（上面「家里的东西」里没有的）：写 new（这一批里的临时代号 x1、x2……；编号由程序发）、name（叫得出区别的名字，例如「深青围巾」「兔子窗花」「压在茶壶底下的纸条」，别只写「围巾」「纸条」）、kind（它是什么，一个词：围巾、窗花、纸条、布偶、书……）、look（样子：颜色、材料、大小、上面有什么，能和同一种的别的那件分清）、owner（归谁：napcat / napcat2 / master，大家一起用的留空）、origin（哪来的）、from（用清单里哪几件做的），再加上它的第一步。第一步只能是「买回来」「收到」「捡到」「开始做」（起了个头、还没做完）「做好了」「原来就有」（家里本来就有、清单里还没记的）。
   - 已有的那件又发生了一步：写 id（清单里的编号）和这一步，what 从这几样挑：挪地方 / 交给（交到谁手上）/ 用了 / 做了一截（没做完的接着做）/ 做好了（没做完的那件做完了）/ 变了样（修好了、弄脏了、洗干净了）/ 送人了 / 丢了 / 坏了扔了 / 用完了 / 记错了。
   - 每一步写：at、what、place（搁在哪）、holder（在谁手上、身上：napcat / napcat2 / master，别人写名字；放下搁着就留空）、state（这一步之后它什么样，例如「织到一半」「织好了」「补上了耳朵」）、by（谁弄的）、owner（归属换了才写，例如送给主人了写 master）、quote。跟上一步一样的不用写。
   - 同一种东西有好几件的（清单里标着「长得像，认准编号」），看清是谁做的、什么样、在哪再记；对不上是哪一件就不记，别记到别的那件上，也别为它另建一件。
{world_notes}
输出这个 JSON（没有的字段留空数组）：

{{
  "entries": [
    {{"who": "napcat", "kind": "activity", "start": "2026-10-11 09:00", "end": "2026-10-11 09:40", "place": "", "doing": "",
      "public": "", "with": [], "training": "", "seen_by": [], "asked": false, "quote": "摘一句原文"}}
  ],
  "constraints": [
    {{"who": "napcat", "text": "练成收放之前不许出藏经阁", "public": "在藏经阁闭关练功",
      "since": "2026-10-12 00:11", "until": "在练的事 #3 练成", "until_time": ""}}
  ],
  "lift_constraints": ["c3"],
  "intentions": [{{"who": "napcat", "text": "晚饭后跟主人去温泉街夜市，挨个摊子逛一圈、跟着他挤一回人堆（15:11 她问今晚能不能带她去，主人答应了，说周六人多）", "window": "18:40~21:00", "date": "2026-10-17", "with": ["master"]}},
                 {{"who": "napcat2", "text": "去镇上鱼摊买两条黑鱼回来，晚上做水煮鱼（16:40 主人说想吃水煮鱼，她答应这就去买）", "now": true, "since": "2026-10-17 16:40", "window": "", "date": "", "with": []}},
                 {{"who": "master", "text": "去鹈茅学院上课（23:03 睡前他说明天还有课）", "window": "", "date": "2026-10-18", "with": []}}],
  "drop_intentions": ["i3"],
  "early": [{{"who": "napcat2", "entry": "e88", "said": "衣裳收进屋叠好了"}}],
  "stale": [{{"who": "napcat2", "entry": "e100", "why": "主人叫她去街上看看有什么硬菜", "how": "收尾", "wrap": "关火，把炒了一半的饭盛出来扣好", "minutes": 2, "result": "饭炒到一半，盛在盘里扣着", "to": "", "doing": ""}}],
  "things": [
    {{"new": "n1", "name": "嫩豆腐", "kind": "鲜水豆腐", "origin": "主人在商店街豆腐店买的", "from": [],
      "at": "2026-10-18 08:30", "what": "买回来", "left": "两块", "place": "灶房·水盆里", "keep": "泡在水里", "state": "整的", "by": "master", "quote": "摘一句原文"}},
    {{"new": "n2", "name": "豆腐味噌汤", "kind": "熟汤羹粥", "origin": "", "from": ["n1", "w7"],
      "at": "2026-10-18 11:40", "what": "做好了", "left": "一锅", "place": "灶房·灶台", "keep": "收着放", "by": "napcat2", "quote": "摘一句原文"}},
    {{"id": "n1", "at": "2026-10-18 11:40", "what": "用掉一部分", "left": "一块", "into": "n2", "by": "napcat2", "quote": "摘一句原文"}},
    {{"id": "w7", "at": "2026-10-18 11:40", "what": "用完了", "left": "", "into": "n2", "by": "napcat2", "quote": "摘一句原文"}},
    {{"id": "w4", "at": "2026-10-18 11:40", "what": "用掉一部分", "used": "3 克", "into": "n2", "by": "napcat2", "quote": "摘一句原文"}}
  ],
  "items": [
    {{"new": "x1", "name": "深青围巾", "kind": "围巾", "look": "深青色粗毛线，平针", "owner": "master", "origin": "丛雨给主人织的", "from": [],
      "at": "2026-10-16 13:05", "what": "开始做", "place": "卧房·矮桌", "state": "起了个头", "by": "napcat", "quote": "摘一句原文"}},
    {{"id": "p3", "at": "2026-10-17 20:00", "what": "交给", "holder": "master", "owner": "master", "state": "织好了", "by": "napcat", "quote": "摘一句原文"}}
  ],
  "promises": [{{"action": "add", "bots": ["napcat"], "date": "2026-10-12", "time": "14:00",
                "what": "陪主人进山看红叶", "with": "主人"}}],
  "trainings": [{{"action": "start", "bot": "napcat", "what": "收放自如", "standard": "十回连着一收一放不漏不散",
                 "need_hours": 12}}],
  "abilities": [{{"bot": "napcat", "text": "已能真正进食"}}]
}}

关于 promises / trainings 的 action：
- promises：add（新约定）/ done（完成了，填 id）/ cancel（取消了，填 id）；
- trainings：start（开始练）/ update（改时长或标准）/ done（练成了）/ drop（不练了），update/done/drop 要填 id。
- abilities 只在主人明确确认她学会了一件新本事、而这件事不是在练的事时才写，很少用。
"""


REVIEW_TEMPLATE = """现在是 {now}。下面是还在生效的规矩（约束），以及最近两天世界线里记下的事。
请判断哪些规矩已经不该再管着她们了。

还在生效的规矩：
{constraints}

最近两天发生的事（按时间先后）：
{events}

算「不该再管着」的情况：到期条件已经达成；主人撤销或放宽了；后来发生的事已经和它对不上了
（例如被赶出去、禁足之后主人又叫她回来、原谅了她；说好"这几天"的那几天已经过了）；
规矩是因为某件事才定的（例如因为丛雨被赶走，才让亚托莉别去找她、这几天只陪主人），那件事结束了，规矩也跟着撤。
说话方式这类一直有效的规矩（例如回话不许用某个字）不要动。拿不准就不动。

只输出 JSON：{{"lift": [{{"id": "c10", "reason": "一句话说明"}}]}}
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


def _digest(text: str) -> str:
    return hashlib.md5(str(text or "").encode("utf-8")).hexdigest()[:12]


# 她得先收尾 / 做完 / 交接完才去做新的那件：只挪这么多分钟以内开始的（再早的是在补记以前的事）
FREE_SHIFT_MINUTES = 10
# 两人「一起」做的同一件新事：两边开始的时刻差这么多分钟以内才算是同一件（两边各自收尾的时间可能不一样）
PAIR_NEAR_MINUTES = 15


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
        # 她把日程里后面才做的事说早了：async (bot, umo, entry, said, now)，由 service 接上去叫她改口
        self.on_early = None
        self._early: list = []
        self._early_seen: set[str] = set()

    # ---------- 配置 ----------

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
        if self.provider_id:
            try:
                provider = self.context.get_provider_by_id(self.provider_id)
            except Exception:
                provider = None
            if provider is not None:
                return provider
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

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
        """最后一条消息已经安静够久了，且有没处理的。"""
        if umo in self._running:
            return False
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
            if role == "assistant":
                # 她主动发给主人的（不是回话）：里面说的打算、提议不记（第 17 条）
                who = label + ("（主动发的）" if str(item.get("src") or "") == "proactive" else "")
            else:
                who = "主人"
                # 主人塞给她的一次性提示（<system>……</system>）不是他说的话，不记成规矩
                text = strip_inline_system(text)
                if not text.strip():
                    continue
            lines.append("%s %s %s：%s" % (item.get("date"), item.get("time"), who, text))
        return "\n".join(lines)

    def _today_block(self, now: datetime.datetime) -> str:
        parts = []
        for item in self.world.constraints(active_only=True):
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
                tags.append("%s 答应现在就去" % str(item.get("now_at"))[11:16])
            parts.append("- 打算 %s（%s）：%s" % (item.get("id"), "，".join(tags), item.get("text")))
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
                    hm = str(item.get("start"))[11:16]
                    if item.get("end"):
                        hm += "–%s" % str(item.get("end"))[11:16]
                    flags = []
                    if who != "master" and str(item.get("id")) == current_ids.get(who):
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
                    place = clean_text(item.get("place") or "", 40)
                    head = "- %s %s %s%s" % (item.get("id"), hm, name,
                                              "（%s）" % "、".join(flags) if flags else "")
                    if place:
                        head += " " + place
                    lines.append((start, head + "：" + clean_text(doing_text(item) or item.get("public"), 120)
                                  + result_text(item)))
        lines.sort(key=lambda x: x[0])
        if lines:
            parts.append("- 今天的简略世界线（一行一条，从早到晚；标「还没到」的是还没发生的）：")
            parts.extend(text for _, text in lines)
        return "今天到目前为止（供你对照，别重复记）：\n" + ("\n".join(parts) if parts else "（空）")

    def _things_block(self, now: datetime.datetime) -> str:
        """家里的吃的：现有的一件一行（编号、剩多少、在哪、哪来的、新不新鲜），附最近一天没了的。"""
        store = getattr(self, "things", None)
        if store is None:
            return ""
        flat = now.replace(tzinfo=None)
        shelf = store.view(flat)
        return ("家里的吃的（存货，按第 18 条记；每件前面是程序发的编号）：\n" + store.food_list(flat, shelf, self.label)
                + "\n\n家里的东西（吃的以外，按第 19 条记；每件前面是程序发的编号）：\n" + store.item_list(flat, shelf, self.label))

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
        """配置里写的故事固定事实（藏经阁在哪、门关不关这类），一行一条。"""
        text = str(self.cfg.get("world_notes") or "").strip()
        lines = [x.strip().lstrip("-• ").strip() for x in text.splitlines() if x.strip()]
        if not lines:
            return ""
        return "\n故事里的固定事实（照着记，不要写出和它冲突的事）：\n" + "\n".join("- " + x for x in lines)

    def build_prompt(self, umo: str, items: list[dict], now: datetime.datetime) -> str:
        labels = "、".join(
            "%s=%s" % (bot, self.label(bot) or bot) for bot in self.raw_log.enabled_bots()
        )
        return RECORDER_TEMPLATE.format(
            now=fmt_time(now),
            where=self._where_text(umo),
            labels=labels or "（未配置）",
            transcript=self._transcript(items, umo),
            today_block=self._today_block(now),
            things_block=self._things_block(now),
            kinds=" / ".join(KINDS),
            steps=" / ".join(STEPS),
            keeps=" / ".join(KEEPS),
            spots=" / ".join(SPOTS),
            world_notes=self.world_notes,
        )

    # ---------- 执行 ----------

    async def run(self, umo: str, now: datetime.datetime | None = None) -> bool:
        moment = now or self._now()
        items = self.pending(umo, moment)
        if not items:
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
        try:
            prompt = self.build_prompt(umo, items, moment)
            text = ""
            for attempt in (1, 2):
                try:
                    response = await asyncio.wait_for(
                        provider.text_chat(
                            prompt=prompt,
                            system_prompt=SYSTEM_PROMPT,
                            session_id="super_enhance_recorder",
                        ),
                        timeout=self.timeout,
                    )
                    text = getattr(response, "completion_text", "") or ""
                    break
                except Exception as e:
                    if attempt == 2:
                        raise
                    logger.warning("[super_enhance/world] 记录员第 %d 次失败：%s", attempt, e)
            data = extract_json(text)
            if data is None:
                self.world.note_error("recorder", "返回内容不是 JSON")
                return False
            self.apply(umo, data, moment, items)
            last = items[-1]
            last_ts = float(last.get("ts") or moment.timestamp())
            self.world.set_cursor(umo, last_ts, _digest(last.get("text")))
            self.world.clear_error("recorder")
            logger.info("[super_enhance/world] 记录员处理了 %d 条（%s）", len(items), umo)
            await self._flush_early(moment)
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
                    if await self.run(umo, moment):
                        count += 1
                        did += 1
            if not did:
                break
        return count

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
        active = self.world.constraints(active_only=True)
        if not active:
            return []
        provider = self._resolve_provider()
        if provider is None:
            raise RuntimeError("没有可用的模型")
        response = await asyncio.wait_for(
            provider.text_chat(
                prompt=self._review_prompt(active, moment),
                system_prompt=SYSTEM_PROMPT,
                session_id="super_enhance_constraint_review",
            ),
            timeout=self.timeout,
        )
        data = extract_json(getattr(response, "completion_text", "") or "")
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
        batch = list(items or [])
        flat_now = now.replace(tzinfo=None)
        bots = set(self.raw_log.enabled_bots())
        # 这次改到了谁的现在（谁 → 从几点起）：最后把她后面补写排的段全撤掉，由补写按新的情况从头排
        self._touched, self._mentioned, self._fresh = {}, set(), set()
        # 说早了的那段要留着叫她改口，不跟着撤
        self._keep = {str(x.get("entry") or "").strip() for x in data.get("early") or [] if isinstance(x, dict)}
        records = []
        # 先定对不上的那几段怎么收场（收尾、做完、交接要几分钟），再记这次的新条目：免得新条目先把手上的事一刀截断
        self._collect_early(data.get("early") or [], umo, items, now)
        free_at = self._apply_stale(data.get("stale") or [], data.get("early") or [], now)
        for raw in data.get("entries") or []:
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
            kind = "event" if str(raw.get("kind") or "") == "event" and who != "master" else "activity"
            end = "" if kind == "event" else norm_time(raw.get("end"), date_text)
            if end and parse_time(end) is not None and parse_time(end) <= parse_time(start):
                end = ""
            ready = free_at.get(who)
            if kind == "activity" and ready is not None and parse_time(start) < ready \
                    and parse_time(start) >= flat_now - datetime.timedelta(minutes=FREE_SHIFT_MINUTES):
                # 她得先收完尾、做完、交接完手上的事才去做这件：开始往后挪到那时（结束跟着挪）
                shift = ready - parse_time(start)
                start = fmt_time(ready)
                if end:
                    end = fmt_time(parse_time(end) + shift)
            mine = self._by_start_doing(who, start, end) if kind == "activity" and who != "master" else None
            if mine is not None:
                # 她用「现在去做」自己写过了：记录员不另记，也不盖掉它（几点做完、做完叫她都照那段）
                logger.info("[super_enhance/world] 记录员记的「%s」撞上%s用现在去做写的 %s，不另记",
                            clean_text(raw.get("doing"), 30), self.label(who) or who, mine.get("id"))
                continue
            entry = {
                "who": who,
                "kind": kind,
                "start": start,
                "end": end,
                "place": raw.get("place"),
                "doing": raw.get("doing"),
                "public": raw.get("public"),
                "with": raw.get("with") or [],
                "training": self.training_id(raw.get("training"), who),
                "seen_by": self._seen_by(raw, umo),
                "source": "chat",
                "src_umo": umo,
                "src_quote": raw.get("quote"),
                "created": fmt_time(now),
                # 主人叫她现在去做的：做完叫她回来说一声；被打断照实写；后来发现其实没去做，记录员能撤
                "asked": bool(raw.get("asked")) and kind == "activity" and who != "master",
            }
            moved = self._take_planned(raw.get("same_as"), who, start, end, flat_now) if kind == "activity" else None
            if moved:
                entry.update(moved)
            record = self.world.add_entry(date_text, entry)
            if record is not None and moved:
                self._fit_moved(record)
                logger.info("[super_enhance/world] 记录员把%s排好的 %s 挪到 %s（%s），分步和标的吃的照留",
                            self.label(who) or who, raw.get("same_as"), start[11:16], clean_text(record.get("doing"), 30))
            if record is not None and kind == "activity" and who in bots:
                records.append(record)
                stop = parse_time(record.get("end")) if record.get("end") else None
                # 补记很早以前的事不算；记的是现在、刚才的事才算改了她的现在
                if stop is None or stop > flat_now - datetime.timedelta(minutes=FREE_SHIFT_MINUTES):
                    self._touch(who, parse_time(record.get("start")))
        # 记录员写了她和对方一起、却只给她记了：两人本来就在一起的，给对方也记上
        self._pair_up(records, umo, now)
        if batch and not any(str(x.get("role") or "") == "user" for x in batch):
            # 这批全是她主动发的、主人一句没说：规矩和打算都不可能从这里来（主动消息里的不记）
            if data.get("constraints") or data.get("intentions"):
                logger.info("[super_enhance/world] 这批只有她主动发的消息，丢掉记录员给的 %d 条规矩、%d 条打算",
                            len(data.get("constraints") or []), len(data.get("intentions") or []))
            data = dict(data, constraints=[], intentions=[])
        for raw in data.get("constraints") or []:
            if not isinstance(raw, dict):
                continue
            self.world.add_constraint({
                "who": raw.get("who"),
                "text": raw.get("text"),
                "public": raw.get("public"),
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
        for cid in data.get("lift_constraints") or []:
            self.world.lift_constraint(str(cid), note="对话里解除")
        # 先撤掉过时的打算（按编号），再记这次新说的
        for token in data.get("drop_intentions") or []:
            token = str(token or "").strip()
            if not token:
                continue
            if re.match(r"^i\d+$", token):
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
            now_at = ""
            if raw.get("now") and who in bots:
                # 答应现在就去做的：从她答应的那一刻起排（说不清就按现在；不会晚于现在）
                moment = parse_time(norm_time(raw.get("since"), flat_now.date().isoformat(), fallback=fmt_time(now)))
                now_at = fmt_time(min(moment, flat_now) if moment is not None else flat_now)
            record = self.world.set_intention(
                who, raw.get("text"), raw.get("window") or "", "chat",
                with_list=together, date=raw.get("date") or "",
                heard_by=[heard] if who == "master" and heard else None, now_at=now_at,
            )
            # 只有新记的才腾地方；同一条再说一遍不动（补写可能已经把它排进去了）；主人自己的安排不腾她们的地方
            if record and str(record.get("id")) not in known and who != "master":
                if record.get("now_at"):
                    self._go_now(record, now)
                else:
                    self._make_room(record, now)
        self._apply_promises(data.get("promises") or [], now, umo)
        self._apply_trainings(data.get("trainings") or [], now)
        self._apply_abilities(data.get("abilities") or [], now)
        self._apply_things(data.get("things") or [], umo, batch, now)
        self._apply_things(data.get("items") or [], umo, batch, now, cat=CAT_ITEM)
        # 改到了谁的现在：她后面补写排的全撤掉，补写从头排；最后核对两人「一起」对得上
        for who, since in sorted(self._touched.items()):
            self._redo_after(who, since, now)
        self._unpair(now)

    def _collect_early(self, raws, umo: str, items, now: datetime.datetime) -> None:
        """记录员标出的「说早了」：核对那段确实在这批消息之后才排着，攒起来等 run 里叫她改口。"""
        since = self._since(items, now)
        bots = set(self.raw_log.enabled_bots())
        for raw in raws:
            if not isinstance(raw, dict):
                continue
            who = str(raw.get("who") or "").strip()
            entry_id = str(raw.get("entry") or "").strip()
            if who not in bots or not entry_id or entry_id in self._early_seen:
                continue
            date_text = self.world.date_of(entry_id, who, since)
            entry = next((x for x in self.world.entries(date_text, who) if str(x.get("id")) == entry_id), None) if date_text else None
            start = parse_time(entry.get("start")) if entry else None
            if entry is None or start is None or start <= since:
                logger.info("[super_enhance/world] 记录员说%s说早了 %s，对不上日程，不管", who, entry_id)
                continue
            if str(entry.get("source") or "") != "step":
                # 对话里记的、她自己用 start_doing 写的，都是她说了才有的，谈不上说早了
                logger.info("[super_enhance/world] 记录员说%s说早了 %s，但那段不是补写排的，不管", who, entry_id)
                continue
            said_at = self._said_at(items, raw.get("said"))
            if said_at is not None and said_at >= start:
                logger.info("[super_enhance/world] 记录员说%s说早了 %s，可那句话是 %s 说的，那段已经开始了，不管",
                            who, entry_id, said_at.strftime("%H:%M"))
                continue
            self._early_seen.add(entry_id)
            self._early.append((who, umo, dict(entry), clean_text(raw.get("said"), 120)))

    def _said_at(self, items, said) -> datetime.datetime | None:
        """记录员摘的那句原话是哪条消息说的：那条消息的时刻（不带时区的本地时间）；找不到就 None。"""
        probe = "".join(str(said or "").split())
        if len(probe) < 4:
            return None
        for item in items or []:
            text = "".join(str(item.get("text") or "").split())
            if text and (probe in text or text in probe):
                try:
                    return datetime.datetime.fromtimestamp(float(item.get("ts")), self.tz).replace(tzinfo=None)
                except Exception:
                    return None
        return None

    def _make_room(self, intention: dict, now: datetime.datetime) -> None:
        """新记了一条带时间段的打算：她那段时间里补写排好的事让开——没开始的撤掉，跨进来的截到打算开始的那一刻；
        对方写着和她一起的那几段也一样。空出来的由补写把打算排进去（顺带上对方）。
        时间段太长（超过 MAKE_ROOM_HOURS）或已经过了的不动，免得撤掉一大片。"""
        span = window_span(intention.get("window"), now)
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
                if start < lo:
                    self.world.update_entry(date_text, str(item.get("id")), end=fmt_time(lo), intent="", stopped=True,
                                            stop_by="到了说好去做的事：%s" % clean_text(intention.get("text"), 30))
                    done.append("%s %s 到 %s 为止" % (item.get("id"), clean_text(item.get("doing"), 20), fmt_time(lo)[11:]))
                else:
                    self.world.delete_entry(date_text, str(item.get("id")))
                    done.append("%s %s 撤掉" % (item.get("id"), clean_text(item.get("doing"), 20)))
        if done:
            logger.info("[super_enhance/world] 新打算 %s（%s %s）腾地方：%s", intention.get("id"),
                        self.label(who) or who, intention.get("window"), "；".join(done))

    def _touch(self, who: str, moment) -> None:
        """记下这次改到了她的现在、从几点起（取最早的）：apply 最后据此把她后面补写排的段撤掉重排。"""
        if moment is None or who not in set(self.raw_log.enabled_bots()):
            return
        touched = self.__dict__.setdefault("_touched", {})
        old = touched.get(who)
        touched[who] = moment if old is None else min(old, moment)

    def _go_now(self, intention: dict, now: datetime.datetime) -> None:
        """新记了一条「现在就去」的打算：那件事交给补写马上排。她手上那段要是补写排的、记录员这次没定怎么收场（多半是闲着），
        到她答应的那一刻为止；算改了她的现在，apply 最后把她后面补写排的段撤掉。已经睡下的觉不动。"""
        who = str(intention.get("who") or "")
        at = parse_time(intention.get("now_at")) or now.replace(tzinfo=None)
        if self.world.sleep_at(who, at) is not None:
            logger.info("[super_enhance/world] %s答应现在就去做「%s」，可她已经睡下了，睡觉不打断，只记打算",
                        self.label(who) or who, clean_text(intention.get("text"), 30))
            return
        cur = self.world.current(who, at)
        if cur is not None and not is_event(cur) and str(cur.get("source") or "") == "step" \
                and str(cur.get("id")) not in self._mentioned:
            date_text, entry_id = str(cur.get("start"))[:10], str(cur.get("id"))
            start = parse_time(cur.get("start"))
            if start is not None and start < at:
                self.world.update_entry(date_text, entry_id, end=fmt_time(at), intent="", stopped=True,
                                        stop_by="改去%s" % clean_text(intention.get("text"), 30))
            else:
                self.world.delete_entry(date_text, entry_id)
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
                        or str(before.get("source") or "") not in ("step", "chat") or str(before.get("id")) in self._mentioned:
                    continue
                name, mine = self.label(who) or who, self.label(other) or other
                doing = str(record.get("doing") or "")
                doing = doing.replace(mine, name) if mine and mine in doing else "和%s一起%s" % (name, doing)
                b_start = parse_time(before.get("start"))
                b_end = parse_time(before.get("end")) if before.get("end") else None
                if b_end is None or b_end > start:
                    if b_start is not None and b_start < start:
                        self.world.update_entry(str(before.get("start"))[:10], str(before.get("id")), end=fmt_time(start), intent="",
                                                stopped=True, stop_by="改去%s" % clean_text(doing, 30))
                    else:
                        self.world.delete_entry(str(before.get("start"))[:10], str(before.get("id")))
                seen = sorted((set(record.get("seen_by") or []) | {who}) - {other})
                twin = self.world.add_entry(str(record.get("start"))[:10], {
                    "who": other, "kind": "activity", "start": record.get("start"), "end": record.get("end") or "",
                    "place": record.get("place"), "doing": doing, "public": record.get("public"),
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
        写一句她中途走了，对方照旧接着做。手动写的、聊天里记的、这次刚记的、说早了要留着改口的、已经睡下的觉都不动。"""
        flat = now.replace(tzinfo=None)
        bots = set(self.raw_log.enabled_bots())
        fresh = self.__dict__.get("_fresh") or set()
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
                    if iid in self._mentioned or iid in self._keep or iid in fresh:
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
                self.world.update_entry(day, iid, doing=doing if note in doing else doing + note,
                                        **{"with": [x for x in (item.get("with") or []) if x != who]})
                freed.append("%s的 %s 只去掉%s" % (self.label(owner) or owner, iid, name))
        # 对方和她一起的段还没开始：对方从那段起往后补写排的都撤掉重排
        for day, item, owner, iid, start in movable():
            if owner in cutoff and start >= cutoff[owner]:
                self.world.delete_entry(day, iid)
                freed.append("%s的 %s 撤掉" % (self.label(owner) or owner, iid))
        if removed or freed:
            logger.info("[super_enhance/world] %s的现在改了（%s 起）：后面补写排的撤掉重排：%s%s", name, fmt_time(since)[11:],
                        "；".join(removed) or "（没有）", "；对方：" + "；".join(freed) if freed else "")

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
                self.world.update_entry(day, str(item.get("id")), doing=doing.strip("，, ") or str(item.get("doing") or ""),
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
                self.world.update_entry(day, str(item.get("id")), doing=doing if note in doing else doing + note,
                                        **{"with": [x for x in (item.get("with") or []) if x != who]})

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
            "meal": planned.get("meal"),
            "at_home": planned.get("at_home"),
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

    def _take_planned(self, token, who: str, start: str, end: str, now: datetime.datetime) -> dict | None:
        """记录员说她这会儿做的就是补写排在后面的那段（same_as）：把那段撤下来，返回要接着用的字段。"""
        entry_id = str(token or "").strip()
        if not entry_id:
            return None
        date_text = self.world.date_of(entry_id, who, now)
        planned = next((x for x in self.world.entries(date_text, who) if str(x.get("id")) == entry_id), None) if date_text else None
        if not self._movable(planned, now):
            logger.info("[super_enhance/world] 记录员说%s这会儿做的是 %s，可那段不是还没做完的补写段，照常另记", who, entry_id)
            return None
        self.world.delete_entry(date_text, entry_id)
        out = self._carry(planned, start, end)
        out["moved_from"] = {"date": date_text, "entry": dict(planned)}
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

    def _apply_stale(self, raws, early, now: datetime.datetime) -> dict:
        """记录员说对不上了的段（补写排的、主人让做的、之前从聊天里记的；手动写的不动），照它定的收场办（how）：
        做完（先做完再走，算做完）、收尾（先关火盛出来这种再走，算没做完）、交给（交给另一个人接着做）、放下（到此刻为止）、
        没做（压根没做，整段撤掉；要是从补写排的那段挪过来的，原来那段恢复）、接着做（一起做的另一个人一个人接着做）。
        还没开始的段（接着做以外）撤掉。之后跟着处理（_follow_up）：靠没做成的东西的、接着没做成的事往下做的、还没开始的补写段撤掉。
        一起做的另一个人那段记录员没提的，等这次全记完再看（_redo_after：她真走了才只把她去掉、写一句她中途走了）。
        改到的段记进 _touched（谁、几点起改的），apply 最后把她后面补写排的段全撤掉重排。
        返回 {谁: 什么时候腾出手来}（收尾、做完、交接完的时刻），这次的新条目从那时开始。"""
        flat = now.replace(tzinfo=None)
        bots = set(self.raw_log.enabled_bots())
        # 同一段又被说成「说早了」的：要叫她改口，那段得留着
        keep = {str(x.get("entry") or "").strip() for x in early if isinstance(x, dict)}
        mentioned = {str(x.get("entry") or "").strip() for x in raws if isinstance(x, dict)}
        self._mentioned = getattr(self, "_mentioned", set()) | mentioned
        free_at, pending, orphans = {}, [], []
        later = [x for x in raws if isinstance(x, dict) and self._how(x) == "接着做"]
        for raw in [x for x in raws if isinstance(x, dict) and self._how(x) != "接着做"] + later:
            who = str(raw.get("who") or "").strip()
            entry_id = str(raw.get("entry") or "").strip()
            if who not in bots or not entry_id or entry_id in keep:
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
                if str(raw.get("doing") or "").strip():
                    fields["doing"] = clean_text(raw.get("doing"), 120)
                self.world.update_entry(date_text, entry_id, **fields)
                # 走了的那个人段里没做到的那几步标的吃的、东西（两人一起的饭常只标在一个人那段里）：跟到这段上，她做完就算
                moved = []
                for leaver, tags in orphans:
                    if leaver in was_with and tags:
                        moved.extend({k: v for k, v in tag.items() if k != "step"} for tag in tags)
                        tags.clear()
                if moved:
                    current = next((x for x in self.world.entries(date_text, who) if str(x.get("id")) == entry_id), entry)
                    self.world.patch_entry(date_text, entry_id, {"things": list(current.get("things") or []) + moved})
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
            after = self.world.patch_entry(date_text, entry_id, fields) or entry
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
            "origin": {"kind": "continue", "text": str(after.get("doing") or "")}, "created": fmt_time(now),
        })
        if record is not None:
            # 这次刚交过去的那段：撤后面补写排的段时不算在里面
            self.__dict__.setdefault("_fresh", set()).add(str(record.get("id")))
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

    async def _flush_early(self, now: datetime.datetime) -> None:
        """攒下的「说早了」逐条交出去叫她改口；出错不影响记录员。"""
        pending, self._early = self._early, []
        if self.on_early is None:
            return
        for who, umo, entry, said in pending:
            try:
                await self.on_early(who, umo, entry, said, now)
            except Exception:
                logger.warning("[super_enhance/world] 叫她改口没排上", exc_info=True)

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

    def _thing_stage(self, raw: dict, umo: str, items, day: str, now: datetime.datetime) -> dict:
        """记录员写的一步 → 存货里的一步；src 对回原话是哪条消息、谁说的。"""
        hit = self._said_item(items, raw.get("quote"))
        said = norm_time("%s %s" % (hit.get("date"), hit.get("time")), day) if hit else ""
        src = {"umo": umo, "quote": clean_text(raw.get("quote"), 80)}
        if hit:
            src["at"] = said
            if str(hit.get("role") or "") == "assistant":
                bot = bot_of_umo(umo)
                src["who"] = (self.label(bot) or bot) + ("（主动发的）" if str(hit.get("src") or "") == "proactive" else "")
            else:
                src["who"] = "主人"
        return {
            "at": norm_time(raw.get("at"), day, fallback=said or fmt_time(now)),
            "what": raw.get("what"),
            "left": raw.get("left"),
            "place": raw.get("place"),
            "keep": raw.get("keep"),
            "state": raw.get("state"),
            "amount": raw.get("amount"),
            "used": raw.get("used"),
            "holder": self._by(raw.get("holder")) if raw.get("holder") else "",
            "owner": self._by(raw.get("owner")) if raw.get("owner") else "",
            "by": self._by(raw.get("by")),
            "note": raw.get("note"),
            "src": src,
            "recorded": fmt_time(now),
        }

    def _apply_things(self, raws, umo: str, items, now: datetime.datetime, cat: str = CAT_FOOD) -> None:
        """新的一件先建（临时代号换成程序发的编号），再给已有的加步；做菜用掉的料两头都记上（菜的 from、料的 into）。
        对照的是此刻现算的存货（含世界线里做完的段）；聊天里说到了世界线里的那件，就把它存下来（从此算确定有的）。
        cat=item 时记的是吃的以外的东西（第 19 条的 items）：另带 look、owner，不替料补「用掉」。"""
        store = getattr(self, "things", None)
        if store is None or not isinstance(raws, list) or not raws:
            return
        flat = now.replace(tzinfo=None)
        day = str((items[0].get("date") if items else "") or flat.date().isoformat())
        view = store.view(flat)
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
            stage = self._thing_stage(raw, umo, items, day, now)
            if bad_time(stage):
                logger.warning("[super_enhance/things] 新的一件 %s 时间不对或晚于现在，不记：%s", raw.get("name"), stage.get("at"))
                continue
            moment = parse_time(stage["at"])
            gap = datetime.timedelta(minutes=SAME_NEW_MINUTES)
            same = find_same([x for x in view.values() if cat_of(x) == cat], raw.get("name"),
                             norm_step(stage.get("what"), cat), moment - gap, moment + gap)
            if same is not None:
                keys[key] = resolve(same.get("id"))
                logger.info("[super_enhance/things] %s 已经有了（%s），不再新建", raw.get("name"), same.get("id"))
                continue
            sources = []
            for token in raw.get("from") or []:
                text = str(token or "").strip()
                source = store.get(keys[text]) if text in keys else view.find(text)
                if source is None or not usable(source, stage["at"]):
                    logger.warning("[super_enhance/things] %s 的用料 %s 对不上（没有这件，或那会儿还没有 / 已经没了），不写进 from",
                                   raw.get("name"), token)
                    continue
                sid = resolve(text)
                if sid and sid not in sources:
                    sources.append(sid)
            item, why = store.create(raw.get("name"), raw.get("kind"), raw.get("origin"), sources, stage, fmt_time(now),
                                     cat=cat, look=raw.get("look") or "", owner=self._by(raw.get("owner")) if raw.get("owner") else "")
            if item is None:
                logger.warning("[super_enhance/things] 不记 %s：%s", raw.get("name"), why)
                continue
            keys[key] = str(item["id"])
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
                logger.warning("[super_enhance/things] 没有 %s 这一件，「%s」不记", raw.get("id"), raw.get("what"))
                continue
            stage = self._thing_stage(raw, umo, items, day, now)
            if bad_time(stage):
                logger.warning("[super_enhance/things] %s 的「%s」时间不对或晚于现在，不记：%s", tid, raw.get("what"), stage.get("at"))
                continue
            into = resolve(raw.get("into")) if raw.get("into") else ""
            if into and into != tid:
                stage["into"] = into
            why = store.add_stage(tid, stage)
            if why:
                logger.info("[super_enhance/things] %s 的「%s」不记：%s", tid, raw.get("what"), why)
                continue
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
                # 做菜那会儿已经记了它用掉多少（只是没写 into）：不再补一步空的，把剩多少盖掉
                if made_at is not None and any(
                    x.get("what") in USE_STEPS and parse_time(x.get("at")) is not None
                    and abs((parse_time(x.get("at")) - made_at).total_seconds()) <= SAME_STEP_MINUTES * 60
                    for x in source.get("stages") or []
                ):
                    continue
                why = store.add_stage(sid, {
                    "at": first["at"], "what": "用掉一部分", "by": first.get("by"), "into": dish["id"],
                    "note": "做这盘菜时用了，聊天里没说剩多少", "src": first.get("src"), "recorded": fmt_time(now),
                })
                if why:
                    logger.info("[super_enhance/things] %s 做进 %s 那一步补不上：%s", sid, dish["id"], why)
