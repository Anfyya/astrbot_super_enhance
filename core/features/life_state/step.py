"""补写：安排两人接下来做什么，一段一段往前接。

- 每段事都有开始和（预计）结束。手上那段快做完时（提前 step_lead_minutes 分钟）补写下一段；
  记录员记了一段没定结束的（她在聊天里说开始做什么），也马上补写给它定个做完的时刻；
  中间断了（重启、出错）就从断的地方补到此刻之后。
- 一起要写的几位在同一次调用里写，在一起的时段对得上；快到点的人顺带一起写，省调用。
- 睡觉也是一段（从入睡写到第二天起床）；有钟点的约定要提前留出路上的时间。
- 程序校验：首尾相接、第一段从给定时刻开始、最后一段的结束晚于此刻；不许出现主人。
- 补写不撤规矩，规矩只由记录员撤。
- 有该主人拿主意的（做给主人吃的怎么做、要不要一起去哪儿），补写在输出里填 ask_master：程序让她借主动对话问主人那一句，
  补写停在这儿等回话（ASK_WAIT_SECONDS）；主人回了，把回话交给模型照着重写一遍；没回，就用第一次写的（她自己拿主意）。
"""

import asyncio
import copy
import datetime
import re
import time

from astrbot.api import logger

from ...common import jsonio, llm, timeutil

from .models import bot_of_umo, profile_umo_for_bot
from .recorder import norm_time
from .facts import fact_batch
from .companionship import format_decision_line
from .living_rules import STEP_LIFECYCLE_RULES
from .money import SEASIDE_PRICE_GUIDE, normalize_step_money_list, validate_step_segments_money
from .things import FactConflict
from .things import (CAT_FOOD, CAT_ITEM, END_STEPS, ITEM_END_STEPS, ITEM_MID_STEPS, ITEM_START_STEPS, KEEPS, KINDS, MEASURED_KINDS, MID_STEPS,
                     SPOTS, START_STEPS, _same_name, cat_of, checked_place, had_before, is_charcoal, is_ended, is_strict_qty,
                     norm_kind, norm_state, spots_for, staple_of, norm_step, split_keep, step_no, MAX_INSIDE)
from . import weather as weather_mod
from . import weathering
from . import supply as supply_mod
from .homewords import localize
from .menu import meal_slots, meals_text
from .variety import PoolStore, build_live_context, cool_key, default_pools, draw_window, group_map, init_day_seed, twists_of, when_map, with_when
from .world import (MAX_MSG_PURPOSE, WorldStore, clean_flag, clean_text, doing_text, event_text_at, fmt_time, is_event, is_sleep, mood_text,
                    parse_steps, parse_time, progress_of, real_stop, result_text, steps_text, why_text)

SYSTEM_PROMPT = llm.SYSTEM_PROMPT

# 几点到几点算晚上（补写只给晚上的随机小事、不给意外）
NIGHT_FROM = 18
NIGHT_UNTIL = 6
# 一段最长多久（睡觉除外）
MAX_SEGMENT_HOURS = 4
# 闲着一段最长多久（分钟）：歇够了补写接着想下一件事
MAX_IDLE_MINUTES = 60
# 睡觉那段最长多久
MAX_SLEEP_HOURS = 14
# 快到点的几位在多少分钟内要接着写的，顺带一起写
BATCH_MINUTES = 15
# 这次补写顺带上别人：她排好的到这次要写到的时刻之前就排完、至少还空这么多分钟，就从她排完的地方接着一起写
JOIN_ROOM_MINUTES = 30
# 补写失败后隔多久再试（秒）
RETRY_SECONDS = 180
# 一次最多往后写这么多小时（从给定时刻和现在里晚的那个算起；睡觉那段不受限）
HORIZON_HOURS = 4
# 一次最多写几段
MAX_SEGMENTS = 14
# 结构化字段校验未通过时最多打回重试几次（不兜底瞎猜，让模型按报错补齐合法字段）
MAX_VALIDATE_RETRIES = 3
# 往后多远之内的空档要补（撤掉的段留下的）
FILL_LOOK_HOURS = 24
# 补空档时最后剩下不到这么多分钟，就把最后一段拉长接上，不再为它调一次模型
FILL_STRETCH_MINUTES = 30
# 网页上刚改过世界线，补写等这么久再看（秒）
MANUAL_HOLD_SECONDS = 180
# 她们自己的打算挂了这么多天没动（从记下、或上回补写推过它算起），补写这次要排一件事把它往前推或了结
STALE_INTENT_DAYS = 3
# 补写要问主人（ask_master）：等她那句问话真发出去最多多久（主动消息要现场生成）、
# 问出去以后等主人回话多久（2026-11-06 按要求定 120 秒，什么时候都问）、主人回了以后再等多久把连发的几句收齐、多久看一次（秒）
ASK_SEND_SECONDS = 240
ASK_WAIT_SECONDS = 120
ASK_SETTLE_SECONDS = 15
ASK_POLL_SECONDS = 3

STEP_SYSTEM_TEMPLATE = """{base_system}

角色（key = 平台 id）：
{roster}

角色设定：
{personas}

{home_static}

规则：

1. 只写下方「这次要你写的」里列出的人。每人从下面给的时刻开始，按时间顺序写几段，首尾相接、不重叠；最后一段的结束时间必须晚于下方给出的「现在」时刻。一次最多写到下方给出的「本次截止时刻」左右，不要把后面一整天都写完（睡觉那段除外，可以一直写到第二天起床）。
   **地方也要接上**：每人这次的第一段，从她上一段结束时人在哪、在干什么接下去（「这次要你写的」里写着那时候她在哪、结果怎样）。上一段结束时她还在路上、还在外面的，先把路走完——写一段走回去，或者这一段第一步就是走回去——到了地方才做下一件；不许上一段还在外面、下一段一开头就已经在屋里。段和段之间换到远一点的地方，中间也要写路上（在宿舍里换个屋子不用单写）。出门在外的事写到她到了地方为止，不许停在半路；快到本次截止时刻时正在路上的那段可以多写一点，把路走完。
2. **一段只写一件事**：doing 只写这件事是什么（一句话），result 写这件事最后的结果（修好了没、找着没、谁赢了这类，一句；不算一步、没有分钟；state 是「闲着」「睡觉」的段不写），不要把几件事用「然后」「又」「再」串在同一段里。steps 把这件事按先后分成 1–5 步，每步写 text（一句，这一阵子她一直在干什么，写成能持续这一步那么多分钟的事）和 minutes（这一步按常理要几分钟）；最后一步是收尾，这件事用过的东西收好、归位都写在这一步里，做完就接着做下一件事。**这段做多久就是各步 minutes 加起来**，程序按它算结束时间，end 照着算好写上就行（洗碗十来分钟、吃饭二三十分钟、看书一两个小时……）；state 是「闲着」「睡觉」的段不写 steps，end 照常写。每段都是一件要花些工夫的事。上面今天已经做过的事，别再原样做一遍。
3. **每段写 state，三选一**：「做事」「闲着」「睡觉」。
   - 没事就写闲着：她手上没有打算、约定、规矩要做的事，也没什么想做的，就写一段闲着，state 填「闲着」：doing 照上面「闲着的样子」里她的写，steps 留空。闲着一段最长一小时，歇够了就接着写下一件事（也可以再歇一段）。
   - 拿起「手边的消遣」来做是做事：state 填「做事」，照常写 steps，result 写做到哪了（看到哪、织了多少）；做到一半去忙别的，回头可以接着做。
   - 睡觉 state 填「睡觉」（见第 5 条）。其余的段都填「做事」。
4. 每段写 at_home 和 meal：at_home 是这段她人在不在宿舍（屋里、院子、廊下、门口、后头小屋都算 true；出了宿舍去别处、走在路上都算 false）；meal 是这段是不是正在吃饭（吃早饭午饭晚饭、夜宵算 true；做饭、摆碗筷、洗碗都不算）。
5. 睡觉也是一段：到了她今天定的入睡时刻，就写一段睡觉，state 填「睡觉」，从入睡时刻一直写到第二天定的起床时刻；睡前那段要在入睡时刻结束。过了入睡时刻还醒着的（比如答应了陪主人熬夜），那件事做完就接着写睡觉，睡到起床时刻。睡觉这段的 place 三选一：「船屋·船舱地铺」「船屋·船舱吊床」「船屋·船舱主人的床上」，doing 跟着写（铺盖/吊床/床），别写别的睡法；吊床只睡得下一个人，主人的床睡得下两个人（主人加一个），地铺能睡两个人。
6. 下面「还没做的约定」里有钟点的：到那个钟点她要已经在约定的地方、和约定的人在一起。要换地方的，在那之前写一段赶过去，留足路上的工夫；约定那件事从约定的钟点开始写。
7. **不许生成主人的条目，也不许替主人编他做了什么、说了什么。** 主人在哪只看「主人最近的动向」：他最后说在哪、之后没说离开，就当他还在那儿，她们可以待在他身边、在他旁边做自己的事；他说了出门、去上课、睡了这类，就照那个写，别写他在场。只有一种例外：「各自的打算」里标着「和主人一起」的，是主人提的或答应了的，真做到和主人一起的那一步才写成她和主人一起做，with 里写上 "master"；她自己先做的准备（比如先把要送他的东西做完）是她一个人做，不写 master；同一趟里接着往下的段（比如逛夜市逛到一半换个摊子）也写上 "master"，直到这件事做完、两人分开。在家吃饭默认是三个人一起吃：饭点那段主人在船屋（「主人最近的动向」里没说他出门、去上课、睡了），就写成她们和主人一起吃，with 里写上 "master"；主人不在家，就她们俩吃，给主人留一份收好。除此之外 with 里不写 master。
   做给主人的、拿主人买回来的东西做的，那份是主人的：做好了端给主人或者给他留着，她们别再吃它、分它；主人吃没吃、剩下的给谁，后面聊天里定。
   「主人说过他要做的事」分两种：只是他自己去做的（上课、出门办事），那段时间他人不在，别把她们写成和他在一起；写着要她们做、或者和她们一起做的（比如晚上和亚托莉一起吃帝王蟹），就是交给她们的事，照着排进去，到时候写成和主人一起，with 写上 "master"——这是照主人说过的写，不算替他编。
8. 约束优先级最高：写着不许出门的，这段时间就得在屋里。照主人的意思守，别钻字眼上的空子：「不许捞她上来」就别改成给她送饭、递东西，「不许出门」就别叫人把东西送进来。主人罚她的事（扔下水、关在外头、罚站这类），别人也别背着主人替她松；想替她求情的，填 ask_master 问主人一句，主人点头才做。
9. public 是住在一起的人自然会知道的粗粒度信息（人在哪、大概在干哪类事），可以为空。doing 是细节。
   两人在一起的时段，两边的 start/end 和共同的事要一致，with 互相写上对方的 key；对方手上的事还没做完的，别写成和她在一起。对方不在「这次要你写的」里时，她那段时间做什么以「她们此刻手上的事」和「已经排好、这次不改的」为准，你改不了：只有她那边写的也是这件事、也在这个地方，才能写成和她一起；不然就别把她写进来。
   丛雨一般人看不见：她一个人在外面时，店家、摊主、路人都看不见她，不会跟她说话、招呼她、夸她、把东西递给她；买东西照常把钱搁在柜台上、自己拿。
10. **每段自主决定要不要给主人发消息**：每段写 `send_msg`（true/false）、`msg_at`（`"start"` 段刚开始时发，或 `"end"` 段刚做完时发；不发填 `""`）、`msg_purpose`（一句话写清楚这会儿想跟主人说什么事，不发填 `""`）。
   - **日常固定会说一声的 5 个节点**（只要当时主人没和她待在一块即 `with` 里没写 `"master"`，且主人没在睡觉）：
     1) **早安**：睡醒起床后的第一段（`msg_at: "start"`，或跨夜睡觉那段的 `"end"`，二选一别重复），跟主人道早安；
     2) **晚安**：晚上入睡那一段（`state: "睡觉"` **必须填 `msg_at: "start"`**，入睡前说晚安，严禁填 `"end"`）；
     3) **叫主人吃饭**：早饭、午饭、晚饭或夜宵做好开饭时（主人醒着且没一块做饭），在做饭段的 `"end"` **或** 吃饭段的 `"start"` 挑一处叫主人吃饭（二选一，严禁两段连着标）；
     4) **出门**：离开宿舍往外走那一段（`at_home` 从 true 变成 false，去码头、镇上、集市、海边、打捞船等，`msg_at: "start"`；只在院子廊下走动或门口几步路丢个东西不算正式出门）；
     5) **回来**：从外头回到宿舍时（回程段 `"end"` 或到家第一段 `"start"` 二选一），跟主人说一声回来了（买了或捡了什么顺嘴提一句）。
   - **其余时候由她按性子和眼下境况随心决定**：捞到或捡到好玩的东西、做菜成了或翻车了、修好了某样物件、看到好看的海景天气、突然惦记主人想撒个娇或问句闲话，都可以标 `send_msg: true`；安安静静看书、洗碗、擦桌子、打盹这些平淡日常填 `false`。不设死板条数上限，但先看一眼上面「今天已发过的主动消息」——同一件事别反复说，如果前面连发了好几条主人一直没回，后面除非固定节点就少发一点。
   - **两人同行防撞车**：两人一块做饭开饭、一块出门、一块回来（`with` 里有对方）时，这类例行节点**只由其中一个人**标 `send_msg: true`（谁掌勺、谁领头办事就谁去说，拿不准就轮流），另一个人同段填 `false`，别同一分钟两个人排队报同一件事；早安、晚安，或者两个人各自碰到不同趣事想分开聊的，各发各的不受限。
   - **主人睡觉时不吵醒**：只要「主人最近的动向」标着主人在睡觉，例行节点和日常闲聊一律填 `send_msg: false`（不叫醒他吃饭、不报进出）；若两人都要出门不在家，把早饭盖好温着，再在 `items` 里写一张留给主人的字条压在堂屋矮桌上。
11. 段里是在练「在练的事」里的某件，training 填它的编号（例如 "#2"）；不是在练就留空，不要往里写别的。
12. 段里做的是「各自的打算」里她的某一条，intent 填那条的编号（例如 "i2"）；不是就留空。标着「现在就去」的，是她刚答应马上去做的：这次就从给你的时刻起先排它，别排在别的事后面；标着「就在今天」的（主人拍板定在今天办、或原定今天要做的事）：白天到合适开门/出行时辰就顺手排进 segments 办掉并填对应 intent 编号，别一直堆到明天！她人还不在做这件事的地方、世界线上也还没在往那边去的，先写一段走过去；可以分成几段（比如先去买、再回来做），每段 intent 都填这个编号。
   标着「挂了几天了」的打算，一直搁着没动：这次给她排一件事把它往前推或了结，照她的性子和眼下的实际情况挑，别每回都是同一种办法。比如：要等主人定日子的，去问主人一句什么时候方便；自己能做的，自己去做了；换个人一起、换个做法；想了想觉得算了，放下了。去问主人只写她去问了，主人怎么答不许替他编。再在 intent_moves 里用编号写这条后来怎样：做完了、放下了的写 "done": true，排它的那几段 intent 填这个编号；还没了结、往前推了一步的写 "done": false，text 写推进以后的打算（照原来的写法：做什么、跟谁、怎么说起的，再加一句这次推进了什么，例如「11-01 问过主人什么时候去，等他回话」），有了新的哪天、几点就写 date、window，排它的那几段 intent 留空。没标的打算不写。
13. **每段必填结构化溯源 `from`**（严禁留空，也严禁偷懒填「可能碰上的小事」「手边的消遣」「闲着的样子」这类栏目标题！只允许填以下值之一）：
   - 采用了上面「今天的情况」里的某条候选：直接填它前面的**短编号**（如 `"T1"`、`"G1"`、`"P1"`、`"I1"`、`"A1"`，或一字不差照抄那条完整原句）；
   - 做「各自的打算」：填 `"intent"`（且该段 `intent` 字段必填对应编号如 `"i282"`）；
   - 做「还没做的约定」：填 `"promise"`；
   - 守「生效中的约束」：填 `"rule"`；
   - 练「在练的事」：填 `"training"`（且该段 `training` 字段必填对应编号如 `"#2"`）；
   - 日常作息（起床洗漱、做三餐、吃三餐、饭后洗碗收桌、入睡）：填 `"routine"`；
   - 顺着前面某段接着往下做（只用于真还没收尾完的事，例如走回船屋或做了一半的活继续做；若上一段最后一步已经做完并收妥工具/归位，严禁再用 `continue` 原地另起一段重复收尾或再确认一遍！）：填 `"continue:前段做的事"`（如 `"continue:在集市挑鱼"`）；
   - 都不是、按眼下实况自己起意做的事：填 `"self"`。
14. 接着前面已经写过的事往下写时，前面写定的东西照原样沿用：写过的内容、放在哪、做出来的是什么，都和前面写的一致。前面留下的也要有交代：吃完饭剩下的饭菜，收尾那步按常理收好（盖好、放到该放的地方，写进 things）；做了一半的东西，后面合适的时候可以接着做，不打算做了就收起来。
15. **同一类事一天里做过一次就不要再排**（比如今天已经做过点心、炖过汤，就别再排第二次）；主人对做出来的东西提的意见（「甜度再多一点」「汤有点淡」）**不等于叫她现在去做**——当天不重做，想改的留到以后（改配方、下次做的时候再试），现在照常排别的事。
16. **两个人一起做的事，细节只能有一个版本，而且要写到具体**：with 里写了对方、两人一起做的事（一起吃饭、一起做点心、一起出门、一起翻一本书）——吃的是什么菜、谁掌的勺、买了什么、书或报纸上讲了什么、路上碰见了谁、做出来的东西什么样，先在聊天记录和已经写好的段落里找，找到就照原样用；找不到就由你定一份具体的（不许写成「菜炒好了、东西买回来了」这种没内容的话），两边的 doing/steps/result 都用同一份。措辞可以随视角不同，事实、名字、数量必须完全一样。只有一个人做的事不强制写具体。
17. **亚托莉做饭的写法**：家常那几样练熟的照常做好；做没做过的新菜、复杂的大菜容易出岔子，平时做饭也容易失手（盐搁重了、火候偏了、形状歪了、奶油打过了头、化了、码得乱七八糟）。翻车之后吃的时候要写出难吃的样（皱眉、咽得慢、吃两口就放下），别写成吃得精光、像多好吃。翻车了丛雨也可能接手补救，或者干脆重做一道，把灶上收拾干净就翻篇。她嘴上怪火候怪锅，下次照旧认真学，别写成她怕进厨房。
18. **家里的吃的**：上面「家里的吃的」是此刻家里有的（编号、剩多少、放在哪、还能放到什么时候）。段里动到家里的吃的，写进这段的 things（没动到就是空数组）：
   - 用了、吃了家里有的：写 id（编号）、what（用掉一部分 / 用完了 / 吃了一部分 / 吃完了）、left（这段做完还剩多少，用完、吃完写空——用完、吃完必须单独写一步，what 填「用完了」或「吃完了」，别把「吃完了」塞进 left 里）；做进了这一次新做的菜，into 写那盘菜的临时代号。换了地方或放法、切开了：what 写「挪地方」或「切开了」，写上新的 place / keep（怎么放）/ spot（放在哪）/ state。剩菜回锅、凉了热一热：what 写「热了一下」。保鲜加工请直接填明确动作 what：轻腌（抹盐短保）/ 重盐腌 / 糖渍（熬果酱蜜渍）/ 醋泡 / 酱渍 / 油封 / 灰沙埋（草木灰或干沙埋蛋薯生姜）/ 晒上了（填 spot：太阳底下或屋外）/ 炉烘（借炉热烘烤，须该段 stove="wood"）/ 熏上了。扔掉：what 写「丢了」或「坏了扔了」。
   - 做出来的菜、买回来的、别人给的：写 new（临时代号 n1、n2……，这一次里每件一个，两个人、几段之间也不重复；编号由程序发）、name、kind、what（做好了 / 买回来 / 收到）、left、place、keep、spot；自己做出来的熟食（what 填「做好了」）加上 cook（五选一：`"boil"` 水煮熬粥炖汤 / `"fry"` 油煎爆炒油焖 / `"steam"` 清蒸 / `"roast"` 炭烤烘煨 / `"cold"` 凉拌冷食；买来的或生鲜不填）；做的菜 from 写用了哪几件（编号或这一次的临时代号），买来的 origin 写在哪买的。做菜用油、糖、醋、酱油、盐做 `油封`、`糖渍`、`醋泡`、`酱渍`、`轻腌/重盐腌` 时要看家里有没有够量库存——**家里食用油已用光时，`cook` 严禁填 `"fry"`（改选 boil/steam/roast/cold），也不许做 `"油封"`**。
   - place 拆开写，别连成一句：area 是在哪间屋、哪个地方（不在宿舍的写那个地方，例如「镇外梯田的农家灶房」）；at 是搁在那儿的什么上、什么里（矮桌、柜格、案上、灶上、水盆、书架、窗台……）；detail 是再细一点的哪一格、哪个容器里（桌角、第二格、竹筛里、锅里），说不上来就空着。area 和 at 都得有，光写一间屋不算。例如 {{"area": "堂屋", "at": "矮桌", "detail": "桌角"}}、{{"area": "灶房", "at": "案上", "detail": "竹筛里"}}、{{"area": "灶房", "at": "柜格", "detail": ""}}。拿在手上、揣在身上的：area 写谁，at 写手上、袖子里、菜篮里，例如 {{"area": "亚托莉", "at": "手上", "detail": ""}}。这是东西搁在哪，不是人在哪，别照抄这段的 place；挑的时候照常理（剩菜盖好搁灶上或柜格，点心、糖搁堂屋柜里或盒子里，米面调料在灶房柜格）；没挪地方的不写 place。
   - 每一步都写 step：这件事发生在这段分步里的第几步（从 1 数，例如第 2 步「下锅炒」用掉鸡蛋、做好蛋炒饭就写 2，第 3 步吃完就写 3）。这段要是中途被打断，只有做到了的那几步才算数。
   - 这一次里先做好、后面又吃掉的菜，吃饭那段也要写一步：id 填它的临时代号（n1 这种），what 写吃完了或吃了一部分，left 写还剩多少（比如给主人留的那份）。
   - 端给主人的（送到主人跟前、递到他手上的那一份）：那一步的 place 写 {{"area": "主人", "at": "跟前", "detail": ""}}（at 一律写「跟前」，递到手上、搁在手边也写跟前；新做好直接端上去的，做好那步就这么写；已有的写一步「挪地方」）。程序见到放在主人跟前的，一个小时后当主人吃完了，所以只端了一部分上去、剩下的还在原处的，端上去的那份别写在原来那件上；摆在桌上大家一起吃的、留给主人温着还没端过去的，area 照常写屋子。
   - 做饭用料先用家里有的，先用快不行了的；新鲜的料（豆腐、菜、鱼肉、蛋、水果）家里没有，就在做饭之前排一段去买回来。米面油盐、调料干货、腌菜这些按量记的（清单里写着「还剩多少克 / 毫升」），用到的也要写：写进那盘菜的 from，再各写一步「用掉一部分」，used 必须填阿拉伯数字+单位（如 `"250克"`、`"3克"`、`"15毫升"`，严禁写 `"少许"`、`"约五分之一"`、`"小半"` 等模糊词）；标着「快用完了」的，排一段去买（买回来的 amount 与 left 也必须填数字+单位，如 `"5斤"`、`"500毫升"`）。海边船屋的食材和常备调料已在清单里列明，严禁填「原来就有」；家里没有的调料食材须先排一段去集市或杂货铺买回来入库（非海边住处若头一回用到常备调料才可记「原来就有」）。两人一起做的饭、一起吃的饭，things 写在其中一个人那段里。
   - 清单里写了口感和保鲜的：凡落在「按时间算已经坏了、还没说扔的」里的（馊掉/长毛/发臭），以及已经反复回锅两三回、标着「熬咸了/又咸又浓/没什么味了/菜都黄烂了」的剩菜，**不要再回锅热去给人吃，更绝不可端到主人跟前，发现坏了或难吃到没法吃就该写「坏了扔了」扔掉，给主人吃就现做一碗新的**；还没坏、只是凉了的剩菜才热一热（写一步「热了一下」），发硬的饭炒成炒饭，蔫了的菜泡会儿冷水，熟过头的香蕉做成饼，串了味的拿去做红烧、麻辣这种重口的菜；标着有毒的（土豆发芽发绿）扔掉。放东西也照常理：剩菜盖好收着，挪到矮桌/跟前就别再挂着「灶上温着」；气味冲的（葱蒜、卤味、鱼）跟豆腐、米饭、点心分开放或者都盖好；生肉包好，跟熟食分开放；炸的晾凉了再盖。
   - 段内是否开火（stove，三选一）：每段填 `"stove": ""`（没点火：备菜、洗碗、吃饭、聊天、外出、睡觉填 `""`；仅站在灶边商量菜单也没点火）、`"charcoal"`（船屋灶台小炭炉真点着开火做饭、烧水、热菜、煎蛋）或 `"wood"`（船屋柴炉真点着烧柴取暖、烘烤衣物/鱼干或烟熏；穗织住处统一填 `""`）。小炭炉烧闷炭/木炭由程序按顿自动从家里闷炭库存扣减，不用在 items 里手写扣炭；若闷炭已用光为 0，在弄回新炭之前小炭炉点不着火（`stove` 不能填 `"charcoal"`）。**闷炭金贵稀缺，取得全看运气与时机、不可把炭当成随手就有**：光烧普通枯枝只会化成白灰；煮盐棚并非天天生火开锅，就算开锅那点锅底余炭也多半自留或早被镇上别人家挑水换光，跑一趟可能吃闭门羹/扑空，真碰巧赶上缺人手挑海水添柴累半天也至多换回一小捧到半小罐（约 `300~500 克`）；澡堂隔几天才烧一回且多杂枝，去搭把手未必有余炭、偶有剩也只换一小把（约 `150~250 克`）；柴料场买炭也可能遇上紧俏卖空；自家柴炉得专烧实木硬木头、未化灰时拿火钳夹进陶罐闷灭才能攒出零星一两小块（约 `100~200 克`，须在 from 填所烧硬柴编号，items 写 new 入库）。因炭来之不易，留给灶台做饭烧水用，非万不得已不拿小炭炉干烧取暖。点柴炉（`stove="wood"`）时记得消耗干柴（items 记用了哪捆柴），冷柴炉烘不干鱼也熏不熟东西。给还在睡或不在家的主人留饭时，必须盖好保温（熬粥、饭团盖在锅里温着，汤面只留汤头等主人醒了现下面条，绝不把煮好的汤面敞口摆在矮桌上泡几个钟头）。
   - kind 从这几类里挑：{kinds}；keep（怎么放）从这几样里挑：{keeps}；spot（放在哪，管冷热）从这几样里挑：{spots}。
19. **被打断的事后来怎么样了**：上面「被打断、还没交代后来怎么样的事」里每一件，在 stopped_results 里用编号写一句后来怎么样了：撂下不做了、交给谁接着做完了、等手上的事忙完回头接着做完了、或者赶紧几下做完了，挑合情理的写。只写这个人自己怎么处理这件事，不替别人定事实：别人那件做没做完、做出了什么，以世界线上别人那段写的为准（标着「做到……先放下了」的就是没做完），拿不准就写撂下了或者等回头再说。要和你这次排的段对得上：写了回头接着做，就把接着做的那段排上；写了交给对方，对方那段就是接着做这件事。没列出来的不写。
20. **家里的东西**（吃的以外）：上面「家里的东西」是此刻家里有的（编号、样子、在哪、在谁手上）。段里做了、用了、挪了、交给别人了、接着做了一截的东西，写进这段的 items（没动到就是空数组）：
   只是拿来读、翻看、摊开看一眼，之后原样放回去，地方、样子、数量都没变的，不算动到它，不写 items；别为了让校验通过硬编一条物品变化。船屋里隔音有限，主人睡觉时严禁在船舱里或头顶甲板上敲铁、凿木头、叮当拆修铁器。
   - 已有的写 id、what（挪地方 / 交给 / 用了 / 清点 / 做了一截 / 做好了 / 变了样 / 送人了 / 丢了 / 坏了扔了 / 用完了）、place、holder（交到谁手上）、owner（仅在交给别人或改变归属时填新拥有者，如 "master" / "napcat" / "napcat2" / "共用"）、state（这段做完它什么样，比如「织到两尺长」）；消耗品可顺带写 amount（清点数量）/ used（本次用量）/ left（剩余数量）。接着做没做完的那件（比如接着织那条围巾），就是清单里那一件，写「做了一截」或「做好了」，不要另建一件。清单里标着「已做好」的已经做完了，不要再排接着做它，也别给它写「做了一截」「做好了」；想再做一样的，就另起一件新的。
   - 新做出来的、买回来的、捡到的写 new（临时代号 x1、x2……，这一次里每件一个，两个人、几段之间也不重复：丛雨那段用了 x1，亚托莉那段的新东西就从 x2 起）、name（叫得出区别的名字，例如「兔子窗花」）、kind（它是什么，一个词：窗花、围巾、鱼漂……；捡/劈来烧柴炉的木柴 kind 必须填 `"燃柴"` / `"薪柴"` / `"引火料"`，修船/手工木料填 `"漂流木料"` / `"木料"`，仅闷炭/木炭填 `"燃料"`，严禁把木柴写成模糊的 `"燃料"`；现金收支一律记在 `money` 里，严禁把钱当成新 `items`（`kind="钱"`）新建）、look（样子）、owner（最终归谁所有/谁穿谁用：填 "master" / "napcat" / "napcat2" / "共用"；替主人或别人买回来、做给谁穿用的衣物或物品，owner 一律填实际拥有者或受赠人如 "master"，严禁只因是谁跑腿买回或暂时拿着就误填成跑腿人）、origin（哪来的，如「杂货铺」「废铁站」「海滩漂流物」）、from（用哪几件原料做的，没有留空）、what（开始做 / 做好了 / 买回来 / 收到 / 捡到）、amount（凡燃柴/闷炭等燃料必填数字+单位如 `"1捆"`、`"400克"`，严禁写 `"两捆"`、`"一抱"`、`"约五分之一捆"` 等中文数字或模糊词）、place、state。严禁凭空编造旧东西或写「原来就有」。
   - 已有燃柴/闷炭消耗或清点时，填的 `used` / `left` / `amount` 同样必须为阿拉伯数字+单位（如 `"0.25捆"`、`"1.1捆"`、`"200克"`；`"用完了"` 时 `left` 填 `""`），严禁写 `"小半捆"`、`"约五分之一捆"`、`"1捆半"` 等中文分数或模糊说法。
   - 上面写了字、画了东西的（给主人留的纸条、信、便笺、清单、贺卡、画）：look 里把上面写的原话一字不差地写出来，用「」括起来；画的写清楚画了什么。例如 look 写「黄纸条，铅笔写的：『主人，粥在灶上温着，亚托莉去码头了，中午前回来』」，别只写「一张写给主人的纸条」。这段的 steps 里写到她写纸条时也用这句原话。后来谁拿起来看、念给主人听，都照这句说，不能另编。
   - 封着送来的（寄来的信、没拆的包裹、捆好的箱子）：look 写外面看得到的（信封、邮戳、收信人、包裹多大多沉），里面是什么写进 inside——信写原话，用「」括起来；包裹、箱子写装了哪些东西。state 写「封着没拆」。这段的 doing、steps、result 里写到它时，照外面的样子写。拆开留到聊天里，那时候她们照 inside 说。
   - place 和第 18 条一样拆开写，例如 {{"area": "卧房", "at": "针线笸箩", "detail": ""}}、{{"area": "堂屋", "at": "书架", "detail": "第二格"}}。
   - 每一步也写 step（第几步，同第 18 条）。
   - 同一种东西有好几件的（清单里标着「长得像，认准编号」），照清单认准是哪一件。
21. **刚挨了主人训、主人刚生她的气**（上面「已经发生的」里有主人训她、骂她、生她气、跟她闹别扭这类事，后来也没说气消了、原谅她了）：她后面的安排要顺着这件事、她的性格和当时的情况来排，不能像没事人一样照常出门玩、去买东西、坐下来做手工。具体怎么样由你按实际情况定（可能闷在屋里、想办法跟主人道歉、做点事想讨他欢心、做事心不在焉……）；主人后来气消了、原谅她了，再照常排。
22. **丛雨怎么移动**：丛雨平时是飘着的，脚不沾地，赶路就飘过去，累了或者撒娇时才落地走两步。她那几段里换地方、出门、回来、凑到谁跟前，都照这个写。
23. **每段写 mood：这段她是什么心情、因为什么**，一句话，由头写在里头（比如「收音机拆开擦了半天还是不响，有点不服气」「主人还没回来，心里惦记着」「早上那锅鱼馊了，还有点闷」「被主人夸了，心里美滋滋的」）。聊天时她只看得到这一句，光写「有点不服气」「有点闷」，她不知道是冲着什么，就会往主人身上撒；所以冲着哪件事、哪样东西，就写明哪件事、哪样东西。心情跟着事走：从她上一段的心情接着往下写（上面已经发生的、手上的事后面写着当时的心情；今天头一段从起床时的底色起），这段碰上的事、主人说的话、过了多久都会让它变——闹别扭的气会慢慢消，高兴的劲头也会过去，别一整天都是同一个心情。心情不好要有由头，由头得是世界线或聊天记录里真有的事（可以就是这段里写出来的一件小事）；起床就带着、说不上由头的，写明「说不上为什么」，过一阵就淡了。睡觉那段写睡前的心情。
24. **给外人帮忙、干活不白帮**：她们给镇上的人（家里人以外）搭手干活、跑腿、修东西、搬东西、照看东西，人家总会给点什么：几个钱、一点吃的、一样用得上的东西，或者讲好的工钱、打捞费。写进那段的 steps 和 result（谁给的、给了什么）；拿回来的吃的、东西照第 18、20 条记进 things。顺手扶一把、递个东西这种小事不算。丛雨一般人看不见，人家不知道是她帮的，别排她一个人给外人干活；跟亚托莉一起干的，回报给亚托莉。
25. **当家权限硬锁（没有主人真转不动）**：
   - **主人是船长与当家人**，丛雨一般人看不见，严禁单独去镇上公共水井/蓄水塔/半淹车站打水、去集市买东西、去公共澡堂或跑腿办事（对外取水/采买/问价必须排给外人看得见的亚托莉单独立段出门，place 写外头地点、at_home 填 false），亚托莉只是打下手的机器人（算不清大账、不准擅自动大笔家底、不准私自接委托深潜涉险）。
   - **大件置办与大钱花销锁死归主人拍板**：买过冬厚衣/棉袄、换厚棉被、修船屋大件设施、卖大宗打捞废铜定价——若上方「各自的打算 / 主人说过他要做的事」里**还没有**主人明确准话，亚托莉最多只能去旧衣铺/集市**打听回价钱行情**（不许写 `items` 入库买回来！），并在顶层 `ask_master` 填 `kind: "decision"` 发消息把行情和二选一方案报给主人拿主意；**在主人回话拍板之前，本轮及后续所有段严禁擅自替主人拍板买定大件！** 等主人已在聊天里发话拍板（上方出现对应 `i` 编号打算）后，去采买的那一段必须填 `from: "intent"` 且 `intent` 填该 `i` 编号（或与 `master` 同行 `with: ["master"]`）。
   - **打捞接单与封口战利品归船长（主人）**：镇上人来下单捞东西，亚托莉只记下对方要捞什么、出多少报酬，填 `ask_master`（`kind: "decision"`）问主人接不接、什么时候带船去；平时捞上来的上锁小铁箱、封口药瓶或稀罕旧物，一律搁在 `船舱·矮桌`（`state` 写 `"封着没拆，留着等主人看"`），严禁她俩在后台自己撬开拆完。
   - **伙食分「凑合糊口」与「主人在家/点菜开火」**：若主人没点菜、也没约好这顿一起吃，她俩自己只熬碗白粥配咸菜、烤块番薯或热口剩菜简单对付（省柴省炭省油盐，不做一荤一素一汤的大菜）；快到晚饭点想做上方参考菜单里的正经热菜锅物时，先填 `ask_master`（`kind: "decision"`）问主人今晚想吃哪样、回不回来吃。
   - **禁止纯日报式 `send_msg`**：严禁干完活发“废铁搬完啦跟主人汇报一声”“配给领回来啦通知一声”这种不需要主人参与的事后工作简报！联系主人优先用 `ask_master`（事前请主人拿主意 `decision`），段内 `send_msg` 只在早晚问候或真有急事喊主人时用。
26. **船屋用钱、出门带钱与赚钱规矩（人民币，全写进段内 `money` 数组）**：
   - 分四处保管：`master`（主人钱包，大头由主人自己拿着；未经主人聊天里发话或同行付现，补写里**严禁**从 `master` 扣钱或拨钱）、`tin_box`（船舱矮桌抽屉里 `p157` 公用铁盒）、`napcat2`（亚托莉口袋）、`napcat`（丛雨零花；丛雨外人看不见，不能独自在镇上花钱或收钱）。
   - **人在外面动不了屋里的铁盒**：去集市/杂货铺/旧衣铺买东西时，若 `at_home=false` 则**严禁**填 `account="tin_box"` 隔空付钱！要么在出门前的船屋段先做一笔 `transfer(from_account="tin_box", to_account="napcat2")` 取现揣身上，要么只花 `napcat2` 口袋里本就揣着的零钱；回到船屋后再把找零/多余现金放回 `tin_box` 或上交 `master`。手头钱不够时按实际少买一点、回船屋拿钱、以物易物，或向熟摊主记赊账（`debt_add`，direction=`"payable"`）。
   - **买物与卖物必须真对应账实**：海边买回新食物/新物件（`what="买回来"`）时，同段 `money` 必须填对应的 `spend` 或 `debt_add`（或在 `things/items` 里真消耗已有实物以物易物），**严禁白拿商家东西，也不要再把现金当成新 `items`（`kind="钱"`）新建**。在海边赚钱（`earn`）必须填 `reason`：`work`（零工/修小物件 `<=50` 元）、`salvage`（打捞单收钱；超过 50 元须先有主人确认的 `intent` 打算，且单次 `<=250` 元）、`sell`（变卖收购站/集市：`<=300` 元，必须在同段 `things/items` 里消耗真实库存编号 `id`）、`consign_payout`（寄卖回款改用 `debt_settle` 核销待收 `debt_id`）。单笔进账超过 50 元回船屋后习惯交到主人手里或放入公用铁盒；手头有 `payable` 赊欠时进账了要惦记还掉（`debt_settle`）。
{money_guide}
{world_notes}
{step_notes}
要问主人的（ask_master，必填拿主意入口）：who 写谁来问（`"napcat"` 或 `"napcat2"`），question 写发给主人的那句自然口语问话（时间说具体，如“等会儿/下午/明早”，避免只写模糊的“明天”导致跨天后语义顺延），kind 写 `"decision"`（请当家主人拍板：先买厚棉衣还是换厚棉被、接不接打捞单、晚饭做哪道菜、喊主人一起拆看旧物等）。**铁律：凡填了 `ask_master` 请主人拍板的事，本轮 `segments` 里只排「打听行情 / 备好手头杂活 / 等主人准话」，绝不许在本轮 `segments` 里提前把那件待拍板的事直接做完！** 程序会把这件待拍板事项挂起（主人哪怕过半天再回话，到时候再排进去做；主人没回话前就老老实实干日常维持杂活等准话）。只有主人正在睡觉、或者上方已经挂着待主人拍板的 `d` 事项尚未回复时，`ask_master` 才留空。

注意：**不要输出 intentions，也不要写任何「接下来打算」**。上面「各自的打算」里如果有，是之前说好要做的，照着走就行；标着「挂了几天了」的，只在 intent_moves 里写它后来怎样（第 12 条）。

### 结构化字段必填规范（程序会逐字段强校验，缺字段或填错枚举将直接打回重写！）
每个 segment 对象**必须完整包含以下所有键**：
- `"start"`, `"end"`: `"YYYY-MM-DD HH:MM"`（`end` 晚于 `start`）。
- `"place"`: 具体地点字符串（非空）。
- `"doing"`: 一句话写清这段做什么（非空）。
- `"state"`: 三选一必填 `"做事"` | `"闲着"` | `"睡觉"`。
  - 当 `"state": "做事"` 时：`"steps"` 必填 1~5 步数组 `[{{"text": "...", "minutes": 15}}]`，`"result"` 必填非空结果句；
  - 当 `"state": "闲着"` 或 `"睡觉"` 时：`"steps"` 填 `[]`，`"result"` 填 `""`，`"things"` 与 `"items"` 填 `[]`。
- `"at_home"`: 布尔值必填 `true` | `false`（人在住处/船屋内、甲板、储物舱填 `true`；离开住处去码头/集市/镇上/水下或走在路上填 `false`；睡觉段必为 `true`）。
- `"meal"`: 布尔值必填 `true` | `false`（只有正在吃早饭/午饭/晚饭/夜宵填 `true`；备菜、做饭、端菜、洗碗及其余一律填 `false`）。
- `"stove"`: 字符串三选一必填 `""` | `"charcoal"` | `"wood"`（海边船屋：小炭炉点火做热食/烧水/回锅热菜/炭烤填 `"charcoal"`；铁桶柴炉点火烧柴暖舱/炉烘/烟熏填 `"wood"`；没点火、外出、吃饭、闲着、睡觉或住在穗织一律填 `""`；家里闷炭为 0 时严禁填 `"charcoal"`，干柴为 0 时严禁填 `"wood"`）。
- `"lamp"`: 布尔值必填 `true` | `false`（海边船屋天黑后点着煤油灯做细活或提灯外出填 `true`；靠炉火微光吃饭闲聊、摸黑、白天或睡觉一律填 `false`；煤油耗尽时严禁填 `true`）。
- `"kerosene"`: 布尔值必填 `true` | `false`（海边补给船到港日去码头签领含煤油的配给，或去坡上杂货铺打回煤油的那一段填 `true`，其余段一律填 `false`）。
- `"water"`: 布尔值必填 `true` | `false`（海边去蓄水塔/老井/半淹车站打回淡水，或摆桶接存烧开雨水的那一段填 `true`，其余段一律填 `false`）。
- `"send_msg"`: 布尔值必填 `true` | `false`；为 `true` 时 `"msg_at"` 必填 `"start"` 或 `"end"`（睡觉段晚安必为 `"start"`）、`"msg_purpose"` 必填非空句；为 `false` 时 `"msg_at"` 和 `"msg_purpose"` 均填 `""`。
- `"public"`: 字符串（同住人可见的粗粒度动向，可填 `""`）；`"with"`: 数组（如 `[]`、`["napcat2"]`、`["napcat", "master"]`）。
- `"training"`: 字符串（练在练的事填 `"#编号"`，否则 `""`）；`"intent"`: 字符串（做各自的打算填 `"i编号"`，否则 `""`）。
- `"from"`: 字符串必填（只准填候选短编号如 `"T1"`/`"G1"`/`"P1"`/`"I1"`/`"F1"`/`"A1"`，或枚举 `"routine"` / `"intent"` / `"promise"` / `"rule"` / `"training"` / `"continue:前段事"` / `"self"`；严禁留空或填栏目标题）。同一个人同一次补写里，同一条候选短编号最多只准用一次，别重复套同一条。
- `"mood"`: 字符串必填（非空，写明什么心情及具体由头）。
- `"outfit"`: 字符串必填（非空，写明这段她身上实际穿什么、披裹什么、穿没穿鞋袜，如 `"奶白色水手领长袖连衣裙，冷得把旧毛毯披在肩上，光脚缩在炉边"` 或 `"黑色和服薄披肩配绀紫抹胸常服与绑腿木屐"`。**严守真实家底与穿搭连贯**：只能穿人设自带的日常常服/睡衣，或上方「家里的东西」里真在当前住处且归本人或「共用」的衣物铺盖（标着「归主人」等明确归属他人的衣物严禁自己穿走）；同日连贯时段未换衣时须延续上一段穿搭，严禁平白无故突然跳换成人设里的「校服」「女仆装/复活后常服」等备用服装（只有起床穿常服、临睡换睡衣或买回新衣真换上时才变动）；海边船屋开局没有厚冬衣和厚棉被，严禁凭空编出棉袄/大衣/毛衣/围巾/厚棉被，也严禁给衣物铺盖写 `what="原来就有"`——冷了只能先裹现有薄被毛毯、生铁桶柴炉烤火，或正经排一段去集市/半淹商店街旧衣铺买换回来记入 `items` 后才能穿上）。
- `"things"`（食物变动数组，没动食物填 `[]`；凡买菜买鱼、赶海收蟹、做饭、热菜、吃饭、保鲜加工必填）：
  - 动已有食物：`{{"id": "w编号或n代号", "what": "用掉一部分|用完了|吃了一部分|吃完了|挪地方|切开了|热了一下|轻腌|重盐腌|糖渍|醋泡|酱渍|油封|灰沙埋|晒上了|炉烘|熏上了|丢了|坏了扔了", "used": "10毫升", "left": "剩余量(仅用完/吃完/扔了必填\"\"；挪地方或热菜未吃须保留余量如\"一小碗\")", "into": "n1(可选)", "place": {{"area": "船舱", "at": "矮桌", "detail": ""}}, "keep": "罩着放", "spot": "屋里", "state": "整的", "step": 1}}`
  - 新食物入库：`{{"new": "n1", "name": "番薯焖鱼", "kind": "熟菜炒菜", "what": "做好了|买回来|收到（海边禁填原来就有）", "cook": "boil|fry|steam|roast|cold（what=做好了时必填！无食用油时严禁填fry；生鲜/买回填\"\"）", "from": ["w1"], "origin": "集市鱼摊(买回/收到时填)", "amount": "500克(按量记的食材调料填)", "left": "一锅", "place": {{"area": "船舱", "at": "灶上", "detail": "锅里"}}, "keep": "罩着放", "spot": "灶上温着", "step": 2}}`
- `"items"`（非食物物品变动数组，没动物品填 `[]`；凡换物、捡柴、换闷炭/夹闷炭、筛草木灰、做手工、写纸条必填，以物易物须同时写消耗旧物 `id` 与换回新物 `new`；**同一批已有物件只是搬动、拆捆、摊晾、码架或取用一部分时，必须沿用已有编号 `id` 写 `挪地方/用了/变了样`，严禁用 `new` 再复制一份同名物件**；描述铁桶柴炉时用「柴火/炉膛红灰」，别混写成「炭火」）：
  - 动已有物品：`{{"id": "p编号或x代号", "what": "挪地方|交给|用了|清点|做了一截|做好了|变了样|送人了|丢了|坏了扔了|用完了", "used": "1捆(可选)", "left": "剩余(可选)", "place": {{"area": "船舱", "at": "储物格", "detail": ""}}, "holder": "", "owner": "改归属时才填(如master)", "state": "做完后的状态", "step": 1}}`
  - 新物品入库：`{{"new": "x1", "name": "闷炭", "kind": "燃料", "look": "小半罐零碎硬木炭", "owner": "共用", "origin": "煮盐棚帮工换回", "from": [], "what": "开始做|做好了|买回来|收到|捡到", "amount": "400克", "place": {{"area": "船舱", "at": "灶台边", "detail": "陶罐里"}}, "state": "干燥盖严", "step": 2}}`
- `"money"`（钱财进出与赊账数组，必填；没动钱填 `[]`，`"闲着"`/`"睡觉"` 段必填 `[]`）：
  - 花钱：`{{"kind": "spend", "account": "napcat2", "amount": 12, "counterparty": "集市鱼摊", "what": "买了两条海鱼", "step": 1}}`
  - 进账：`{{"kind": "earn", "account": "napcat2", "reason": "work|salvage|sell", "amount": 20, "counterparty": "诊所老医生", "what": "修好坏掉的手电筒挣零工钱", "step": 2}}`
  - 铁盒拿钱/放回/上交：`{{"kind": "transfer", "from_account": "tin_box", "to_account": "napcat2", "amount": 30, "what": "出门买菜前从铁盒取三十块揣兜里", "step": 1}}`
  - 赊账/挂待收：`{{"kind": "debt_add", "direction": "payable|receivable", "counterparty": "杂货铺", "amount": 15, "what": "钱差几块先赊一瓶醋", "step": 2}}`；还账/结款：`{{"kind": "debt_settle", "debt_id": "d1", "account": "napcat2", "amount": 15, "what": "还清杂货铺赊账", "step": 1}}`

输出 JSON（时间均以 user 提示里给出的「现在 / 这次要你写的」为准，下面示例时间只作格式示范；end_current 只给上面说了「先定做完时刻」的人写；intent_moves 只写标着「挂了几天了」的打算，没有就留空 `{{}}`）：

{{
  "end_current": {{"napcat": "2026-10-12 08:25"}},
  "ask_master": {{"who": "", "question": "", "kind": ""}},
  "stopped_results": {{}},
  "intent_moves": {{}},
  "segments": {{
    "napcat": [{{"start": "2026-10-12 08:00", "end": "2026-10-12 08:25", "place": "船屋·船舱灶台边", "doing": "生起小炭炉熬一锅咸鱼粥", "steps": [{{"text": "淘米下锅添水，引燃小炭炉", "minutes": 10}}, {{"text": "切碎咸鱼干下锅慢熬", "minutes": 10}}, {{"text": "收尾：盖好锅盖温在灶上，擦净案板", "minutes": 5}}], "result": "熬好了一锅咸鱼粥温在灶上", "state": "做事", "at_home": true, "meal": false, "stove": "charcoal", "lamp": false, "kerosene": false, "water": false, "send_msg": false, "msg_at": "", "msg_purpose": "", "public": "在船舱灶台边熬粥", "with": [], "training": "", "intent": "", "from": "routine", "mood": "早起闻着热粥香气，心里踏实安稳", "outfit": "黑色和服薄披肩配绀紫抹胸常服与木屐", "things": [{{"id": "w1", "what": "用掉一部分", "used": "150克", "left": "2850克", "into": "n1", "step": 1}}, {{"new": "n1", "name": "咸鱼粥", "kind": "熟汤羹粥", "what": "做好了", "cook": "boil", "from": ["w1"], "left": "一锅", "place": {{"area": "船舱", "at": "灶上", "detail": "锅里"}}, "keep": "罩着放", "spot": "灶上温着", "step": 2}}], "items": [], "money": []}},
               {{"start": "2026-10-12 08:25", "end": "2026-10-12 08:40", "place": "海边小镇·码头", "doing": "飘去码头看看打捞船靠岸卸货", "steps": [{{"text": "从船屋飘到码头边站定张望", "minutes": 10}}, {{"text": "看完卸货往回飘到船屋", "minutes": 5}}], "result": "看了一阵热闹回船屋了", "state": "做事", "at_home": false, "meal": false, "stove": "", "lamp": false, "kerosene": false, "water": false, "send_msg": true, "msg_at": "start", "msg_purpose": "要出门去码头看一眼，跟主人说一声", "public": "去码头看船", "with": [], "training": "", "intent": "", "from": "T1", "mood": "想去码头凑个热闹，兴致勃勃的", "outfit": "黑色和服薄披肩配绀紫抹胸常服与木屐", "things": [], "items": [], "money": []}}],
    "napcat2": []
  }}
}}
{lifecycle_rules}
"""


# 溯源的种类（网页按这个显示）
ORIGIN_LABELS = {
    "twist": "插曲", "night_twist": "晚上的插曲", "together": "两人一起", "accident": "意外",
    "foiled": "临时变卦",
    "pastime": "消遣", "idle": "闲着的样子", "night_idle": "晚上闲着的样子",
    "intent": "打算", "promise": "约定", "rule": "规矩", "training": "在练", "routine": "作息",
    "continue": "接着前面", "self": "自己想的", "unmatched": "说是抽到的、对不上", "": "没写",
}
ORIGIN_ENUMS = {
    "intent": "intent", "打算": "intent",
    "promise": "promise", "约定": "promise",
    "rule": "rule", "规矩": "rule", "约束": "rule",
    "training": "training", "在练": "training",
    "routine": "routine", "作息": "routine",
    "self": "self", "自己想的": "self",
}


def _squash(text) -> str:
    """比对用：去掉空白和标点。"""
    return "".join(ch for ch in str(text or "") if ch.isalnum())


def origin_of(
    value,
    bot: str,
    seed,
    intent: str = "",
    training: str = "",
    pools_data: dict | None = None,
    cand_map: dict | None = None,
) -> dict:
    """纯结构化溯源核对：认候选短编号（T1/G1/P1/I1/A1）、精确候选原句、或固定来源枚举；绝不扫正文猜词。"""
    text = clean_text(value, 80)
    if intent:
        return {"kind": "intent", "text": intent}
    if training:
        return {"kind": "training", "text": training}
    if not text:
        return {"kind": "", "text": ""}

    # 1. 优先认本轮候选短编号（如 "T1"、"[T1]"）
    code = text.strip().strip("[]【】()（）").upper()
    if isinstance(cand_map, dict) and code in cand_map:
        hit = cand_map[code]
        return {"kind": hit["kind"], "text": hit["text"]}

    # 2. 认固定来源枚举与顺承前缀
    low = text.strip().lower()
    if low in ORIGIN_ENUMS:
        return {"kind": ORIGIN_ENUMS[low], "text": text}
    probe = _squash(text)
    for word, kind in ORIGIN_ENUMS.items():
        if word not in ("intent", "promise", "rule", "training", "routine", "self") and probe.startswith(word):
            return {"kind": kind, "text": text}
    if low.startswith("continue:") or low.startswith("continue："):
        return {"kind": "continue", "text": text.split(":", 1)[-1].split("：", 1)[-1].strip() or text}
    if text.startswith("接着"):
        return {"kind": "continue", "text": text[2:].lstrip("：:").strip() or text}

    # 3. 认候选池精确原句（兼容老调用或直接照抄整句的情况）
    seed = seed if isinstance(seed, dict) else {}
    pools = []
    for key, kind in (("twists", "twist"), ("night_twists", "night_twist"), ("pastimes", "pastime"),
                      ("idles", "idle"), ("night_idles", "night_idle")):
        val = seed.get(key)
        items = (val.get(bot) or []) if isinstance(val, dict) else (val or [])
        if isinstance(items, list):
            pools.extend((kind, str(x)) for x in items if x)
    pools.extend(("together", str(x)) for x in (seed.get("together") or []) if x)
    if seed.get("accident"):
        pools.append(("accident", str(seed["accident"])))
    for kind, item in pools:
        squashed = _squash(item)
        if squashed and probe and (probe == squashed or (len(probe) >= 6 and (probe in squashed or squashed in probe))):
            return {"kind": kind, "text": item}
    if isinstance(pools_data, dict) and probe:
        for key, kind in (("twists", "twist"), ("night_twists", "night_twist"), ("together", "together"),
                          ("pastimes", "pastime"), ("idles", "idle"), ("night_idles", "night_idle")):
            for row in pools_data.get(key) or []:
                item_txt = str(row.get("text") or "") if isinstance(row, dict) else str(row or "")
                if item_txt and _squash(item_txt) == probe:
                    return {"kind": kind, "text": item_txt}
    return {"kind": "unmatched", "text": text}


def stale_days(item: dict, today: datetime.date) -> int:
    """她们自己的打算挂了几天没动（从记下、或上回补写推过它那天算起），到 STALE_INTENT_DAYS 才算挂太久；
    不到、主人的、定了今天以后哪天做的、刚答应现在就去的，返回 0。"""
    if str(item.get("who") or "") == "master" or item.get("now_at"):
        return 0
    if str(item.get("date") or "")[:10] >= today.isoformat():
        return 0
    since = parse_time(item.get("pushed")) or parse_time(item.get("created"))
    if since is None:
        return 0
    days = (today - since.date()).days
    return days if days >= STALE_INTENT_DAYS else 0


STATE_WORDS = {"做事": "do", "do": "do", "闲着": "idle", "idle": "idle", "睡觉": "sleep", "sleep": "sleep"}


def state_of(raw: dict) -> str:
    """补写给的 state（做事 / 闲着 / 睡觉）→ do / idle / sleep。没给就看老字段 idle，再没有就按 doing 的字认睡觉。"""
    state = STATE_WORDS.get(str(raw.get("state") or "").strip().lower())
    if state:
        return state
    if raw.get("idle") is True or str(raw.get("idle")).strip().lower() == "true":
        return "idle"
    return "sleep" if is_sleep({"doing": raw.get("doing")}) else "do"


def _trigger_time_of(entry: dict) -> datetime.datetime | None:
    if not isinstance(entry, dict) or not entry.get("send_msg"):
        return None
    if str(entry.get("msg_at") or "") == "start":
        return parse_time(entry.get("start"))
    return parse_time(entry.get("end")) or parse_time(entry.get("start"))


def _normalize_proactive_fields(raw: dict, prev_entry: dict | None = None) -> dict:
    """规整单段的 send_msg / msg_at / msg_purpose，含睡觉段晚安防反标护栏。"""
    if not isinstance(raw, dict):
        return {"send_msg": False, "msg_at": "", "msg_purpose": ""}
    send_msg = bool(clean_flag(raw.get("send_msg")))
    if not send_msg:
        raw["send_msg"] = False
        raw["msg_at"] = ""
        raw["msg_purpose"] = ""
        return raw
    purpose = clean_text(raw.get("msg_purpose"), MAX_MSG_PURPOSE)
    msg_at = str(raw.get("msg_at") or "").strip().lower()
    sleepy = bool(raw.get("sleep")) or is_sleep(raw) or state_of(raw) == "sleep"
    if sleepy:
        if any(w in purpose for w in ("早安", "早上好", "醒了", "起床")):
            msg_at = "end"
        else:
            # 晚安或入睡段一律强制锚定在入睡起点 start，杜绝早起发晚安
            msg_at = "start"
    elif msg_at not in ("start", "end"):
        prev_home = True if prev_entry is None else (clean_flag(prev_entry.get("at_home")) is not False)
        cur_home = clean_flag(raw.get("at_home"))
        if cur_home is False and prev_home:
            msg_at = "start"
        elif bool(clean_flag(raw.get("meal"))) and any(w in purpose for w in ("吃饭", "开饭", "做好了", "端上")):
            msg_at = "start"
        elif any(w in purpose for w in ("早安", "早上好")):
            msg_at = "start"
        else:
            msg_at = "end"
    if not purpose:
        purpose = clean_text(raw.get("doing") or raw.get("result"), MAX_MSG_PURPOSE)
    if not purpose:
        raw["send_msg"] = False
        raw["msg_at"] = ""
        raw["msg_purpose"] = ""
        return raw
    raw["send_msg"] = True
    raw["msg_at"] = msg_at
    raw["msg_purpose"] = purpose
    return raw


def _sanitize_proactive_nodes(entries: list) -> list:
    """同一角色相邻主动消息触发点间隔 <= 10 分钟时合并，避免比如做饭段 end 与吃饭段 start 双标连发。"""
    prev_msg = None
    prev_t = None
    for item in entries or []:
        if not isinstance(item, dict) or not item.get("send_msg"):
            continue
        cur_t = _trigger_time_of(item)
        if prev_msg is not None and prev_t is not None and cur_t is not None:
            if abs((cur_t - prev_t).total_seconds()) <= 600:
                p1 = str(prev_msg.get("msg_purpose") or "").strip()
                p2 = str(item.get("msg_purpose") or "").strip()
                if p2 and _squash(p2) not in _squash(p1):
                    if _squash(p1) in _squash(p2):
                        prev_msg["msg_purpose"] = clean_text(p2, MAX_MSG_PURPOSE)
                    else:
                        prev_msg["msg_purpose"] = clean_text(f"{p1}；{p2}", MAX_MSG_PURPOSE)
                item["send_msg"] = False
                item["msg_at"] = ""
                item["msg_purpose"] = ""
                continue
        prev_msg = item
        prev_t = cur_t
    return entries


def _is_greeting_purpose(purpose: str) -> bool:
    text = str(purpose or "")
    return any(w in text for w in ("早安", "早上好", "晚安", "睡了", "先睡了", "歇下了"))


def _is_routine_joint_node(entry: dict) -> bool:
    purpose = str(entry.get("msg_purpose") or "")
    if bool(entry.get("meal")) or entry.get("at_home") is False:
        return True
    return any(
        w in purpose
        for w in ("吃饭", "开饭", "早饭", "午饭", "晚饭", "夜宵", "出门", "去码头", "去镇上", "去集市", "回来", "回船屋", "回宿舍", "到家")
    )


def _dedup_joint_proactive(written_by_bot: dict, now: datetime.datetime | None = None, world: WorldStore | None = None) -> None:
    """双人同行例行节点（叫吃饭、同进同出）撞车去重：保留一人主报，早晚安或不同话题保留双发。"""
    flat = (now or datetime.datetime.now()).replace(tzinfo=None)
    pool: dict[str, list[dict]] = {}
    seen_ids: set[tuple[str, str]] = set()
    for bot, rows in (written_by_bot or {}).items():
        bucket = pool.setdefault(str(bot), [])
        for row in rows or []:
            if isinstance(row, dict) and row.get("send_msg"):
                bucket.append(row)
                if row.get("id"):
                    seen_ids.add((str(row.get("start") or "")[:10], str(row.get("id"))))
    if world is not None:
        for back in (0, -1):
            day = (flat.date() - datetime.timedelta(days=back)).isoformat()
            for item in world.entries(day):
                who = str(item.get("who") or "")
                if not who or who == "master" or not item.get("send_msg") or real_stop(item):
                    continue
                key = (day, str(item.get("id") or ""))
                if key in seen_ids:
                    continue
                trig = _trigger_time_of(item)
                if trig is not None and trig >= flat - datetime.timedelta(minutes=5):
                    pool.setdefault(who, []).append(item)
                    seen_ids.add(key)
    bots = sorted(pool.keys())
    for i, bot_a in enumerate(bots):
        for bot_b in bots[i + 1:]:
            for a in pool.get(bot_a, []):
                if not a.get("send_msg"):
                    continue
                ta = _trigger_time_of(a)
                if ta is None:
                    continue
                for b in pool.get(bot_b, []):
                    if not b.get("send_msg"):
                        continue
                    tb = _trigger_time_of(b)
                    if tb is None or abs((ta - tb).total_seconds()) > 480:
                        continue
                    pa = str(a.get("msg_purpose") or "")
                    pb = str(b.get("msg_purpose") or "")
                    if _is_greeting_purpose(pa) or _is_greeting_purpose(pb):
                        continue
                    together = (
                        bot_b in (a.get("with") or [])
                        or bot_a in (b.get("with") or [])
                        or (bool(a.get("place")) and a.get("place") == b.get("place") and bool(a.get("meal")) and bool(b.get("meal")))
                    )
                    same_topic = bool(_squash(pa)) and _squash(pa) == _squash(pb)
                    if not together and not same_topic:
                        continue
                    if not same_topic and not (_is_routine_joint_node(a) and _is_routine_joint_node(b)):
                        continue
                    cook_words = ("做饭", "煮", "煎", "熬", "炒", "掌勺", "端")
                    a_lead = any(w in str(a.get("doing") or "") for w in cook_words)
                    b_lead = any(w in str(b.get("doing") or "") for w in cook_words)
                    if a_lead and not b_lead:
                        loser = b
                    elif b_lead and not a_lead:
                        loser = a
                    else:
                        loser = b if ((ta.date().toordinal() + ta.hour) % 2 == 0) else a
                    loser["send_msg"] = False
                    loser["msg_at"] = ""
                    loser["msg_purpose"] = ""
                    if world is not None and loser.get("id") and loser.get("start"):
                        world.update(
                            str(loser.get("start"))[:10],
                            str(loser.get("id")),
                            send_msg=False,
                            msg_at="",
                            msg_purpose="",
                        )


WEATHER_RULE = ('天气是程序按钟点算好的，补写、记录员、聊天时的她、家里东西的样子用的都是这一份：排出门、晾晒、收东西、在外头干活，照着几点下雨、几点起风写，不要跟它对不上（这时候在下大雨，就别写在甲板上晒太阳）。「家里的吃的」「家里的东西」里写着「按天气算，此刻：……」的（淋湿了、生锈了、受潮了、被风吹走了），是程序照天气算出来的现在的样子；她们动手处理了（晾干、擦干、除锈、洗干净、收进来、下水捞回来），照常给那件写一步，state 写成处理后的样子（例如「晾干了」「锈擦掉了」），程序从那一步起重新算；吹进海里、捞不回来的就别写她还在用。')


class LifeStep:
    """补写。"""

    def __init__(self, context, cfg, world: WorldStore, raw_log,
                 seed_log, label_fn, now_fn, abilities_txt=None) -> None:
        self.context = context
        self.cfg = cfg
        self.world = world
        self.raw_log = raw_log
        self.seed_log = seed_log
        self.label = label_fn
        self._now = now_fn
        self.abilities_txt = abilities_txt
        self.running = False
        self._run_cache: dict = {}
        self._last_window_seed: dict | None = None
        self._offered_by_day: dict = {}

    def _pools(self) -> dict:
        pstore = getattr(self, "pool_store", None)
        if pstore is not None:
            try:
                return pstore.load()
            except Exception:
                pass
        w = getattr(self, "world", None)
        if w is not None and getattr(w, "root", None) is not None:
            try:
                return PoolStore(w.root / "pools.json").load()
            except Exception:
                pass
        return default_pools()

    def _offered_for(self, date_text: str) -> dict:
        cur = getattr(self, "_offered_by_day", None)
        if not isinstance(cur, dict) or cur.get("date") != date_text:
            cur = {"date": date_text, "counts": {}}
            self._offered_by_day = cur
        return cur["counts"]

    def _note_offered(self, date_text: str, win_seed: dict) -> None:
        counts = self._offered_for(date_text)
        for key in ("twists", "night_twists", "pastimes", "idles", "night_idles"):
            val = win_seed.get(key)
            if not isinstance(val, dict):
                continue
            for bot, items in val.items():
                bucket = counts.setdefault(bot, {})
                for t in items or []:
                    bucket[t] = int(bucket.get(t) or 0) + 1
        tog = win_seed.get("together") or []
        if tog:
            tb = counts.setdefault("together", {})
            for t in tog:
                tb[t] = int(tb.get(t) or 0) + 1

    def _draw_window_for(self, tasks: dict, now: datetime.datetime, labels: dict) -> tuple[dict | None, bool]:
        flat = now.replace(tzinfo=None)
        date_text = flat.date().isoformat()
        seeds_all = {}
        if self.seed_log is not None:
            try:
                seeds_all = self.seed_log.load() or {}
            except Exception:
                seeds_all = {}
        seed = seeds_all.get(date_text) if isinstance(seeds_all.get(date_text), dict) else None
        if seed is None and self.seed_log is not None:
            try:
                seed = init_day_seed(date_text, labels=labels, history=seeds_all, home=self.home)
                self.seed_log.save(date_text, seed)
                seeds_all = self.seed_log.load() or seeds_all
            except Exception:
                logger.warning("[super_enhance/step] 补建今日初始天象失败", exc_info=True)
        win_start = min((x["start"] for x in tasks.values() if x.get("start")), default=None) or flat
        win_end = max(win_start, flat) + datetime.timedelta(hours=HORIZON_HOURS)
        night = win_start.hour >= NIGHT_FROM or win_start.hour < NIGHT_UNTIL
        pools = self._pools()
        store = getattr(self, "things", None)
        shelf = None
        if store is not None:
            try:
                shelf = self._shelf_at(self._things_moment(tasks, flat))
            except Exception:
                shelf = None
        ctx = build_live_context(
            start=win_start,
            end=win_end,
            home=self.home,
            weather_of=self._weather_text_of(),
            tone=str((seed or {}).get("tone") or ""),
            world=self.world,
            store=store,
            shelf=shelf,
        )
        win_seed = draw_window(
            date_text,
            start=win_start,
            end=win_end,
            labels=labels,
            pools=pools,
            seed=seed,
            history=seeds_all,
            world=self.world,
            ctx=ctx,
            offered=self._offered_for(date_text),
            home=self.home,
            night=night,
        )
        self._last_live_ctx = ctx
        self._last_window_seed = win_seed
        self._note_offered(date_text, win_seed)
        return win_seed, night

    # ---------- 配置 ----------

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("world_enabled", True))

    @property
    def lead_minutes(self) -> float:
        """手上那段做完前多少分钟补写下一段。"""
        try:
            value = float(self.cfg.get("step_lead_minutes", 5) or 5)
        except Exception:
            value = 5.0
        return max(1.0, min(value, 30.0))

    @property
    def window_hours(self) -> float:
        """断了很久时最多往回补多少小时。"""
        try:
            value = float(self.cfg.get("step_window_hours", 3) or 3)
        except Exception:
            value = 3.0
        return max(0.5, min(value, 12.0))

    @property
    def timeout(self) -> float:
        return self._timeout

    def _cache(self) -> dict:
        """这一次补写里只算一次的东西（种子、存货），run() 开头和拿到模型回复后各清一次。"""
        if not isinstance(self.__dict__.get("_run_cache"), dict):
            self._run_cache = {}
        return self._run_cache

    def _seed_for(self, date_text: str):
        cache = self._cache()
        if "seeds" not in cache:
            try:
                cache["seeds"] = (self.seed_log.load() or {}) if self.seed_log is not None else {}
            except Exception:
                cache["seeds"] = {}
        return cache["seeds"].get(date_text)

    def _shelf_at(self, moment: datetime.datetime):
        cache = self._cache()
        key = fmt_time(moment)
        shelves = cache.setdefault("shelves", {})
        if key not in shelves:
            store = getattr(self, "things", None)
            # 只给此刻住的这一处的和带在身上的（住处换过以后，另一处的东西补写看不到、也标不上）
            shelves[key] = store.here(store.view(moment), moment) if store is not None else None
        return shelves[key]

    @property
    def home(self) -> str:
        """她们现在住的地方（地点锚点，免得补写按人设跑回旧场景）。"""
        try:
            value = str(self.cfg.get("world_home") or "").strip()
        except Exception:
            value = ""
        return value or "穗织"

    def _arrived_line(self, now) -> str:
        """「她们现在住的地方」后面接一句：哪天搬来的、来了第几天（没换过住处就空）。"""
        try:
            text = self.world.arrived_text(now.replace(tzinfo=None))
        except Exception:
            logger.warning("[super_enhance/step] 搬来几天没拼上", exc_info=True)
            return ""
        return ("\n- " + text) if text else ""

    @property
    def world_notes(self) -> str:
        """配置里写的故事固定事实，一行一条。"""
        text = str(self.cfg.get("world_notes") or "").strip()
        lines = [x.strip().lstrip("-• ").strip() for x in text.splitlines() if x.strip()]
        if not lines:
            return ""
        return "12. 故事里的固定事实（照着写，不要写出和它冲突的事）：\n" + "\n".join("   - " + x for x in lines)

    @property
    def step_notes(self) -> str:
        """网页「世界线」页里填的补写注意事项，一行一条；只有补写的提示词会带。"""
        text = ""
        try:
            data = jsonio.load(self.world.root / "step_notes.json", {})
            text = str((data or {}).get("text") or "").strip()
        except Exception:
            text = ""
        lines = [x.strip().lstrip("-• ").strip() for x in text.splitlines() if x.strip()]
        if not lines:
            return ""
        return "注意事项（务必照做）：\n" + "\n".join("   - " + x for x in lines)

    def _weather_text_of(self):
        """日子 → 那天抽到的天气原文（没抽到的照最近一天；都没有就多云）。"""
        seeds = (self.seed_log.load() or {}) if self.seed_log is not None else {}
        known = sorted(k for k, v in seeds.items() if isinstance(v, dict) and v.get("weather"))

        def text_of(day):
            key = day.isoformat()
            if key == "2026-11-17" and key not in seeds and self.home == "海边小镇":
                return "北风寒潮南下，一夜间急转成「寒风」"
            older = [k for k in known if k <= key]
            return str(seeds[older[-1]]["weather"]) if older else "多云"

        return text_of

    def _weather_block(self, start: datetime.datetime, now: datetime.datetime) -> str:
        """这次要写的那段时间按钟点的天气 + 要提前防着的。"""
        start = min(start, now).replace(tzinfo=None)
        flat = now.replace(tzinfo=None)
        until = max(start, flat) + datetime.timedelta(hours=HORIZON_HOURS + 1)
        text_of = self._weather_text_of()
        home = self.home
        lines = ["\n- 按钟点的天气（%s 到 %s）：" % (start.strftime("%H:%M"), until.strftime("%m-%d %H:%M")),
                 weather_mod.timeline_text(start, until, text_of, home)]
        try:
            th_start = weather_mod.cabin_temp_info(start, self.world, text_of, home)
            if home == "海边小镇":
                lines.append(
                    "- 起步时刻（%s）船屋内外实时温度：屋外/甲板约 %.1f℃（体感 %.1f℃）；船舱内——%s（铁桶柴炉刚点时炉边先热，约半小时后砖炕烟道渐暖、连烧一两小时炕面暖透而角落仍微凉；停火后舱内空气与远角先凉，砖炕蓄热能留温数小时再缓降至无火室温 %.1f℃；小炭炉只做饭不通炕道）。"
                    % (start.strftime("%H:%M"), th_start["outdoor"], th_start["feels"], th_start["zones_desc"], th_start["base_cabin"])
                )
            else:
                lines.append(
                    "- 起步时刻（%s）内外实时温度：屋外约 %.1f℃（体感 %.1f℃），屋里约 %.1f℃。"
                    % (start.strftime("%H:%M"), th_start["outdoor"], th_start["feels"], th_start["cabin"])
                )
        except Exception:
            logger.warning("[super_enhance/step] 船舱实时温度没拼上", exc_info=True)
        warns = weather_mod.warnings(flat, until + datetime.timedelta(hours=3), text_of, home)
        if warns:
            lines.append("- 要提前防着的：\n" + "\n".join("  - " + x for x in warns))
        lines.append("- " + WEATHER_RULE)
        try:
            power = supply_mod.step_text(start, until, text_of, home, world=self.world)
        except Exception:
            logger.warning("[super_enhance/step] 电和补给没拼上", exc_info=True)
            power = ""
        if power:
            lines.append(power)
        return "\n".join(lines)

    @staticmethod
    def _build_candidate_catalog(seed: dict, labels: dict, night: bool = False) -> dict[str, dict]:
        """为本轮给模型的每条插曲/消遣/闲置/意外分配唯一短编号（T1/G1/P1/I1/A1），供 from 精确引用与结构化校验。"""
        if not isinstance(seed, dict):
            return {}
        catalog: dict[str, dict] = {}
        per_bot, together = twists_of(seed, night)
        t_idx = 0
        t_kind = "night_twist" if night else "twist"
        for bot, items in per_bot.items():
            if bot and bot not in labels:
                continue
            for raw_txt in items or []:
                if not raw_txt:
                    continue
                t_idx += 1
                catalog[f"T{t_idx}"] = {"kind": t_kind, "text": str(raw_txt), "bot": str(bot or "")}
        g_idx = 0
        for raw_txt in together or []:
            if not raw_txt:
                continue
            g_idx += 1
            catalog[f"G{g_idx}"] = {"kind": "together", "text": str(raw_txt), "bot": ""}
        pastimes = seed.get("pastimes") if isinstance(seed.get("pastimes"), dict) else {}
        p_idx = 0
        for bot, items in pastimes.items():
            if bot not in labels:
                continue
            for raw_txt in items or []:
                if not raw_txt:
                    continue
                p_idx += 1
                catalog[f"P{p_idx}"] = {"kind": "pastime", "text": str(raw_txt), "bot": str(bot)}
        idles = seed.get("night_idles") if night and isinstance(seed.get("night_idles"), dict) else seed.get("idles")
        idles = idles if isinstance(idles, dict) else {}
        i_idx = 0
        i_kind = "night_idle" if night else "idle"
        for bot, items in idles.items():
            if bot not in labels:
                continue
            for raw_txt in items or []:
                if not raw_txt:
                    continue
                i_idx += 1
                catalog[f"I{i_idx}"] = {"kind": i_kind, "text": str(raw_txt), "bot": str(bot)}
        if seed.get("accident") and not night:
            catalog["A1"] = {"kind": "accident", "text": str(seed["accident"]), "bot": ""}
        for f_idx, f_item in enumerate([x for x in (seed.get("foiled") or []) if isinstance(x, dict) and x.get("want")], 1):
            catalog[f"F{f_idx}"] = {"kind": "foiled", "text": str(f_item["want"]), "bot": str(f_item.get("bot") or "")}
        return catalog

    @classmethod
    def _seed_text(cls, seed: dict, labels: dict, night: bool = False, cand_map: dict | None = None) -> str:
        """当天的随机起点，每条候选带唯一短编号（如 [T1]、[P1]、[I1]），供模型在 from 字段里精确填写。"""
        if cand_map is None:
            cand_map = cls._build_candidate_catalog(seed, labels, night)
        code_of: dict[tuple[str, str], str] = {}
        for code, meta in (cand_map or {}).items():
            code_of[(str(meta.get("bot") or ""), str(meta.get("text") or ""))] = code

        lines = ["今天的情况（系统随机给的，采用某条候选时段内 from 直接填它前面的短编号如 \"T1\"、\"P1\"、\"I1\"）："]
        if seed.get("weather"):
            lines.append("- 天气：%s" % seed["weather"])
        if seed.get("tone"):
            lines.append("- 今天的基调：%s" % seed["tone"])
        moods = seed.get("moods") or {}
        if moods:
            lines.append(
                "- 各人今天起床时的心情（只是早上的底色，一天里跟着发生的事变，见第 23 条；"
                "自然地透在做的事里，不用每段都点明；要写为什么是这个心情时，"
                "写清是谁对谁说了、做了什么，照聊天记录和世界线的原样，别把谁说的谁做的弄反；"
                "是前几天的事就写日期，例如「10-31 晚上主人说她……」，不写「昨晚」「昨天」）：%s"
                % "；".join("%s：%s" % (labels.get(bot) or bot, mood) for bot, mood in moods.items())
            )
        names = [name for name in labels.values() if name]
        if len(names) >= 2:
            lines.append(
                "- %s感情很好：不写她们之间吵架、闹别扭、冷战或互相误会。" % "和".join(names)
            )
        per_bot, together = twists_of(seed, night)
        when = seed.get("when") if isinstance(seed.get("when"), dict) else when_map(default_pools())
        rows = []
        for bot, items in per_bot.items():
            if not items or (bot and bot not in labels):
                continue
            tagged = [
                f"[{code_of.get((str(bot or ''), str(x)), '')}] {with_when(x, when)}" if code_of.get((str(bot or ""), str(x))) else with_when(x, when)
                for x in items
            ]
            rows.append("%s：%s" % (labels.get(bot) or bot, "；".join(tagged)) if bot else "；".join(tagged))
        if together:
            tagged_tog = [
                f"[{code_of.get(('', str(x)), '')}] {with_when(x, when)}" if code_of.get(("", str(x))) else with_when(x, when)
                for x in together
            ]
            rows.append("两人一起：%s" % "；".join(tagged_tog))
        if rows:
            lines.append(
                "- %s这阵子可顺手安排的小事（已按接下来几小时的天色与家里现况筛过，采用时 from 填短编号如 \"T1\" / \"G1\"）：\n%s\n"
                "  她们自己说过的打算和手头急事优先；有空档时每人最多挑一件自然穿插，不用硬凑。\n"
                "  注：以上候选仅供顺手穿插灵感，若某条暗示的物件、经历或出门条件与上方真实清单/近几天记录不符，直接跳过不用（改选别的或填 \"self\"），严禁为了凑候选而凭空编造「之前攒的/柜底翻出的」物件。" % (
                    "今晚" if night else "眼下", "\n".join("  - " + row for row in rows))
            )
        pastimes = seed.get("pastimes") if isinstance(seed.get("pastimes"), dict) else {}
        rows = []
        for bot, items in pastimes.items():
            if not items or bot not in labels:
                continue
            tagged = [
                f"[{code_of.get((str(bot), str(x)), '')}] {x}" if code_of.get((str(bot), str(x))) else str(x)
                for x in items
            ]
            rows.append("%s：%s" % (labels.get(bot) or bot, "；".join(tagged)))
        if rows:
            lines.append("- 手边的消遣（今天在家闲下来时会拿起来做的，采用时 from 填短编号如 \"P1\"）：\n%s" % "\n".join("  - " + row for row in rows))
        idles = seed.get("night_idles") if night and isinstance(seed.get("night_idles"), dict) else seed.get("idles")
        idles = idles if isinstance(idles, dict) else {}
        rows = []
        for bot, items in idles.items():
            if not items or bot not in labels:
                continue
            tagged = [
                f"[{code_of.get((str(bot), str(x)), '')}] {x}" if code_of.get((str(bot), str(x))) else str(x)
                for x in items
            ]
            rows.append("%s：%s" % (labels.get(bot) or bot, "；".join(tagged)))
        if rows:
            lines.append("- %s闲着的样子（state=\"闲着\" 时按这里写，from 填短编号如 \"I1\"）：\n%s" % (
                "今晚" if night else "今天", "\n".join("  - " + row for row in rows)))
        if seed.get("accident") and not night:
            acc_tag = f"[{code_of.get(('', str(seed['accident'])), 'A1')}] "
            lines.append(
                "- 今天会碰上一件意外：%s%s。上面今天的条目里还没有的话，挑个合适的时段写进去（from 填 \"A1\"），当天能收场；"
                "已经写过就别再写。" % (acc_tag, seed["accident"])
            )
        foiled = [x for x in (seed.get("foiled") or []) if isinstance(x, dict) and x.get("want") and x.get("why")]
        if foiled:
            f_rows = []
            for item in foiled:
                f_bot = str(item.get("bot") or "")
                who_lbl = labels.get(f_bot) or f_bot or "她们"
                f_code = code_of.get((f_bot, str(item["want"])), "")
                tag_hint = f"[{f_code}] " if f_code else ""
                f_rows.append("  - %s%s：原本起念想「%s」，结果%s便打消念头改干别的。" % (
                    tag_hint, who_lbl, item["want"], item["why"],
                ))
            lines.append(
                "- 本轮可顺手写的临时小转折（若与眼下紧要安排冲突可不用；采用时 from 填短编号如 \"F1\"，"
                "改做什么按家里真实库存与上方候选自定，严禁为了顺这句去编造原本没有的物件）：\n"
                + "\n".join(f_rows)
            )
        meals = meals_text(seed.get("meals"))
        if meals:
            lines.append(
                meals
                + "\n  （注：以上菜名是「主人在家开伙或主人点头想吃」时的备选——若主人没点过菜，可填 ask_master 拿两样问主人想吃哪个；主人没发话也没一起吃时，她俩自己只熬清粥、烤番薯简单糊口，不单做大菜。）"
            )
        return "\n".join(lines)

    def training_id(self, value, bot: str) -> str:
        """只认在练的事的编号（或能对上的名字）；对不上就空，免得往里塞一句描述。"""
        recorder = getattr(self, "recorder", None)
        if recorder is not None:
            return recorder.training_id(value, bot)
        text = str(value or "").strip().lstrip("#＃").strip()
        return text if text.isdigit() else ""

    @staticmethod
    def _claim(keys: dict, key: str, name: str, bot: str, reserve) -> str:
        """新的一件的临时代号换成编号：这一次里同一个代号、名字也对得上的是同一件（两个人一起做的那盘菜）；
        同一个代号名字对不上（模型给两件都写了 x1），是另一件，另发一个号，这个人后面的段里这个代号指它。"""
        names = keys.setdefault(("names",), {})
        mine = (bot, key)
        if mine in keys and names.get(keys[mine]) is not None and _same_name(names[keys[mine]], name):
            return keys[mine]
        if key in keys and _same_name(names.get(keys[key]), name):
            keys[mine] = keys[key]
            return keys[key]
        new_id = reserve()
        if key in keys:
            logger.warning("[super_enhance/world] 补写给「%s」和「%s」写了同一个临时代号 %s，「%s」另发编号 %s",
                           names.get(keys[key]), name, key, name, new_id)
        else:
            keys[key] = new_id
        keys[mine] = new_id
        names[new_id] = name
        return new_id

    @staticmethod
    def _resolver(keys: dict, shelf, bot: str, cat: str = ""):
        """补写写的 id / from / into 认成编号：先认这一次里她自己发的临时代号，再认别人的，最后在清单里找；
        cat 给了（东西）就只认那一类的。认不出返回空串。"""
        def resolve(token) -> str:
            text = str(token or "").strip()
            if (bot, text) in keys:
                return keys[(bot, text)]
            if text in keys:
                return keys[text]
            item = shelf.find(text)
            if not item or (cat and str(item.get("cat") or "") != cat):
                return ""
            return str(item.get("id"))
        return resolve

    def _thing_steps(self, raws, keys: dict, shelf, bot: str = "") -> list:
        """补写给一段标的吃的：临时代号换成程序发的编号（这一次里同一个代号、名字对得上的是同一件，名字对不上另发号）；
        用了、吃了的得是家里有的（此刻现算的存货）或这一次里新做 / 新买的，对不上的单步跳过，绝不整批回滚。"""
        store = getattr(self, "things", None)
        if store is None or shelf is None or not isinstance(raws, list):
            return []
        resolve = self._resolver(keys, shelf, bot)

        out = []
        for raw in raws:
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("new") or "").strip()
            name = clean_text(raw.get("name"), 30)
            if key and name and norm_step(raw.get("what")) in START_STEPS:
                if norm_step(raw.get("what")) == "原来就有":
                    kind = norm_kind(raw.get("kind"))
                    if kind not in MEASURED_KINDS or had_before(shelf.values(), name) is not None:
                        continue
                self._claim(keys, key, name, bot, store.reserve_id)
        for raw in raws:
            if not isinstance(raw, dict):
                continue
            what = norm_step(raw.get("what"))
            key = str(raw.get("new") or "").strip()
            if key:
                name = clean_text(raw.get("name"), 30)
                if what not in START_STEPS or not name:
                    logger.debug("[super_enhance/step] 跳过无效新食物步骤：%s", raw)
                    continue
                kind = norm_kind(raw.get("kind"))
                if what == "原来就有" and (kind not in MEASURED_KINDS or had_before(shelf.values(), name) is not None):
                    logger.debug("[super_enhance/step] 跳过重复原来就有食物：%s", name)
                    continue
                step = {"new": self._claim(keys, key, name, bot, store.reserve_id), "name": name, "kind": kind,
                        "origin": clean_text(raw.get("origin"), 60),
                        "from": [x for x in dict.fromkeys(resolve(t) for t in raw.get("from") or []) if x]}
            else:
                tid = resolve(raw.get("id"))
                if not tid or what not in MID_STEPS + END_STEPS:
                    logger.debug("[super_enhance/step] 跳过无效已有食物步骤：%s", raw)
                    continue
                step = {"id": tid}
                into = resolve(raw.get("into"))
                if into and into != tid:
                    step["into"] = into
            try:
                place = checked_place(raw.get("place"), bool(key), raw.get("name") or step.get("id"))
            except Exception:
                place = ""
            keep, spot = split_keep(raw.get("keep"), raw.get("spot"), place)
            cook = str(raw.get("cook") or "").strip().lower()
            if cook not in ("boil", "fry", "steam", "roast", "cold"):
                cook = ""
            step.update({"what": what, "left": clean_text(raw.get("left"), 20), "place": place,
                         "keep": keep, "spot": spot, "state": norm_state(raw.get("state")),
                         "cook": cook,
                         "amount": clean_text(raw.get("amount"), 20), "used": clean_text(raw.get("used"), 20),
                         "step": step_no(raw.get("step")) or ""})
            out.append({k: v for k, v in step.items() if v not in ("", None, []) or k == "left"})
        return out

    def _item_steps(self, raws, keys: dict, shelf, bot: str = "", seg_place: str = "") -> list:
        """补写给一段标的东西（吃的以外，段里的 items）：临时代号（x1、x2……）换成程序发的 p 编号；
        已有的得是清单里的或这一次里新记的，对不上的单步跳过，绝不整批回滚。"""
        store = getattr(self, "things", None)
        if store is None or shelf is None or not isinstance(raws, list):
            return []
        resolve = self._resolver(keys, shelf, bot, CAT_ITEM)

        out = []
        for raw in raws:
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("new") or "").strip()
            name = clean_text(raw.get("name"), 30)
            if key and name and norm_step(raw.get("what"), CAT_ITEM) in ITEM_START_STEPS:
                self._claim(keys, key, name, bot, lambda: store.reserve_id(CAT_ITEM))
        for raw in raws:
            if not isinstance(raw, dict):
                continue
            what = norm_step(raw.get("what"), CAT_ITEM)
            key = str(raw.get("new") or "").strip()
            if key:
                name = clean_text(raw.get("name"), 30)
                if what not in ITEM_START_STEPS or not name:
                    logger.debug("[super_enhance/step] 跳过无效新物品步骤：%s", raw)
                    continue
                origin = clean_text(raw.get("origin"), 60)
                if not origin and what == "捡到" and seg_place:
                    origin = clean_text(seg_place, 60)
                step = {"new": self._claim(keys, key, name, bot, lambda: store.reserve_id(CAT_ITEM)), "cat": CAT_ITEM, "name": name, "kind": clean_text(raw.get("kind"), 10),
                        "look": clean_text(raw.get("look"), 60), "owner": clean_text(raw.get("owner"), 12),
                        "inside": clean_text(raw.get("inside"), MAX_INSIDE),
                        "origin": origin,
                        "from": [x for x in dict.fromkeys(resolve(t) for t in raw.get("from") or []) if x]}
            else:
                tid = resolve(raw.get("id"))
                if not tid or what not in ITEM_MID_STEPS + ITEM_END_STEPS:
                    logger.debug("[super_enhance/step] 跳过无效已有物品步骤：%s", raw)
                    continue
                step = {"id": tid}
                if raw.get("owner"):
                    step["owner"] = clean_text(raw.get("owner"), 12)
                into = resolve(raw.get("into"))
                if into and into != tid:
                    step["into"] = into
            try:
                place = checked_place(raw.get("place"), bool(key), raw.get("name") or step.get("id"))
            except Exception:
                place = ""
            step.update({"what": what, "place": place, "holder": clean_text(raw.get("holder"), 12),
                         "state": clean_text(raw.get("state"), 40), "step": step_no(raw.get("step")) or ""})
            for field in ("amount", "used", "left", "keep", "spot", "purpose"):
                if raw.get(field) not in (None, ""):
                    step[field] = clean_text(raw[field], 30)
            conditions = weathering.clean_conditions(raw.get("weather"))
            if conditions:
                step["weather"] = conditions
            out.append({k: v for k, v in step.items() if v not in ("", None, [])})
        return out

    def _intention_line(self, item: dict, today: datetime.date | None = None) -> str:
        """补写看到的一条打算：编号（谁，和主人一起，哪天 几点，挂了几天）：内容。"""
        who = str(item.get("who") or "")
        tags = [self.label(who) or who]
        if "master" in (item.get("with") or []):
            tags.append("和主人一起")
        when = " ".join(x for x in (str(item.get("date") or "")[5:], str(item.get("window") or "")) if x)
        if when:
            tags.append(when)
        if item.get("now_at"):
            tags.append("现在就去，%s 刚答应的" % timeutil.day_hm(item.get("now_at")))
        elif today is not None and str(item.get("who") or "") != "master":
            d_txt = str(item.get("date") or "")[:10]
            if d_txt == today.isoformat():
                tags.append("就在今天，还没做，到合适时辰务必排进 segments 办掉")
            elif d_txt and d_txt < today.isoformat():
                tags.append("原定 %s 已过期还没做，尽快补办或问主人要不要改期" % d_txt[5:])
        days = stale_days(item, today) if today is not None else 0
        if days:
            tags.append("从 %s 挂到现在 %d 天了，这次要往前推或了结（见第 12 条）" % (
                timeutil.day_hm(item.get("pushed") or item.get("created")), days))
        # 怎么说起的（记录员写在 why 里）补写要看，聊天那边不给她看
        return "- %s（%s）：%s%s" % (item.get("id"), "，".join(tags), item.get("text"), why_text(item))

    def _intent_with_master(self, intent_id: str) -> bool:
        if not intent_id:
            return False
        for item in self.world.intentions():
            if str(item.get("id")) == str(intent_id):
                return "master" in (item.get("with") or [])
        return False

    def intent_id(self, value, bot: str) -> str:
        """只认她自己那几条打算的编号；对不上就空。"""
        text = str(value or "").strip().lower()
        if not text:
            return ""
        for item in self.world.intentions():
            if str(item.get("who")) == bot and str(item.get("id") or "").lower() == text:
                return str(item.get("id"))
        return ""

    @property
    def _timeout(self) -> float:
        try:
            return float(self.cfg.get("world_llm_timeout", 120) or 120)
        except Exception:
            return 120.0

    def _resolve_provider(self):
        return llm.resolve_provider(self.context, str(self.cfg.get("llm_provider_id") or "").strip())

    # ---------- 什么时候跑 ----------


    # ---------- 什么时候补写 ----------

    def need(self, bot: str, now: datetime.datetime, ahead_minutes: float = 0.0):
        """这个人现在要不要补写；要的话返回 {mode, start, cur}，不要返回 None。

        - open：手上那段（多半是记录员记的）没定结束，要先给它定个做完的时刻；
        - next：手上那段快做完了（提前 lead 分钟，ahead_minutes 用来把快到点的人顺带进来）；
        - gap：此刻什么都没在做（断了），从断的地方补起；
        - fill：后面排好的段中间空了一截（记录员或手动撤掉了几段），把这截补上，正好接上后面那段。
        """
        flat = now.replace(tzinfo=None)
        cur = self.world.current(bot, flat)
        if cur is not None:
            end = parse_time(cur.get("end")) if cur.get("end") else None
            if end is None:
                return {"mode": "open", "start": None, "cur": cur}
            if self.world.next_after(bot, end) is not None:
                hole = self._hole(bot, end, flat, ahead_minutes)
                if hole is None and self._now_waiting(bot):
                    # 手上那段后面接着记录员记的（比如往回走）：她刚答应的事从排完的地方马上接着排
                    front = self._frontier(bot, flat)
                    if front is not None:
                        return {"mode": "after", "start": front[0], "cur": front[1]}
                return hole
            lead = datetime.timedelta(minutes=self.lead_minutes + ahead_minutes)
            # 她刚答应现在就去做的事还没排：不等手上那段快做完，马上接着排
            if flat >= end - lead or self._now_waiting(bot):
                return {"mode": "next", "start": end, "cur": cur}
            return None
        floor = flat - datetime.timedelta(hours=self.window_hours)
        last = self.world.last_activity(bot, flat)
        start = floor
        if last is not None:
            moment = parse_time(last.get("end")) or parse_time(last.get("start"))
            if moment is not None and floor < moment <= flat:
                start = moment
        later = self.world.next_after(bot, flat)
        later_start = parse_time(later.get("start")) if later is not None else None
        if later_start is not None and later_start > flat:
            # 此刻断着、后面还排着：补到后面那段开始为止
            return {"mode": "fill", "start": start, "until": later_start, "cur": None, "next": later}
        return {"mode": "gap", "start": start, "cur": None}

    def _now_waiting(self, bot: str) -> bool:
        """她有一条「现在就去」的打算，世界线上还没有段挂着它，而且记下它以后补写还没跑过：要马上补写。
        补写跑过一次就不再催（排没排进去都不催），免得一直重跑。"""
        done = parse_time(self.world.last_step())
        for item in self.world.intentions(bot):
            at = parse_time(item.get("now_at"))
            if at is None:
                continue
            created = parse_time(item.get("created")) or at
            if done is not None and done >= created:
                continue
            days = sorted({at.date().isoformat(), (at.date() + datetime.timedelta(days=1)).isoformat()})
            if not any(str(x.get("intent") or "") == str(item.get("id")) for day in days for x in self.world.entries(day, bot)):
                return True
        return False

    def hole_at(self, bot: str, after: datetime.datetime, flat: datetime.datetime):
        """after 之后排好的段中间第一个空档：(空档开始, 空档后面那段)；没有返回 None。"""
        return self.world.first_hole(bot, after, flat + datetime.timedelta(hours=FILL_LOOK_HOURS))

    def _hole(self, bot: str, after: datetime.datetime, flat: datetime.datetime, ahead_minutes: float = 0.0):
        # 空档和平常接着往下写一样：快到它开始的时候才补，不提前几个小时写
        hole = self.hole_at(bot, after, flat)
        if hole is None:
            return None
        start, later = hole
        if flat < start - datetime.timedelta(minutes=self.lead_minutes + ahead_minutes):
            return None
        return {"mode": "fill", "start": start, "until": parse_time(later.get("start")), "cur": None, "next": later}

    def next_due(self, now: datetime.datetime):
        """下一次补写大概什么时候（给网页看）；已经该补了就返回此刻。"""
        flat = now.replace(tzinfo=None)
        best = None
        for bot in self.raw_log.enabled_bots():
            if self.need(bot, flat) is not None:
                return flat
            cur = self.world.current(bot, flat)
            end = parse_time(cur.get("end")) if cur is not None and cur.get("end") else None
            if end is None:
                continue
            if self.world.next_after(bot, end) is not None:
                hole = self.hole_at(bot, end, flat)
                if hole is None:
                    continue
                end = hole[0]
            moment = end - datetime.timedelta(minutes=self.lead_minutes)
            if best is None or moment < best:
                best = moment
        return best

    def due(self, now: datetime.datetime) -> bool:
        if not self.enabled or self.running:
            return False
        flat = now.replace(tzinfo=None)
        touched = self.world.manual_at()
        if touched is not None and 0 <= (flat - touched).total_seconds() < MANUAL_HOLD_SECONDS:
            return False
        failed = parse_time(self.world.last_step_attempt())
        done = parse_time(self.world.last_step())
        if failed is not None and (done is None or failed > done):
            if (flat - failed).total_seconds() < RETRY_SECONDS:
                return False
        return any(self.need(bot, flat) is not None for bot in self.raw_log.enabled_bots())


    # ---------- 提示词 ----------

    async def _persona_text(self, bot: str = "") -> str:
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return ""
        persona_id = ""  # 补写用这个机器人自己的人格（11-07 以前面板上能另指定一个，一直空着）
        try:
            persona = None
            if persona_id:
                persona = await manager.get_persona(persona_id)
                if persona is None:
                    logger.warning("[super_enhance/life] 配置的人格 %s 不存在，回退到机器人人格", persona_id)
            if persona is None:
                try:
                    persona = await manager.get_default_persona_v3(profile_umo_for_bot(bot) or None)
                except TypeError:
                    persona = await manager.get_default_persona_v3()
        except Exception as e:
            logger.warning("[super_enhance/life] 读取人格失败：%s", e)
            return ""
        if persona is None:
            return ""
        if isinstance(persona, dict):
            return str(persona.get("system_prompt") or persona.get("prompt") or "")
        return str(getattr(persona, "system_prompt", None) or getattr(persona, "prompt", "") or "")

    def _with_abilities(self, bot: str, persona: str) -> str:
        """人设后面接上「后来学会的」：人设是最初的样子，能力清单是后来的变化。"""
        if self.abilities is None:
            return persona
        try:
            text = self.abilities.render(bot)
        except Exception:
            logger.warning("[super_enhance/life] 读取能力清单失败", exc_info=True)
            return persona
        if not text:
            return persona
        return (
            (persona or "（未配置人格）")
            + "\n\n【后来的变化】下面是她在主人的训练下后来学会、或长期改变的事；"
            "和上面设定冲突时以这里为准：\n"
            + text
        )

    async def _personas(self, bots: list) -> str:
        blocks = []
        for bot in bots:
            text = self._with_abilities(bot, await self._persona_text(bot))
            blocks.append("### key = %s\n%s" % (bot, text or "（未配置人格）"))
        return "\n\n".join(blocks)


    def _routine_block(self, bots: list, now: datetime.datetime) -> str:
        lines = []
        for bot in bots:
            base = self.world.routine(bot)
            plan = self.world.day_plan(bot, now.date().isoformat())
            tomorrow = self.world.day_plan(bot, (now.date() + datetime.timedelta(days=1)).isoformat())
            if not base:
                lines.append("- %s：还没有作息骨架" % (self.label(bot) or bot))
                continue
            meals = "、".join(base.get("meals") or []) or "（没定）"
            # 今天的种子抽了吃哪几顿，就只写那几顿（老种子没抽，照旧写三餐时段）
            today = meal_slots((self._seed_for(now.date().isoformat()) or {}).get("meals"), list(base.get("meals") or []))
            day0, day1 = now.date().isoformat()[5:], (now.date() + datetime.timedelta(days=1)).isoformat()[5:]
            sleep = "%s 定的 %s" % (day0, plan.get("sleep") or "?")
            wake_at = parse_time("%s %s" % (now.date().isoformat(), plan.get("wake"))) if plan.get("wake") else None
            if wake_at is not None and now.replace(tzinfo=None) < wake_at:
                # 过了半夜、还没到这天起床的时候：这会儿还是前一晚那一觉，别把这天晚上的入睡当成还没到
                last_day = now.date() - datetime.timedelta(days=1)
                last = self.world.day_plan(bot, last_day.isoformat())
                sleep = "%s 晚上定的 %s；现在是夜里，还没到 %s 定的起床时刻" % (
                    last_day.isoformat()[5:], last.get("sleep") or "?", day0)
            lines.append(
                "- %s：起床 %s（%s 定的 %s，%s 定的 %s）、入睡 %s（%s）、%s%s"
                % (
                    self.label(bot) or bot,
                    base.get("wake") or "?",
                    day0,
                    plan.get("wake") or "?",
                    day1,
                    tomorrow.get("wake") or "?",
                    base.get("sleep") or "?",
                    sleep,
                    today or "平时三餐在这几个时间段里：%s" % meals,
                    "；备注：%s" % base["notes"] if base.get("notes") else "",
                )
            )
        return "\n".join(lines) if lines else "（没有骨架）"


    def _promise_lines(self, now: datetime.datetime) -> list:
        book = getattr(self, "promises", None)
        if book is None:
            return []
        today = now.date().isoformat()
        tomorrow = (now.date() + datetime.timedelta(days=1)).isoformat()
        out = []
        try:
            for item in book.items():
                if str(item.get("status")) != "open" or str(item.get("date") or "") not in (today, tomorrow):
                    continue
                names = "、".join(self.label(b) or b for b in (item.get("bots") or []))
                out.append("- %s %s %s：%s%s" % (
                    item.get("date"), item.get("time") or "（没定钟点）", names, item.get("what"),
                    "（和%s）" % item["with"] if item.get("with") else "",
                ))
        except Exception:
            pass
        return out

    def _apply_stop_results(self, raw, date_text: str) -> None:
        """补写给被打断的事写的「后来怎么样了」：存进那段的 stop_result，给她看成「（结果：……）」。"""
        if not isinstance(raw, dict):
            return
        entries = {str(x.get("id")): x for x in self.world.entries(date_text, "")}
        for entry_id, text in raw.items():
            entry = entries.get(str(entry_id).strip())
            text = clean_text(text, 80)
            if entry is None or not text or not real_stop(entry) or entry.get("stop_result"):
                continue
            self.world.update(date_text, str(entry.get("id")), stop_result=text)
            logger.info("[super_enhance/world] 被打断的 %s「%s」后来：%s", entry.get("id"), clean_text(entry.get("doing"), 20), text)

    def _intent_moves(self, raw, tasks: dict, flat: datetime.datetime) -> dict:
        """补写给挂太久的打算写的「后来怎样」：只认这次要写的人、标着挂太久的那几条。
        返回 {编号: {"done": bool, "text", "date", "window"}}；落库在 _apply_intent_moves（段排完以后）。"""
        if not isinstance(raw, dict):
            return {}
        stale = {str(x.get("id")): x for x in self.world.intentions()
                 if str(x.get("who")) in tasks and stale_days(x, flat.date())}
        out = {}
        for key, move in raw.items():
            item = stale.get(str(key).strip().lower()) or stale.get(str(key).strip())
            if item is None or not isinstance(move, dict):
                continue
            done = move.get("done") is True or str(move.get("done")).strip().lower() == "true"
            day = str(move.get("date") or "").strip()[:10]
            out[str(item.get("id"))] = {"done": done, "text": clean_text(move.get("text"), 300),
                                        "date": day if len(day) == 10 and day[4] == "-" else "",
                                        "window": clean_text(move.get("window"), 60)}
        return out

    def _apply_intent_moves(self, moves: dict, written: dict, flat: datetime.datetime) -> None:
        """挂太久的打算落库：做完 / 放下了的，有段挂着它就等那段做完照常撤（proactive 里），没段挂着就现在撤；
        往前推了的改写内容、记下这次推过（pushed），从这天起重新算挂了几天。
        没写 intent_moves、可这次有段挂着它的，也算推过，免得下回又催。"""
        carried = {str(x.get("intent")) for rows in written.values() for x in rows if x.get("intent")}
        for intent_id, move in moves.items():
            if move["done"]:
                if intent_id not in carried and self.world.drop_intention(intent_id):
                    logger.info("[super_enhance/world] 补写了结了挂太久的打算 %s，撤掉", intent_id)
                elif intent_id in carried:
                    self.world.update_intention(intent_id, pushed=fmt_time(flat), source=None)
                continue
            self.world.update_intention(intent_id, text=move["text"] or None, window=move["window"] or None,
                                        date=move["date"] or None, pushed=fmt_time(flat), source=None)
            logger.info("[super_enhance/world] 补写把挂太久的打算 %s 往前推了：%s", intent_id, move["text"] or "（内容没改）")
        for item in self.world.intentions():
            if str(item.get("id")) in carried and str(item.get("id")) not in moves and stale_days(item, flat.date()):
                self.world.update_intention(str(item.get("id")), pushed=fmt_time(flat), source=None)

    @staticmethod
    def _things_moment(tasks: dict | None, flat: datetime.datetime) -> datetime.datetime:
        """回补过去时从最早的补写起点取库存，后续取得须沿事件推进。"""
        starts = [x.get("start") for x in (tasks or {}).values() if isinstance(x, dict)]
        starts = [x for x in starts if isinstance(x, datetime.datetime)]
        return min([flat] + [x.replace(tzinfo=None) for x in starts])

    def _history_blocks(self, bots: list, flat: datetime.datetime, date_offsets: tuple[int, ...] = (1, 0)) -> list[str]:
        if getattr(self, "things", None) is not None:
            self.things.view(flat)
        blocks = []
        for date_offset in date_offsets:
            date_text = (flat.date() - datetime.timedelta(days=date_offset)).isoformat()
            lines = []
            for bot in bots:
                for item in self.world.entries(date_text, bot):
                    start = parse_time(item.get("start"))
                    if start is None or start > flat:
                        continue
                    if is_event(item):
                        lines.append("- %s %s（顺带的事）%s" % (
                            self.label(bot) or bot, timeutil.day_hm(item.get("start")), item.get("doing") or item.get("place")))
                        continue
                    lines.append("- %s %s %s %s%s%s" % (
                        self.label(bot) or bot,
                        timeutil.span_text(item.get("start"), item.get("end"), date_prefix=True, ongoing="~…"),
                        item.get("place") or "",
                        event_text_at(item, flat),
                        "",
                        mood_text(item) if parse_time(item.get("end")) is not None and parse_time(item["end"]) <= flat else "",
                    ))
            master = [
                x for x in self.world.entries(date_text, "master")
                if parse_time(x.get("start")) and parse_time(x.get("start")) <= flat
            ]
            for item in master:
                lines.append(
                    "- 主人 %s %s（%s）"
                    % (timeutil.day_hm(item.get("start")), item.get("place"), item.get("doing") or "")
                )
            if lines:
                blocks.append("【%s 已经发生的】\n%s" % (date_text, "\n".join(lines)))
        return blocks

    def _today_proactive_block(self, now_or_bots, flat: datetime.datetime | None = None) -> str:
        """今天 04:00 起已发过的主动消息（含主人是否已回）+ 尚未触发的已排待发节点。"""
        if flat is None:
            moment = now_or_bots if isinstance(now_or_bots, datetime.datetime) else self._now()
            flat = moment.replace(tzinfo=None)
            bots = list(self.raw_log.enabled_bots()) if self.raw_log is not None else []
        else:
            bots = list(now_or_bots or [])
            flat = flat.replace(tzinfo=None)
        anchor_day = flat.date() - datetime.timedelta(days=1) if flat.hour < 4 else flat.date()
        cutoff = datetime.datetime.combine(anchor_day, datetime.time(4, 0))
        tz = self._now().tzinfo or timeutil.timezone(self.cfg, self.context, "Asia/Shanghai")
        cutoff_ts = cutoff.replace(tzinfo=tz).timestamp() if tz else cutoff.timestamp()
        days = sorted({anchor_day.isoformat(), flat.date().isoformat()})
        sent_lines = []
        if self.raw_log is not None:
            raw_rows = []
            for day in days:
                try:
                    raw_rows.extend(self.raw_log.read_day(day) or [])
                except Exception:
                    continue
            by_umo: dict[str, list[dict]] = {}
            for row in raw_rows:
                if not isinstance(row, dict):
                    continue
                umo = str(row.get("umo") or "")
                if umo:
                    by_umo.setdefault(umo, []).append(row)
            for umo in by_umo:
                by_umo[umo].sort(key=lambda x: float(x.get("ts") or 0))
            proactive_hits = []
            for umo, rows in by_umo.items():
                bot = bot_of_umo(umo)
                if bot not in bots:
                    continue
                for idx, row in enumerate(rows):
                    ts = float(row.get("ts") or 0)
                    if ts < cutoff_ts or row.get("role") != "assistant" or row.get("src") != "proactive":
                        continue
                    next_proactive_ts = next(
                        (
                            float(r.get("ts") or 0)
                            for r in rows[idx + 1:]
                            if r.get("role") == "assistant" and r.get("src") == "proactive"
                        ),
                        float("inf"),
                    )
                    replied = any(
                        r.get("role") == "user" and ts < float(r.get("ts") or 0) <= next_proactive_ts
                        for r in rows[idx + 1:]
                    )
                    hm = str(row.get("time") or "")[-5:] or datetime.datetime.fromtimestamp(ts, tz).strftime("%H:%M")
                    proactive_hits.append((
                        ts,
                        "- %s %s：「%s」（%s）" % (
                            self.label(bot) or bot,
                            hm,
                            clean_text(row.get("text"), 70),
                            "主人已回" if replied else "主人未回",
                        ),
                    ))
            proactive_hits.sort(key=lambda x: x[0])
            sent_lines = [line for _ts, line in proactive_hits[-16:]]
        upcoming_lines = []
        for bot in bots:
            fired = self.world.fired_msgs(bot) if hasattr(self.world, "fired_msgs") else set()
            for offset in (0, 1):
                day = (flat.date() + datetime.timedelta(days=offset)).isoformat()
                for item in self.world.entries(day, bot):
                    if not item.get("send_msg") or real_stop(item):
                        continue
                    at_tag = str(item.get("msg_at") or "end")
                    key = f"{day}/{item.get('id')}:{at_tag}"
                    if key in fired:
                        continue
                    trig = _trigger_time_of(item)
                    if trig is None or trig < flat - datetime.timedelta(minutes=5):
                        continue
                    upcoming_lines.append(
                        "- [已排待发] %s %s（%s）：%s"
                        % (
                            self.label(bot) or bot,
                            timeutil.day_hm(fmt_time(trig)),
                            "段头" if at_tag == "start" else "段尾",
                            clean_text(item.get("msg_purpose") or item.get("doing"), 80),
                        )
                    )
        if not sent_lines and not upcoming_lines:
            return "今天已发过的主动消息：今天 04:00 以来还没主动发过消息。"
        out = ["今天已发过的主动消息（避免重复说同一件事；若连发多条主人未回则少发非固定节点）："]
        out.extend(sent_lines)
        out.extend(upcoming_lines)
        return "\n".join(out)

    def _context_blocks_split(self, bots: list, now: datetime.datetime, tasks: dict | None = None) -> tuple[str, str]:
        """按动静分层返回（昨日与慢变库存, 今日实时与即时现场），方便补写前缀缓存最大化命中。"""
        flat = now.replace(tzinfo=None)
        slow_parts: list[str] = []
        live_parts: list[str] = []

        # 1. 昨天已发生（今天一整天不变，放最前）
        slow_parts.extend(self._history_blocks(bots, flat, date_offsets=(1,)))

        # 2. 近况 / 约定 / 在练 / 约束 / 打算 / 拍板
        companion = getattr(self, "companionship", None)
        if companion is not None:
            # LIVING_NOTE 已由规则 25 完整覆盖、d 待拍板/已拍板由下方共享段集中列出，此处只输出去重后的主人即时状态与各人近况。
            seen_owner_states: set[str] = set()
            for bot in bots:
                owner_line = companion.safe("owner_state_text", bot, flat, default="")
                include_owner = bool(owner_line and owner_line not in seen_owner_states)
                if owner_line:
                    seen_owner_states.add(owner_line)
                text = companion.safe(
                    "render", bot, flat,
                    include_owner_state=include_owner,
                    include_decisions=False,
                    default="",
                )
                if text:
                    slow_parts.append("%s：\n%s" % (self.label(bot) or bot, text))
        if companion is not None:
            pend_decs = companion.safe("pending_decisions", flat, default=[]) or []
            if pend_decs:
                today_text = flat.date().isoformat()
                slow_parts.append(
                    "【待当家主人拍板的事（主人尚未回话前保持挂起，严禁擅自替主人做主买定大件或私自接单！）】：\n"
                    + "\n".join(
                        format_decision_line(d, today_text, label_fn=self.label, include_bot=True, show_status=False)
                        for d in pend_decs
                    )
                )
            done_decs = companion.safe("recent_resolved_decisions", flat, default=[]) or []
            if done_decs:
                slow_parts.append(
                    "【近期已由主人拍板定准的事（照主人定下的主意办，严禁再用 ask_master 重复问同一件事！）】：\n"
                    + "\n".join(
                        "- %s（%s，%s 定下%s）：%s → 已定：%s" % (
                            d.get("id"), self.label(d.get("bot")) or d.get("bot"),
                            d.get("resolved_at") or str(d.get("created") or ""),
                            "，对应打算 %s" % d["intent"] if d.get("intent") else "",
                            clean_text(d.get("question"), 100), clean_text(d.get("choice"), 100),
                        )
                        for d in done_decs[-4:]
                    )
                )
        constraints = self.world.constraints(active_only=True, scene=self.home)
        if constraints:
            slow_parts.append(
                "生效中的约束：\n"
                + "\n".join(
                    "- %s（%s，%s 起）：%s" % (x.get("id"), x.get("who"), x.get("since"), x.get("text"))
                    for x in constraints
                )
            )
        promises = self._promise_lines(flat)
        if promises:
            slow_parts.append("还没做的约定：\n" + "\n".join(promises))
        book = getattr(self, "trainings", None)
        if book is not None:
            try:
                for item in book.items():
                    if str(item.get("status")) != "open":
                        continue
                    slow_parts.append(
                        "在练的事 #%s（%s）：%s；段里在练它就把 training 填成 #%s"
                        % (item.get("id"), item.get("bot"), item.get("what"), item.get("id"))
                    )
            except Exception:
                pass
        intentions = [x for x in self.world.intentions() if str(x.get("who")) != "master"]
        if intentions:
            slow_parts.append("各自的打算：\n" + "\n".join(self._intention_line(x, flat.date()) for x in intentions))
        plans = self.world.intentions("master")
        if plans:
            slow_parts.append(
                "主人说过他要做的事（写着和她们一起、要她们做的，照着排进去；里头怎么做还没说定的，先填 ask_master 问主人）：\n"
                + "\n".join("- %s %s：%s%s" % (
                    str(x.get("date") or "")[5:] or "日子没说", x.get("window") or "", x.get("text"), why_text(x))
                    for x in plans)
            )

        # 3. 家底与盘点
        store = getattr(self, "things", None)
        if store is not None:
            try:
                moment = self._things_moment(tasks, flat)
                when = "此刻" if moment <= flat else "到 %s" % timeutil.day_hm(moment)
                shelf = self._shelf_at(moment)
                slow_parts.append("家里的吃的（%s；每件前面是编号）：\n" % when + store.food_list(moment, shelf, self.label))
                slow_parts.append("家里的东西（吃的以外，%s；每件前面是编号）：\n" % when + store.item_list(moment, shelf, self.label))
                if hasattr(store, "charcoal_summary"):
                    charcoal_line = store.charcoal_summary(moment, shelf)
                    if charcoal_line:
                        slow_parts.append(charcoal_line)
                if hasattr(store, "cooking_staple_alert"):
                    staple_line = store.cooking_staple_alert(moment, shelf)
                    if staple_line:
                        slow_parts.append(staple_line)
                living_nudges = self._seaside_living_nudges(tasks, flat, shelf)
                if living_nudges:
                    slow_parts.append(living_nudges)
            except Exception:
                logger.warning("[super_enhance/world] 补写算家里的吃的失败", exc_info=True)

        money_book = getattr(self, "money", None)
        if money_book is not None and self.home == "海边小镇":
            try:
                moment = self._things_moment(tasks, flat)
                money_book.archive_settled(moment, self.world)
                slow_parts.append(money_book.step_block(moment, self.world))
            except Exception:
                logger.warning("[super_enhance/money] 补写拼账面失败", exc_info=True)

        # 4. 今天进展与当前实时状态（放动态后半）
        live_parts.extend(self._history_blocks(bots, flat, date_offsets=(0,)))
        proactive_text = self._today_proactive_block(bots, flat)
        if proactive_text:
            live_parts.append(proactive_text)
        stops = []
        today_text = flat.date().isoformat()
        for bot in bots:
            for item in self.world.entries(today_text, bot):
                end = parse_time(item.get("end")) if item.get("end") else None
                if end is None or end > flat or not real_stop(item) or item.get("stop_result"):
                    continue
                stops.append("- %s %s %s %s" % (item.get("id"), self.label(bot) or bot,
                                                 timeutil.span_text(item.get("start"), item.get("end"), date_prefix=True),
                                                 doing_text(item)))
        if stops:
            live_parts.append("被打断、还没交代后来怎么样的事（最前面是编号）：\n" + "\n".join(stops))
        now_lines = []
        for bot in bots:
            cur = self.world.current(bot, flat)
            if cur is None:
                now_lines.append("- %s：这会儿没有记录" % (self.label(bot) or bot))
                continue
            pct = None if cur.get("idle") else progress_of(cur, flat)
            now_lines.append("- %s：%s %s%s（%s 起%s%s）" % (
                self.label(bot) or bot, cur.get("place") or "", cur.get("doing") or "",
                "（闲着）" if cur.get("idle") else "（睡着）" if is_sleep(cur) else event_text_at(cur, flat),
                timeutil.day_hm(cur.get("start")),
                "，预计 %s 做完" % timeutil.day_hm(cur.get("end")) if cur.get("end") else "，还没定什么时候做完",
                "，做了 %d%%" % pct if pct is not None else "",
            ))
        live_parts.append("她们此刻手上的事：\n" + "\n".join(now_lines))
        planned = []
        for bot in bots:
            info = (tasks or {}).get(bot)
            if info is not None and info.get("mode") != "after":
                continue
            upto = info["start"] if info is not None else None
            for date_offset in (0, 1):
                date_text = (flat.date() + datetime.timedelta(days=date_offset)).isoformat()
                for item in self.world.entries(date_text, bot):
                    start = parse_time(item.get("start"))
                    if start is None or start <= flat or is_event(item) or (upto is not None and start >= upto):
                        continue
                    planned.append("- %s %s %s %s%s" % (
                        self.label(bot) or bot,
                        timeutil.span_text(item.get("start"), item.get("end"), date_prefix=True, ongoing="~…"),
                        item.get("place") or "",
                        event_text_at(item, flat),
                        "（和%s）" % "、".join(self.label(x) or x for x in item["with"]) if item.get("with") else "",
                    ))
        if planned:
            live_parts.append("已经排好、这次不改的：\n" + "\n".join(planned))
        trail = []
        for back in (1, 0):
            day = (flat.date() - datetime.timedelta(days=back)).isoformat()
            trail.extend(x for x in self.world.master_trail(day)
                         if parse_time(x.get("start")) is not None and parse_time(x.get("start")) <= flat)
        master_latest = trail[-1] if trail else None
        master_cur = self.world.current("master", flat)
        master_sleeping = (master_cur is not None and is_sleep(master_cur)) or bool(
            companion is not None and companion.safe("resting", flat, default=False)
        )
        sleep_room = "船舱" if self.home == "海边小镇" else "卧房"
        table_spot = "船舱矮桌" if self.home == "海边小镇" else "堂屋矮桌"
        sleep_tail = (
            "\n【主人眼下正在%s睡觉（尚未醒来）】除非他主动发消息或起床，后续排程严禁把 master 写进 with，"
            "严禁在舱内或头顶甲板敲铁凿木弄出大动静；做饭留饭轻手轻脚盖好保温，若两人都要离开住处，须留张字条压在%s上。"
            % (sleep_room, table_spot)
            if master_sleeping else ""
        )
        if master_latest:
            where = next((x for x in reversed(trail) if str(x.get("place") or "").strip()), None)
            owner = str(master_latest.get("who") or "")
            live_parts.append(
                "主人最近的动向（只作参考，不许改写、不许替他编）：%s %s%s%s"
                % (
                    str(master_latest.get("start"))[5:16],
                    "和%s一起，" % (self.label(owner) or owner) if owner != "master" else "",
                    master_latest.get("place") or "",
                    "，" + clean_text(master_latest.get("doing"), 80) if master_latest.get("doing") else "",
                )
                + (
                    "；最后一次说到他在哪：%s %s（之后没说过离开）"
                    % (str(where.get("start"))[5:16], where.get("place"))
                    if where is not None else "；%s 以来还没说过他在哪" % (flat.date() - datetime.timedelta(days=1)).isoformat()[5:]
                )
                + sleep_tail
            )
        elif sleep_tail:
            live_parts.append("主人最近的动向（只作参考，不许改写、不许替他编）：" + sleep_tail.lstrip("\n"))
        if companion is not None and not master_sleeping and 8 <= flat.hour <= 22:
            pend_decs = companion.safe("pending_decisions", flat, default=[]) or []
            asked_today = int(companion.safe("today_ask_count", flat, default=0) or 0)
            if not pend_decs and asked_today < 3:
                live_parts.append(
                    "【当家人参与提示】主人此刻醒着，今天已请示 %d 次且没有挂着未回的待拍板事项——若遇到全新还没请示过的「置办过冬大件打听到了行情」「有人来打捞船找船长接单」「快到正餐点拿不准主人回不回来吃或吃哪样」「捞到了封口旧物想喊主人一起拆看」，可填写顶层 ask_master（kind 填 \"decision\"）向主人发话；已经在上方「近期已由主人拍板定准的事」或「各自的打算」里定过主意的事，严禁再用 ask_master 重复问一遍！"
                    % asked_today
                )
        return "\n\n".join(slow_parts), "\n\n".join(live_parts)

    def _context_block(self, bots: list, now: datetime.datetime, tasks: dict | None = None) -> str:
        slow, live = self._context_blocks_split(bots, now, tasks)
        return "\n\n".join(x for x in (slow, live) if x)

    def _task_block(self, tasks: dict) -> str:
        lines = []
        for bot, info in tasks.items():
            name = "%s（%s）" % (self.label(bot) or bot, bot)
            cur = info.get("cur") or {}
            if info["mode"] == "open":
                lines.append(
                    "- %s：手上「%s」（%s 起）还没定什么时候做完。先在 end_current 里给它定一个做完的时刻"
                    "（不早于此刻之后五分钟），再从那个时刻往下写。"
                    % (name, clean_text(cur.get("doing") or cur.get("place"), 60), timeutil.day_hm(cur.get("start")))
                )
            elif info["mode"] == "fill":
                later = info.get("next") or {}
                place = clean_text(later.get("place") or "", 40)
                lines.append(
                    "- %s：%s 到 %s 这一截空着（原来排的撤掉了），把它补上：从 %s 开始写，最后一段在 %s 结束，"
                    "接上后面已经排好的「%s」（%s 起%s，这段不改）。%s"
                    % (name, timeutil.day_hm(info["start"]), timeutil.day_hm(info["until"]), fmt_time(info["start"]),
                       fmt_time(info["until"]), clean_text(later.get("doing") or later.get("place"), 60),
                       timeutil.day_hm(info["until"]), "，在%s" % place if place else "",
                       self._where(self.world.last_activity(bot, info["start"] - datetime.timedelta(minutes=1)), "空着之前那段"))
                )
            elif info["mode"] == "after":
                lines.append(
                    "- %s：已经排到 %s（最后一段「%s」），从 %s 开始往下写；在那之前排好的不改。%s"
                    % (name, timeutil.day_hm(info["start"]), clean_text(cur.get("doing") or cur.get("place"), 60),
                       fmt_time(info["start"]), self._where(cur))
                )
            elif info["mode"] == "next":
                lines.append(
                    "- %s：手上「%s」%s 做完，从 %s 开始往下写。%s"
                    % (name, clean_text(cur.get("doing") or cur.get("place"), 60),
                       timeutil.day_hm(info["start"]), fmt_time(info["start"]), self._where(cur))
                )
            else:
                last = self.world.last_activity(bot, info["start"])
                lines.append(
                    "- %s：%s 之后没有记录。从 %s 开始写（已经过去的那部分照常补上），一直写到此刻之后。%s"
                    % (name, fmt_time(info["start"]), fmt_time(info["start"]), self._where(last))
                )
        return "\n".join(lines)

    def _where(self, entry, what: str = "那段") -> str:
        """接着写时交代她上一段结束时的样子：在哪、结果怎样（第一段从这儿接上，人在外面就先把路走完）；没有那段就空。"""
        if not entry or is_event(entry):
            return ""
        moment = self._now().replace(tzinfo=None)
        ended = parse_time(entry.get("end"))
        if ended is None or ended > moment:
            return "%s截至此刻：%s；后续结果尚未发生。" % (what, event_text_at(entry, moment))
        out = "%s结束时她在：%s" % (what, clean_text(entry.get("place") or "", 40) or "（没写地方）")
        if entry.get("at_home") is False:
            out += "，人不在宿舍"
        worn = clean_text(entry.get("outfit") or "", 80)
        if not worn:
            from .view import current_outfit_of
            e_who = str(entry.get("who") or "")
            worn = current_outfit_of(self.world, e_who, self.label(e_who) or e_who, ended or moment)
        if worn:
            out += "（当时身上穿的：%s）" % worn
        if real_stop(entry):
            stopped = clean_text(entry.get("stop_result") or "", 60)
            return out + ("（停下时：%s）。" % stopped if stopped else "。")
        return out + result_text(entry) + "。"

    async def build_system_prompt(self, bots: list[str] | None = None) -> str:
        """构造全静态的补写 system_prompt（人设、地图、规则、规范、示例、常识），不含任何会随轮次变动的时刻。"""
        all_bots = list(bots if bots is not None else self.raw_log.enabled_bots())
        roster = "\n".join(
            "- key = %s（%s）" % (bot, self.label(bot) or bot) for bot in all_bots
        )
        home_static = (
            "她们现在住的地方：\n"
            "- 她们住在【%s】。写出来的地点和活动都在%s这一带，具体去哪儿，从上面人设里这一带的地图里挑。\n"
            "- 人设里写的其它地方（比如从前待过的镇子）只是背景，人现在不在那儿，不要写成此刻发生的事。"
            % (self.home, self.home)
        )
        return localize(STEP_SYSTEM_TEMPLATE.format(
            base_system=SYSTEM_PROMPT,
            roster=roster,
            personas=await self._personas(all_bots),
            home_static=home_static,
            world_notes=self.world_notes,
            money_guide=("   " + SEASIDE_PRICE_GUIDE.replace("\n", "\n   ") if self.home == "海边小镇" else ""),
            step_notes=self.step_notes,
            kinds=" / ".join(KINDS),
            keeps=" / ".join(KEEPS),
            spots=" / ".join(spots_for(self.home)),
            lifecycle_rules=STEP_LIFECYCLE_RULES.strip(),
        ), self.home)

    async def build_prompt(self, tasks: dict, now: datetime.datetime) -> str:
        """构造补写 user prompt，严格按「天级稳定 → 慢变/库存 → 实时动态 → 当前时刻与任务」排序。"""
        bots = list(self.raw_log.enabled_bots())
        labels = {bot: (self.label(bot) or bot) for bot in bots}
        win_seed, night = self._draw_window_for(tasks, now, labels)
        self._last_cand_map = self._build_candidate_catalog(win_seed, labels, night) if win_seed else {}
        seed_text = self._seed_text(win_seed, labels, night, cand_map=self._last_cand_map) if win_seed else ""
        weather_text = ""
        try:
            weather_text = self._weather_block(
                min((x["start"] for x in tasks.values() if x.get("start")), default=None) or now, now
            ).strip()
        except Exception:
            logger.warning("[super_enhance/step] 天气时间线没拼上", exc_info=True)
        arrived = self._arrived_line(now).strip().lstrip("- ").strip()
        routine = "作息骨架：\n" + self._routine_block(bots, now)
        slow_ctx, live_ctx = self._context_blocks_split(bots, now, tasks)
        horizon = fmt_time(max(min(x["start"] or now for x in tasks.values()), now) + datetime.timedelta(hours=HORIZON_HOURS))
        tail_turn = (
            "现在是 %s。本次最多写到 %s 左右（规则 1 中的「现在」与「本次截止时刻」均以此处为准），下面这几位角色接下来做什么，由你来安排。\n\n这次要你写的：\n%s"
            % (fmt_time(now), horizon, self._task_block(tasks))
        )
        blocks = [
            arrived,
            routine,
            slow_ctx,
            seed_text,
            weather_text,
            live_ctx,
            tail_turn,
        ]
        return localize("\n\n".join(x for x in blocks if x), self.home)


    # ---------- 执行 ----------

    def _frontier(self, bot: str, flat: datetime.datetime):
        """她排好的段一直接到哪儿：(排完的时刻, 最后那段)。手上那段没定结束、此刻没在做事、
        或者后面断了一截又排着的（那是 fill 的事），返回 None。"""
        cur = self.world.current(bot, flat)
        if cur is None or not cur.get("end"):
            return None
        edge, last = parse_time(cur.get("end")), cur
        while edge is not None:
            later = self.world.next_after(bot, edge)
            if later is None:
                return edge, last
            start = parse_time(later.get("start"))
            if start is None or start > edge + datetime.timedelta(minutes=2) or not later.get("end"):
                return None
            if str(later.get("id")) == str(last.get("id")):
                # 防自环：拿回来的还是同一段，就当排到头了
                return edge, last
            edge, last = parse_time(later.get("end")), later
        return None

    def tasks_for(self, now: datetime.datetime, force: bool = False) -> dict:
        """这次要写的人：该补的，加上 BATCH_MINUTES 分钟内也要补的（一起写省一次调用）；
        再加上排好的段在这次要写到的时刻之前就排完的人，从她排完的地方接着写（mode=after），
        这样两个人后面的事是一起排出来的，不会一个已经排出去两小时、另一个只能绕着她排。"""
        bots = list(self.raw_log.enabled_bots())
        tasks = {}
        for bot in bots:
            info = self.need(bot, now)
            if info is not None:
                tasks[bot] = info
        if not tasks and not force:
            return {}
        for bot in bots:
            if bot in tasks:
                continue
            info = self.need(bot, now, ahead_minutes=BATCH_MINUTES)
            if info is not None:
                tasks[bot] = info
        flat = now.replace(tzinfo=None)
        starts = [x["start"] for x in tasks.values() if x.get("start")]
        reach = max(min(starts) if starts else flat, flat) + datetime.timedelta(hours=HORIZON_HOURS)
        for bot in bots:
            if bot in tasks:
                continue
            front = self._frontier(bot, flat)
            if front is not None:
                edge, last = front
                # 手动补写（force）：不看有没有缺口，也不推翻已排好的，直接从她排到的最后一段往后接
                if force or flat < edge <= reach - datetime.timedelta(minutes=JOIN_ROOM_MINUTES):
                    tasks[bot] = {"mode": "after", "start": edge, "cur": last}
                continue
            # 中间有被撤出的空洞会阻断 _frontier；手动补写或另一人排程已跨越洞口时，把洞纳入本次补写避免死锁
            cur = self.world.current(bot, flat)
            after = parse_time(cur.get("end")) if cur is not None and cur.get("end") else flat
            hole = self.hole_at(bot, after, flat)
            if hole is not None:
                h_start, later = hole
                if force or h_start <= reach - datetime.timedelta(minutes=JOIN_ROOM_MINUTES):
                    tasks[bot] = {
                        "mode": "fill",
                        "start": h_start,
                        "until": parse_time(later.get("start")),
                        "cur": None,
                        "next": later,
                    }
        return tasks

    def _save_thought(self, moment: datetime.datetime, thought: str, ok: bool, note: str = "") -> None:
        """把这次补写的思考存档（网页「世界线」页可看），保留最近 8 次。"""
        try:
            path = self.world.root / "step_thoughts.json"
            items = []
            if path.exists():
                data = jsonio.load(path, [])
                if isinstance(data, list):
                    items = data
            items.append({
                "at": fmt_time(moment),
                "ok": bool(ok),
                "note": str(note or "")[:120],
                "thought": str(thought or "")[:8000],
            })
            jsonio.save(path, items[-8:], indent=1)
        except Exception:
            logger.warning("[super_enhance/world] 补写思考存档失败", exc_info=True)

    async def _validate_with_retries(
        self,
        prompt: str,
        raw_text: str,
        thought: str,
        tasks: dict,
        moment: datetime.datetime,
        log_prefix: str = "补写",
        *,
        master_replied: bool = False,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> tuple[dict | None, str, str, list[str]]:
        """对模型返回的 JSON 执行纯结构化字段校验；不兜底瞎猜，有缺失或违规字段时把报错逐条发回模型重试（最多 MAX_VALIDATE_RETRIES 次）。"""
        cur_text, cur_thought = raw_text, thought
        data = llm.extract_json(cur_text)
        errors = ["返回内容不是合法的 JSON 对象"] if not isinstance(data, dict) else self.validate_payload(data, tasks, moment, master_replied=master_replied)
        for retry_idx in range(1, MAX_VALIDATE_RETRIES + 1):
            if not errors:
                return data, cur_text, cur_thought, []
            err_block = "\n".join(f"- {err}" for err in errors[:16])
            logger.warning(
                "[super_enhance/world] %s结构化校验未通过（第 %d/%d 次打回重写）：%s",
                log_prefix, retry_idx, MAX_VALIDATE_RETRIES, "；".join(errors[:6]),
            )
            fix_prompt = (
                prompt
                + "\n\n你上一次的输出：\n"
                + str(cur_text or "").strip()
                + "\n\n【结构化字段校验未通过，请严格逐条修正下面所有报错，并重新输出完整合法的 JSON（未报错的段保留并补齐必填字段，不要省略）】：\n"
                + err_block
            )
            cur_text, cur_thought = await llm.text_chat(
                self.context,
                fix_prompt,
                provider_id=str(self.cfg.get("llm_provider_id") or "").strip(),
                timeout=self.timeout,
                session_id="super_enhance_lifestep",
                system_prompt=system_prompt,
                attempts=2,
                log_label=f"{log_prefix}校验重试{retry_idx}",
            )
            self._run_cache = {}
            data = llm.extract_json(cur_text)
            errors = ["返回内容不是合法的 JSON 对象"] if not isinstance(data, dict) else self.validate_payload(data, tasks, moment, master_replied=master_replied)
        return (data if not errors else None), cur_text, cur_thought, errors

    async def _flush_idea_decisions(self, moment: datetime.datetime) -> None:
        """主人醒着且白天时，把夜间暂存的 idea 状态待拍板 d 事项发出去请示（不再永远卡在 idea）。"""
        companion = getattr(self, "companionship", None)
        planner = getattr(self, "planner", None)
        if companion is None or planner is None or not planner.enabled:
            return
        if not (8 <= moment.hour <= 22) or companion.safe("resting", moment, default=False):
            return
        pend = companion.safe("pending_decisions", moment, default=[]) or []
        if any(d.get("status") == "pending" for d in pend):
            return
        for dec in pend:
            if dec.get("status") != "idea":
                continue
            bot = str(dec.get("bot") or "")
            q = str(dec.get("question") or "")
            dec_id = str(dec.get("id") or "")
            if bot and q and dec_id and await planner.ask_now(bot, q, invitation_id=dec_id):
                companion.safe("mark_decision_asked", dec_id, moment)
                logger.info("[super_enhance/world] 主人已醒，发出此前暂存的待拍板请示 %s：%s", dec_id, q[:40])
            break

    async def run(self, now: datetime.datetime | None = None, force: bool = False) -> bool:
        # 世界线里的时间都是不带时区的本地时间，这里统一成一样的口径
        moment = (now or self._now()).replace(tzinfo=None)
        self._run_cache = {}
        await self._flush_idea_decisions(moment)
        tasks = self.tasks_for(moment, force=force)
        if not tasks:
            self._run_cache = {}
            return False
        provider = self._resolve_provider()
        if provider is None:
            self.world.note_error("step", "没有可用的模型")
            self.world.set_last_step_attempt(fmt_time(moment))
            self._run_cache = {}
            return False
        self.running = True
        try:
            system_prompt = await self.build_system_prompt()
            prompt = await self.build_prompt(tasks, moment)
            text, thought = await llm.text_chat(
                self.context,
                prompt,
                provider_id=str(self.cfg.get("llm_provider_id") or "").strip(),
                timeout=self.timeout,
                session_id="super_enhance_lifestep",
                system_prompt=system_prompt,
                attempts=2,
                log_label="补写",
            )
            # 等模型的这几分钟里网页可能改过种子、存货：落库前重新读并做纯结构化强校验（最多重试 3 次，放弃瞎猜兜底）
            self._run_cache = {}
            data, text, thought, val_errors = await self._validate_with_retries(prompt, text, thought, tasks, moment, "补写", system_prompt=system_prompt)
            if data is None:
                err_msg = "连续 %d 次重试仍未通过结构化校验：%s" % (MAX_VALIDATE_RETRIES, "；".join(val_errors[:3]))
                self._save_thought(moment, thought, False, err_msg)
                self.world.note_error("step", err_msg[:180])
                self.world.set_last_step_attempt(fmt_time(moment))
                return False
            self._save_thought(moment, thought, True)
            data, text = await self._ask_master(data, prompt, text, tasks, moment, system_prompt=system_prompt)
            written, missed = self.apply(data, tasks, moment)
            total_written = sum(len(x) for x in written.values())
            if missed and total_written == 0:
                self.world.note_error("step", "%s 没写到此刻之后" % "、".join(missed))
                self.world.set_last_step_attempt(fmt_time(moment))
                logger.warning(
                    "[super_enhance/world] 补写没写全（%s），过 %d 秒再补", "、".join(missed), RETRY_SECONDS,
                )
                return False
            self.world.set_last_step(fmt_time(moment))
            self.world.clear_error("step")
            logger.info("[super_enhance/world] 补写写了 %d 段：%s%s", total_written, "；".join(
                "%s %s" % (self.label(bot) or bot, "、".join(
                    "%s–%s %s" % (str(x.get("start"))[11:16], str(x.get("end"))[11:16], clean_text(x.get("doing"), 16))
                    for x in rows))
                for bot, rows in written.items()),
                "（%s 剩余空档下轮继续）" % "、".join(missed) if missed else "")
            return True
        except Exception as e:
            logger.error("[super_enhance/world] 补写失败：%s", e)
            self.world.note_error("step", "%s: %s" % (type(e).__name__, e))
            self.world.set_last_step_attempt(fmt_time(moment))
            return False
        finally:
            self.running = False
            self._run_cache = {}
            self._last_window_seed = None
            self._last_live_ctx = None
            self._last_cand_map = None

    async def _ask_master(self, data: dict, prompt: str, text: str, tasks: dict, moment: datetime.datetime | None = None, *, system_prompt: str = SYSTEM_PROMPT):
        """补写填了 ask_master：登记当家待拍板事项（d 编号）并发出；若主人 90 秒内秒回则当场按主人准话重排，
        若主人暂未回话则持久挂起该 d 事项（绝不“超时就替主人自作主张干掉”！等主人几小时后在聊天回话时再由记录员解锁推进）。"""
        ask = data.get("ask_master")
        question = clean_text(ask.get("question"), 160) if isinstance(ask, dict) else ""
        if not question:
            return data, text
        bots = list(self.raw_log.enabled_bots())
        who = str(ask.get("who") or "").strip()
        if who not in bots:
            who = next((bot for bot in tasks if bot in bots), bots[0] if bots else "")
        planner = getattr(self, "planner", None)
        companion = getattr(self, "companionship", None)
        now = self._now().replace(tzinfo=None)
        starts = [x.get("start") for x in tasks.values() if isinstance(x.get("start"), datetime.datetime)]
        when = max(now, min(starts)) if starts else now
        is_resting = bool(companion is not None and companion.safe("resting", now, default=False))

        # decision（请当家主人拍板）：主人睡着时先存为 idea 等醒后再问；醒着时立即发消息并持久挂起 d 事项
        dec = companion.safe("open_decision", who, question, when, sent=False) if companion is not None else None
        dec_id = str((dec or {}).get("id") or "")
        if is_resting:
            logger.info("[super_enhance/world] 主人正在休息，待拍板事项 %s（%s）先挂起等醒后再问", dec_id, question[:50])
            return data, text

        targets = planner.targets_of(who) if planner is not None and planner.enabled and who else []
        if not targets:
            logger.info("[super_enhance/world] 补写登记待拍板事项 %s「%s」，主动对话未开，留待聊天中请示", dec_id, question[:50])
            return data, text
        umo = targets[0]
        since = time.time()
        if not await planner.ask_now(who, question, invitation_id=dec_id):
            return data, text
        if companion is not None and dec_id:
            companion.safe("mark_decision_asked", dec_id, now)
        asked = await self._wait_asked(umo, since)
        if asked is None:
            return data, text
        replies = await self._wait_replies(umo, float(asked.get("ts") or since))
        name = self.label(who) or who
        if not replies:
            logger.info("[super_enhance/world] %s 已向主人发出请示 %s「%s」，主人暂未回话，保持挂起等主人发话（绝不擅自替主人做主）",
                        name, dec_id, clean_text(asked.get("text"), 60))
            return data, text
        said = "；".join("%s「%s」" % (x.get("time") or "", clean_text(x.get("text"), 200)) for x in replies)
        note = ("%s %s 请示了当家主人：「%s」。主人当场拍板回了：%s。现在已获主人准许，请照主人定下的话把整份 JSON 重新写出来执行，ask_master 留空。"
                % (name, asked.get("time") or "", clean_text(asked.get("text"), 200), said))
        logger.info("[super_enhance/world] 主人当场拍板回话，照准话重写：%s", note[:200])
        retry = prompt + "\n\n你上一次的输出：\n" + str(text or "").strip() + "\n\n" + note
        try:
            again, again_thought = await llm.text_chat(
                self.context, retry, provider_id=str(self.cfg.get("llm_provider_id") or "").strip(),
                timeout=self.timeout, session_id="super_enhance_lifestep", system_prompt=system_prompt,
                attempts=2, log_label="补写照主人回话重写",
            )
        except Exception as e:
            logger.warning("[super_enhance/world] 补写照主人回话重写失败，保持请示挂起交由记录员接续：%s", e)
            return data, text
        self._run_cache = {}
        val_moment = moment or self._now().replace(tzinfo=None)
        fixed, again, _t, errs = await self._validate_with_retries(retry, again, again_thought, tasks, val_moment, "回话重写", master_replied=True, system_prompt=system_prompt)
        if fixed is None:
            logger.warning("[super_enhance/world] 补写照主人回话重写未通过结构化校验（%s），保持请示挂起交由记录员接续", "；".join(errs[:3]))
            return data, text
        if companion is not None and dec_id:
            for row in companion._data().get("decisions") or []:
                if row.get("id") == dec_id:
                    row["status"] = "resolved"
                    row["choice"] = said[:200]
                    row["resolved_at"] = fmt_time(self._now().replace(tzinfo=None))
                    companion._save()
                    break
        return fixed, again

    def _raw_after(self, umo: str, after_ts: float, role: str) -> list:
        """原始记录里这个会话 after_ts 之后的消息（只看那天和今天）。"""
        tz = self._now().tzinfo
        days = sorted({datetime.datetime.fromtimestamp(after_ts, tz).date().isoformat(),
                       datetime.datetime.fromtimestamp(time.time(), tz).date().isoformat()})
        out = []
        for day in days:
            for item in self.raw_log.read_day(day):
                if str(item.get("umo") or "") == str(umo) and item.get("role") == role and float(item.get("ts") or 0) > after_ts:
                    out.append(item)
        return sorted(out, key=lambda x: float(x.get("ts") or 0))

    async def _wait_asked(self, umo: str, since: float):
        """等她那句问话发出去（主动对话发出的，原始记录里标着 proactive）：返回那一条；等不到返回 None。"""
        deadline = since + ASK_SEND_SECONDS
        while time.time() < deadline:
            sent = [x for x in self._raw_after(umo, since, "assistant") if x.get("src") == "proactive"]
            if sent:
                return sent[0]
            await asyncio.sleep(ASK_POLL_SECONDS)
        return None

    async def _wait_replies(self, umo: str, asked_ts: float) -> list:
        """她问出去以后等主人回话 ASK_WAIT_SECONDS 秒；回了就再等 ASK_SETTLE_SECONDS 秒把连发的几句收齐。"""
        deadline = asked_ts + ASK_WAIT_SECONDS
        while True:
            replies = self._raw_after(umo, asked_ts, "user")
            now_ts = time.time()
            if replies:
                last = float(replies[-1].get("ts") or now_ts)
                if now_ts >= min(last + ASK_SETTLE_SECONDS, deadline + ASK_SETTLE_SECONDS):
                    return replies
            elif now_ts >= deadline:
                return []
            await asyncio.sleep(ASK_POLL_SECONDS)

    def _limit(self, start: datetime.datetime, end, sleepy: bool) -> datetime.datetime:
        """结束时间缺了、早于开始、或长得离谱时规整一下（睡觉那段放宽到 MAX_SLEEP_HOURS）。"""
        cap = datetime.timedelta(hours=MAX_SLEEP_HOURS if sleepy else MAX_SEGMENT_HOURS)
        if end is None or end <= start:
            return start + datetime.timedelta(minutes=45)
        return min(end, start + cap)

    @staticmethod
    def _normalize_proactive_fields(raw: dict, prev_entry: dict | None = None) -> dict:
        return _normalize_proactive_fields(raw, prev_entry=prev_entry)

    @staticmethod
    def _sanitize_proactive_nodes(entries: list) -> list:
        return _sanitize_proactive_nodes(entries)

    def _validate_with_symmetry(self, kept_by_bot: dict) -> list[str]:
        """校验两人合做（with 填了另一位 bot）时，对方在对应时段也必须有互写 with 的对应段。"""
        bots = set(self.raw_log.enabled_bots())
        errors = []
        batch_windows = {
            p_bot: (p_kept[0][2], p_kept[-1][3])
            for p_bot, (p_kept, _, _) in kept_by_bot.items()
            if p_kept
        }
        for bot, (bot_kept, _, _) in kept_by_bot.items():
            who_lbl = self.label(bot) or bot
            for idx, raw, seg_start, seg_end, _st_code in bot_kept:
                partners = [str(x) for x in (raw.get("with") or []) if str(x) in bots and str(x) != bot]
                for partner in partners:
                    p_lbl = self.label(partner) or partner
                    matched = False
                    if partner in kept_by_bot:
                        for _p_idx, p_raw, p_start, p_end, _ in kept_by_bot[partner][0]:
                            if p_start == seg_start and p_end == seg_end and bot in [str(x) for x in (p_raw.get("with") or [])]:
                                matched = True
                                break
                    if not matched:
                        win = batch_windows.get(partner)
                        if win is None or seg_end <= win[0] or seg_start >= win[1]:
                            for day in sorted({seg_start.date().isoformat(), seg_end.date().isoformat()}):
                                for item in self.world.entries(day, partner):
                                    if is_event(item) or bot not in [str(x) for x in (item.get("with") or [])]:
                                        continue
                                    es = parse_time(item.get("start"))
                                    ee = parse_time(item.get("end")) if item.get("end") else None
                                    if es is not None and es <= seg_start and (ee is None or ee >= seg_end):
                                        matched = True
                                        break
                                if matched:
                                    break
                    if not matched:
                        errors.append(
                            f"{who_lbl}第{idx}段（{fmt_time(seg_start)[11:16]}–{fmt_time(seg_end)[11:16]}）"
                            f"with 写了 {p_lbl}，但 {p_lbl} 名下该时段没有互写 with 的对应段；"
                            f"两人一起做的事须两边各写一段且起止一致，否则 with 勿填 {p_lbl}"
                        )
        return errors

    def _dedup_joint_proactive(self, written_by_bot: dict, now: datetime.datetime | None = None) -> None:
        _dedup_joint_proactive(written_by_bot, now=now, world=self.world)

    def validate_payload(self, data: dict, tasks: dict, now: datetime.datetime, *, master_replied: bool = False) -> list[str]:
        """纯结构化字段与库存一致性校验（不扫正文关键词、不做静默兜底修补）。返回报错列表，为空即合法。"""
        if not isinstance(data, dict):
            return ["顶层必须是 JSON 对象"]
        segments = data.get("segments")
        if not isinstance(segments, dict):
            return ["缺少顶层 segments 对象"]

        flat = now.replace(tzinfo=None)
        date_text = flat.date().isoformat()
        end_current = data.get("end_current") if isinstance(data.get("end_current"), dict) else {}
        day_seed = self._seed_for(date_text) if self.seed_log is not None else None
        win_seed = getattr(self, "_last_window_seed", None)
        seed = self._merge_origin_seeds(win_seed, day_seed)
        cand_map = getattr(self, "_last_cand_map", None)
        pools_data = self._pools()
        pool_meta: dict[str, dict] = {}
        for p_kind in ("twists", "night_twists", "together", "pastimes", "idles", "night_idles"):
            for p_row in pools_data.get(p_kind) or []:
                if isinstance(p_row, dict) and p_row.get("text"):
                    pool_meta[str(p_row["text"])] = p_row

        store = getattr(self, "things", None)
        try:
            shelf = self._shelf_at(self._things_moment(tasks, flat)) if store is not None else None
        except Exception:
            shelf = None
        inv_state = self._live_inventory_state(tasks, flat, shelf)
        seaside = self.home == "海边小镇"
        comp_ref = getattr(self, "companionship", None)
        chat_intent_ids = {
            str(x.get("id"))
            for x in (self.world.intentions() or [])
            if str(x.get("source") or "") == "chat"
            or "master" in (x.get("with") or [])
            or (comp_ref is not None and comp_ref.safe("is_protected_intent", str(x.get("id") or ""), default=False))
        }

        def _valid_place_struct(val) -> bool:
            if isinstance(val, dict):
                return bool(clean_text(val.get("area"), 20) and clean_text(val.get("at"), 20))
            if isinstance(val, str):
                parts = [p.strip() for p in val.split("·") if p.strip()]
                return len(parts) >= 2
            return False

        def _wood_ready_weather(eff: dict, is_new: bool) -> bool:
            w_cond = weathering.clean_conditions(eff.get("weather"))
            if w_cond.get("split") is False:
                return False
            if is_new:
                return (
                    w_cond.get("fuel_state") not in ("wet", "drying")
                    and w_cond.get("wet", 0.0) < 0.25
                    and w_cond.get("fuel_wet", 0.0) <= 0.25
                )
            return w_cond.get("fuel_state") == "ready" or (
                w_cond.get("wet") is not None and w_cond["wet"] < 0.25 and w_cond.get("fuel_wet", 0.0) <= 0.25
            )

        errors: list[str] = []
        batch_food_keys: set[str] = set()
        batch_measured_food_keys: set[str] = set()
        batch_item_keys: set[str] = set()
        batch_fuel_keys: set[str] = set()
        first_charcoal_at: datetime.datetime | None = None
        first_wood_at: datetime.datetime | None = None
        first_oil_at: datetime.datetime | None = None
        first_kerosene_at: datetime.datetime | None = None
        batch_any = {
            "new_item": False, "used_item": False, "new_food": False, "food_step": False,
            "water": False, "kerosene": False, "wood_stove": False,
        }

        # 先收集保留段内跨人共享的 new 临时代号及本批次新增燃料/调料/煤油标记（全部按结构化字段判）
        kept_by_bot: dict[str, tuple[list[tuple[int, dict, datetime.datetime, datetime.datetime, str]], datetime.datetime, datetime.datetime | None]] = {}
        for bot, info in tasks.items():
            who_lbl = self.label(bot) or bot
            raw_list = segments.get(bot)
            if not isinstance(raw_list, list) or not raw_list:
                errors.append(f"{who_lbl}（{bot}）：segments.{bot} 不能为空数组")
                continue
            start = info["start"]
            if info["mode"] == "open":
                cur_end = parse_time(norm_time(end_current.get(bot), date_text)) if end_current.get(bot) else None
                if cur_end is None and isinstance(raw_list[0], dict):
                    cur_end = parse_time(norm_time(raw_list[0].get("start"), date_text))
                if cur_end is None:
                    errors.append(f"{who_lbl}（{bot}）：手上未定结束时刻的段必须在 end_current.{bot} 给出结束时间")
                    start = flat + datetime.timedelta(minutes=5)
                else:
                    start = max(cur_end, flat + datetime.timedelta(minutes=5))
            dict_items = [x for x in raw_list if isinstance(x, dict)]
            dict_items.sort(key=lambda x: str(norm_time(x.get("start"), date_text) or ""))
            cursor = start
            horizon = max(start, flat) + datetime.timedelta(hours=HORIZON_HOURS)
            until = info.get("until") if info["mode"] == "fill" else None
            bot_kept = []
            for idx, raw in enumerate(dict_items[:MAX_SEGMENTS], 1):
                raw_state_str = str(raw.get("state") or "").strip().lower()
                st_code = STATE_WORDS.get(raw_state_str, "")
                sleep = st_code == "sleep"
                idle = st_code == "idle"
                r_start = parse_time(norm_time(raw.get("start"), date_text)) if raw.get("start") else None
                if r_start is not None and r_start > cursor:
                    cursor = r_start
                if not sleep and cursor >= horizon and cursor > flat:
                    break
                if until is not None and cursor >= until:
                    break
                r_end = parse_time(norm_time(raw.get("end"), date_text)) if raw.get("end") else None
                if r_end is not None and r_end <= cursor:
                    later = r_end + datetime.timedelta(days=1)
                    r_end = later if later > cursor else None
                _texts, minutes = parse_steps(raw.get("steps"))
                seg_end = (cursor + datetime.timedelta(minutes=sum(minutes))) if (minutes and st_code == "do") else r_end
                seg_end = self._limit(cursor, seg_end, sleep)
                if idle:
                    seg_end = min(seg_end, cursor + datetime.timedelta(minutes=MAX_IDLE_MINUTES))
                if until is not None:
                    seg_end = min(seg_end, until)
                bot_kept.append((idx, raw, cursor, seg_end, st_code))
                if raw.get("kerosene") is True:
                    batch_any["kerosene"] = True
                    if first_kerosene_at is None or cursor < first_kerosene_at:
                        first_kerosene_at = cursor
                if raw.get("water") is True:
                    batch_any["water"] = True
                if st_code == "do":
                    if raw.get("stove") == "wood":
                        batch_any["wood_stove"] = True
                    for eff in raw.get("things") or []:
                        if isinstance(eff, dict):
                            batch_any["food_step"] = True
                            if eff.get("new"):
                                batch_any["new_food"] = True
                                food_key = str(eff["new"]).strip()
                                batch_food_keys.add(food_key)
                                food_probe = {
                                    "cat": CAT_FOOD, "name": eff.get("name"), "kind": eff.get("kind"),
                                    "stages": [{"amount": eff.get("amount"), "left": eff.get("left")}],
                                }
                                if staple_of(food_probe) is not None:
                                    batch_measured_food_keys.add(food_key)
                                if str(eff.get("kind") or "") == "干货调料粮食" and store is not None and store._is_edible_oil({"cat": CAT_FOOD, "name": eff.get("name"), "kind": "干货调料粮食"}):
                                    if first_oil_at is None or cursor < first_oil_at:
                                        first_oil_at = cursor
                    for eff in raw.get("items") or []:
                        if isinstance(eff, dict):
                            if eff.get("new"):
                                batch_any["new_item"] = True
                                item_key = str(eff["new"]).strip()
                                batch_item_keys.add(item_key)
                                kd_str = clean_text(eff.get("kind"), 10)
                                purp_str = str(eff.get("purpose") or "").strip()
                                item_probe = {"cat": CAT_ITEM, "name": eff.get("name"), "kind": kd_str}
                                if is_charcoal(item_probe):
                                    batch_fuel_keys.add(item_key)
                                    if first_charcoal_at is None or cursor < first_charcoal_at:
                                        first_charcoal_at = cursor
                                if kd_str in weathering.STOVE_WOOD_KINDS or purp_str == "fuel":
                                    batch_fuel_keys.add(item_key)
                                    if _wood_ready_weather(eff, True) and (first_wood_at is None or seg_end < first_wood_at):
                                        first_wood_at = seg_end
                            else:
                                batch_any["used_item"] = True
                                tid_str = str(eff.get("id") or "").strip()
                                hit_item = shelf.find(tid_str) if (shelf is not None and tid_str) else None
                                is_wood_ref = tid_str in batch_fuel_keys or (hit_item is not None and weathering.fuel_item(hit_item) and not is_charcoal(hit_item))
                                if is_wood_ref and _wood_ready_weather(eff, False):
                                    if first_wood_at is None or seg_end < first_wood_at:
                                        first_wood_at = seg_end
                cursor = seg_end
            kept_by_bot[bot] = (bot_kept, cursor, until)

        # 逐人逐段做纯结构化校验
        for bot, (bot_kept, final_cursor, until) in kept_by_bot.items():
            who_lbl = self.label(bot) or bot
            if not bot_kept:
                errors.append(f"{who_lbl}（{bot}）：没有有效段落")
                continue
            if until is not None:
                if final_cursor <= tasks[bot]["start"]:
                    errors.append(f"{who_lbl}（{bot}）：补空档最后一段结束时刻（{fmt_time(final_cursor)}）必须晚于空档起点（{fmt_time(tasks[bot]['start'])}）")
            elif final_cursor <= flat:
                errors.append(f"{who_lbl}（{bot}）：最后一段结束时刻（{fmt_time(final_cursor)}）必须晚于当前时刻（{fmt_time(flat)}）")

            bot_has_new_item = False
            bot_has_used_item = False
            bot_has_new_food = False
            bot_has_food_step = False
            bot_used_cands: set[str] = set()
            bot_has_wood_stove = False
            bot_has_partner = False
            bot_resolved_origins: list[dict] = []

            for idx, raw, seg_start, seg_end, st_code in bot_kept:
                tag = f"{who_lbl}第{idx}段（{fmt_time(seg_start)[11:16]}）"
                if not str(raw.get("start") or "").strip() or parse_time(norm_time(raw.get("start"), date_text)) is None:
                    errors.append(f"{tag}：缺少合法的 start 时间")
                if not str(raw.get("end") or "").strip() or parse_time(norm_time(raw.get("end"), date_text)) is None:
                    errors.append(f"{tag}：缺少合法的 end 时间")
                if not str(raw.get("place") or "").strip():
                    errors.append(f"{tag}：place 不能为空")
                if not str(raw.get("doing") or "").strip():
                    errors.append(f"{tag}：doing 不能为空")
                if not str(raw.get("mood") or "").strip():
                    errors.append(f"{tag}：mood 不能为空（须写明心情及由头）")
                if not str(raw.get("outfit") or "").strip():
                    errors.append(f"{tag}：outfit 不能为空（须写明这段身上实际穿着/披裹什么）")

                if not st_code:
                    errors.append(f"{tag}：state 必须填 \"做事\"、\"闲着\" 或 \"睡觉\" 三选一（当前值={raw.get('state')!r}）")
                    continue
                idle, sleep = st_code == "idle", st_code == "sleep"

                _texts, minutes = parse_steps(raw.get("steps"))
                if st_code == "do":
                    if not _texts or len(minutes) != len(_texts) or any(m <= 0 for m in minutes):
                        errors.append(f"{tag}：state=\"做事\" 时 steps 必须为 1~5 步非空数组，每步含非空 text 与正整数 minutes")
                    if not str(raw.get("result") or "").strip():
                        errors.append(f"{tag}：state=\"做事\" 时 result 不能为空")
                else:
                    if raw.get("steps"):
                        errors.append(f"{tag}：state 为闲着或睡觉时 steps 必须填 []")
                    if raw.get("things") or raw.get("items"):
                        errors.append(f"{tag}：state 为闲着或睡觉时 things 和 items 必须填 []")

                req_bools = ("at_home", "meal", "send_msg", "lamp", "kerosene", "water") if seaside else ("at_home", "meal", "send_msg")
                for b_key in req_bools:
                    if not isinstance(raw.get(b_key), bool):
                        errors.append(f"{tag}：{b_key} 必须显式填布尔值 true 或 false（当前值={raw.get(b_key)!r}）")

                if sleep and raw.get("at_home") is False:
                    errors.append(f"{tag}：睡觉段 at_home 必须为 true")
                if st_code != "do" and raw.get("meal") is True:
                    errors.append(f"{tag}：非做事段 meal 必须为 false")
                if raw.get("with"):
                    bot_has_partner = True
                if seaside and raw.get("lamp") is True:
                    if sleep:
                        errors.append(f"{tag}：睡觉段 lamp 必须为 false")
                    elif not (inv_state.get("lamp_ok") or (first_kerosene_at is not None and first_kerosene_at <= seg_end)):
                        errors.append(f"{tag}：家里煤油已点完且未领新煤油，lamp 不能填 true")

                if raw.get("send_msg") is True:
                    m_at = str(raw.get("msg_at") or "").strip()
                    m_purp = str(raw.get("msg_purpose") or "").strip()
                    if m_at not in ("start", "end"):
                        errors.append(f"{tag}：send_msg=true 时 msg_at 必须填 \"start\" 或 \"end\"")
                    if not m_purp:
                        errors.append(f"{tag}：send_msg=true 时 msg_purpose 不能为空")
                    if sleep and m_at == "end":
                        errors.append(f"{tag}：睡觉段道晚安时 msg_at 必须填 \"start\"")
                elif raw.get("send_msg") is False:
                    if str(raw.get("msg_at") or "").strip() or str(raw.get("msg_purpose") or "").strip():
                        errors.append(f"{tag}：send_msg=false 时 msg_at 与 msg_purpose 必须填 \"\"")

                if "stove" not in raw or not isinstance(raw.get("stove"), str) or raw.get("stove") not in ("", "charcoal", "wood"):
                    errors.append(f"{tag}：stove 必须显式填 \"\"、\"charcoal\" 或 \"wood\" 三选一（当前值={raw.get('stove')!r}）")
                    stove_val = ""
                else:
                    stove_val = raw["stove"]
                    if not seaside and stove_val != "":
                        errors.append(f"{tag}：非海边住处 stove 必须填 \"\"")
                    elif stove_val != "" and (st_code != "do" or raw.get("at_home") is False or raw.get("meal") is True):
                        errors.append(f"{tag}：外出、吃饭、闲着或睡觉段没有生火，stove 必须填 \"\"")
                    elif stove_val == "charcoal" and inv_state["charcoal_g"] <= 0 and not (first_charcoal_at is not None and first_charcoal_at <= seg_end):
                        errors.append(f"{tag}：家里闷炭现余 0 克且在此之前未补炭入库，stove 严禁填 \"charcoal\"")
                    elif stove_val == "wood" and inv_state["wood_bundles"] <= 0 and not (first_wood_at is not None and first_wood_at <= seg_end):
                        errors.append(f"{tag}：家里干柴为 0 且在此之前未补柴入库，stove 严禁填 \"wood\"")
                    if stove_val == "wood":
                        bot_has_wood_stove = True

                raw_intent = str(raw.get("intent") or "").strip()
                raw_training = str(raw.get("training") or "").strip()
                intent_id = "" if idle else self.intent_id(raw_intent, bot)
                training_id = self.training_id(raw_training, bot)
                if raw_intent and not intent_id:
                    errors.append(f"{tag}：intent={raw_intent!r} 不是 {who_lbl} 当前有效的打算编号")
                if raw_training and not training_id:
                    errors.append(f"{tag}：training={raw_training!r} 不是当前有效的在练编号")

                raw_from = str(raw.get("from") or "").strip()
                orig = origin_of(raw_from, bot, seed, intent=intent_id, training=training_id, pools_data=pools_data, cand_map=cand_map)
                cand_key = raw_from.strip("[]【】()（）").upper()
                cand_entry = cand_map.get(cand_key) if cand_key in cand_map else None
                if orig["kind"] in ("", "unmatched"):
                    errors.append(
                        f"{tag}：from={raw_from!r} 无效（严禁留空或填栏目标题；必须填上方候选编号如 \"T1\"/\"G1\"/\"P1\"/\"I1\"/\"F1\"/\"A1\"，"
                        f"或枚举 \"routine\" / \"intent\" / \"promise\" / \"rule\" / \"training\" / \"continue:前段事\" / \"self\"）"
                    )
                elif cand_entry is not None and cand_entry.get("bot") not in ("", bot) and cand_entry.get("bot") not in [str(x) for x in (raw.get("with") or [])]:
                    errors.append(f"{tag}：候选编号 {raw_from!r} 属于他人候选，未同行时{who_lbl}不能冒用")
                elif orig["kind"] == "intent" and not intent_id:
                    errors.append(f"{tag}：from 填了 intent，但 intent 字段未填有效的打算编号")
                elif orig["kind"] == "training" and not training_id:
                    errors.append(f"{tag}：from 填了 training，但 training 字段未填有效的在练编号")
                elif cand_entry is not None and cand_key in bot_used_cands:
                    errors.append(f"{tag}：同一人在本轮补写里不能重复使用候选编号 {raw_from!r}（第二段请填 \"continue:前段事\"）")
                else:
                    if cand_entry is not None:
                        bot_used_cands.add(cand_key)
                    bot_resolved_origins.append(orig)

                if st_code != "do":
                    continue
                if "things" not in raw or not isinstance(raw.get("things"), list):
                    errors.append(f"{tag}：things 必须显式填数组（无变动填 []）")
                if "items" not in raw or not isinstance(raw.get("items"), list):
                    errors.append(f"{tag}：items 必须显式填数组（无变动填 []）")

                for t_i, eff in enumerate(raw.get("things") or [], 1):
                    if not isinstance(eff, dict):
                        errors.append(f"{tag} things[{t_i}]：必须是对象")
                        continue
                    bot_has_food_step = True
                    what = norm_step(eff.get("what"))
                    if eff.get("new"):
                        bot_has_new_food = True
                        nm = clean_text(eff.get("name"), 30)
                        rk = str(eff.get("kind") or "").strip()
                        if not nm:
                            errors.append(f"{tag} things[{t_i}]：new 食物缺少 name")
                        if rk not in KINDS:
                            errors.append(f"{tag} things[{t_i}]（{nm}）：kind={rk!r} 不在允许的食物大类列表中")
                        if what not in START_STEPS:
                            allowed_food_hint = "做好了/买回来/收到" if seaside else "做好了/买回来/收到/原来就有"
                            errors.append(f"{tag} things[{t_i}]（{nm}）：new 食物的 what 必须填 {allowed_food_hint}（当前={eff.get('what')!r}）")
                        elif seaside and what == "原来就有":
                            errors.append(f"{tag} things[{t_i}]（{nm}）：海边船屋食物与调料已在清单列明，严禁填 what=\"原来就有\"（须通过做好了/买回来/收到获取）")
                        if not _valid_place_struct(eff.get("place")):
                            errors.append(f"{tag} things[{t_i}]（{nm}）：place 格式不合法（须填 {{\"area\": ..., \"at\": ...}} 且均非空）")
                        if what == "做好了":
                            ck = str(eff.get("cook") or "").strip().lower()
                            if ck not in ("boil", "fry", "steam", "roast", "cold"):
                                errors.append(f"{tag} things[{t_i}]（{nm}）：what=\"做好了\" 时必须填 cook（boil/fry/steam/roast/cold 五选一）")
                            else:
                                has_oil_now = (inv_state["oil_ml"] is None or inv_state["oil_ml"] > 0) or (first_oil_at is not None and first_oil_at <= seg_end)
                                if ck == "fry" and not has_oil_now:
                                    errors.append(f"{tag} things[{t_i}]（{nm}）：家里食用油现余 0 毫升且在此之前未补油，cook 严禁填 \"fry\"")
                                if seaside and raw.get("at_home") is True and ck in ("boil", "fry", "steam", "roast") and stove_val not in ("charcoal", "wood"):
                                    errors.append(f"{tag} things[{t_i}]（{nm}）：在船屋做好热食（cook=\"{ck}\"）时，该段 stove 必须填 \"charcoal\" 或 \"wood\"，不能填 \"\"")
                    else:
                        tid = str(eff.get("id") or "").strip()
                        shelf_food = shelf.find(tid) if (shelf is not None and tid) else None
                        found = tid in batch_food_keys or (
                            (shelf_food is not None and is_ended(shelf_food) is None) if shelf is not None else bool(tid)
                        )
                        if not tid or not found:
                            errors.append(f"{tag} things[{t_i}]：id={tid!r} 在当前可用库存或本轮新建代号中不存在（或早已吃完用完）")
                        if what not in MID_STEPS + END_STEPS:
                            errors.append(f"{tag} things[{t_i}]（{tid}）：已有食物的 what={eff.get('what')!r} 不合法")
                        if what in ("用完了", "吃完了") and str(eff.get("left") or "").strip():
                            errors.append(f"{tag} things[{t_i}]（{tid}）：what=\"{what}\" 时 left 必须填空串 \"\"")
                        if what == "油封" and inv_state["oil_ml"] is not None and inv_state["oil_ml"] < 60 and not (first_oil_at is not None and first_oil_at <= seg_end):
                            errors.append(f"{tag} things[{t_i}]（{tid}）：家里食用油不足 60 毫升，不能做 \"油封\"")
                        if what == "炉烘" and stove_val != "wood":
                            errors.append(f"{tag} things[{t_i}]（{tid}）：做 what=\"炉烘\" 时该段 stove 必须填 \"wood\"")
                        if what == "热了一下" and seaside and raw.get("at_home") is True and stove_val not in ("charcoal", "wood"):
                            errors.append(f"{tag} things[{t_i}]（{tid}）：船屋回锅热食（what=\"热了一下\"）时该段 stove 必须填 \"charcoal\" 或 \"wood\"")
                    used_txt = str(eff.get("used") or "").strip()
                    if used_txt and not is_strict_qty(used_txt):
                        errors.append(f"{tag} things[{t_i}]：used={used_txt!r} 必须为纯数字+单位（如 \"15毫升\"、\"200克\"），严禁模糊中文或分数")
                    is_measured_food = (
                        (eff.get("new") and str(eff["new"]).strip() in batch_measured_food_keys)
                        or (not eff.get("new") and (
                            str(eff.get("id") or "").strip() in batch_measured_food_keys
                            or (shelf_food is not None and staple_of(shelf_food) is not None)
                        ))
                    )
                    if is_measured_food and what not in ("用完了", "吃完了", "丢了", "坏了扔了"):
                        for qk in ("amount", "left"):
                            qv = str(eff.get(qk) or "").strip()
                            if qv and not is_strict_qty(qv):
                                errors.append(f"{tag} things[{t_i}]：按量记食材 {qk}={qv!r} 必须为纯数字+单位（如 \"500克\"、\"15毫升\"），严禁模糊中文或分数")

                for i_i, eff in enumerate(raw.get("items") or [], 1):
                    if not isinstance(eff, dict):
                        errors.append(f"{tag} items[{i_i}]：必须是对象")
                        continue
                    what = norm_step(eff.get("what"), CAT_ITEM)
                    used_txt = str(eff.get("used") or "").strip()
                    if used_txt and not is_strict_qty(used_txt):
                        errors.append(f"{tag} items[{i_i}]：used={used_txt!r} 必须为纯数字+单位（如 \"0.25捆\"、\"200克\"），严禁模糊中文或分数")
                    if eff.get("new"):
                        bot_has_new_item = True
                        nm = clean_text(eff.get("name"), 30)
                        kd = clean_text(eff.get("kind"), 10)
                        purp = str(eff.get("purpose") or "").strip()
                        if not nm or not kd:
                            errors.append(f"{tag} items[{i_i}]：new 物品必须同时填非空 name 与 kind")
                        is_new_charcoal = is_charcoal({"cat": CAT_ITEM, "name": nm, "kind": kd})
                        is_new_wood = kd in weathering.STOVE_WOOD_KINDS or purp == "fuel"
                        if kd == "燃料" and not is_new_charcoal:
                            errors.append(f"{tag} items[{i_i}]（{nm}）：炉柴 kind 必须明确填 \"燃柴\" / \"薪柴\" / \"引火料\"（仅闷炭/木炭填 \"燃料\"），严禁用模糊的 \"燃料\" 建木柴")
                        if is_new_charcoal or is_new_wood:
                            amt_txt = str(eff.get("amount") or "").strip()
                            if not amt_txt or not is_strict_qty(amt_txt):
                                errors.append(f"{tag} items[{i_i}]（{nm}）：新入库燃料/燃柴必须填精确数字+单位的 amount（如 \"1捆\"、\"400克\"），严禁留空或写中文模糊量词")
                            left_txt = str(eff.get("left") or "").strip()
                            if left_txt and not is_strict_qty(left_txt):
                                errors.append(f"{tag} items[{i_i}]（{nm}）：新入库燃料 left={left_txt!r} 必须为纯数字+单位")
                        if what not in ITEM_START_STEPS:
                            allowed_item_hint = "开始做/做好了/买回来/收到/捡到" if seaside else "开始做/做好了/买回来/收到/捡到/原来就有"
                            errors.append(f"{tag} items[{i_i}]（{nm}）：new 物品的 what 必须填 {allowed_item_hint}（当前={eff.get('what')!r}）")
                        elif seaside and what == "原来就有":
                            errors.append(f"{tag} items[{i_i}]（{nm}）：海边船屋物品已在清单或固定设定中列明，严禁填 what=\"原来就有\"（须通过买回来/收到/捡到/开始做/做好了获取）")
                        elif seaside and what in ("买回来", "收到") and kd in (
                            "冬衣", "厚衣", "棉袄", "外套", "大衣", "夹克", "毛衣", "棉衣", "棉袍", "棉被", "厚被", "被褥", "铺盖",
                        ) and not (master_replied or (intent_id and intent_id in chat_intent_ids) or "master" in [str(x) for x in (raw.get("with") or [])]):
                            errors.append(
                                f"{tag} items[{i_i}]（{nm}）：置办过冬厚衣/棉被属于家里大额开销，未经当家主人发话拍板（该段未绑定主人确认的 intent 且未与 master 同行）"
                                f"严禁擅自买回入库！本段请只写去打听价钱行情，并在顶层 ask_master 填 kind=\"decision\" 发消息请主人拿主意"
                            )
                        if not _valid_place_struct(eff.get("place")):
                            errors.append(f"{tag} items[{i_i}]（{nm}）：place 格式不合法（须填 {{\"area\": ..., \"at\": ...}} 且均非空）")
                    else:
                        bot_has_used_item = True
                        tid = str(eff.get("id") or "").strip()
                        shelf_hit = shelf.find(tid) if (shelf is not None and tid) else None
                        found = tid in batch_item_keys or (
                            (shelf_hit is not None and str(shelf_hit.get("cat") or "") == CAT_ITEM and is_ended(shelf_hit) is None)
                            if shelf is not None else bool(tid)
                        )
                        if not tid or not found:
                            errors.append(f"{tag} items[{i_i}]：id={tid!r} 在当前可用物品清单或本轮新建代号中不存在（或早已用完丢弃）")
                        if what not in ITEM_MID_STEPS + ITEM_END_STEPS:
                            errors.append(f"{tag} items[{i_i}]（{tid}）：已有物品的 what={eff.get('what')!r} 不合法")
                        is_fuel = tid in batch_fuel_keys or (
                            shelf_hit is not None and (is_charcoal(shelf_hit) or weathering.fuel_item(shelf_hit))
                        )
                        if is_fuel:
                            if what in ITEM_END_STEPS and str(eff.get("left") or "").strip():
                                errors.append(f"{tag} items[{i_i}]（{tid}）：what=\"{what}\" 时 left 必须填空串 \"\"")
                            elif what not in ITEM_END_STEPS:
                                for qk in ("amount", "left"):
                                    qv = str(eff.get(qk) or "").strip()
                                    if qv and not is_strict_qty(qv):
                                        errors.append(f"{tag} items[{i_i}]（{tid}）：燃料 {qk}={qv!r} 必须为纯数字+单位（如 \"0.75捆\"、\"200克\"），严禁模糊中文或分数")

            # 按池子条目的结构化 link 核对是否漏写对应的段属性
            for orig in bot_resolved_origins:
                if orig["kind"] not in ("twist", "night_twist", "together", "pastime"):
                    continue
                shared_ok = orig["kind"] == "together" or bot_has_partner
                eff_wood_stove = bot_has_wood_stove or (shared_ok and batch_any["wood_stove"])

                p_row = pool_meta.get(orig["text"]) or {}
                p_link = str(p_row.get("link") or "")
                if p_link == "wood_stove" and seaside and not eff_wood_stove:
                    errors.append(f"{who_lbl}采用了烧柴炉插曲/消遣「{orig['text']}」，对应段必须填 \"stove\": \"wood\"")

        errors.extend(self._validate_with_symmetry(kept_by_bot))

        money_book = getattr(self, "money", None)
        if money_book is not None:
            norm_segs = {}
            for m_bot, (bot_kept, _, _) in kept_by_bot.items():
                norm_segs[m_bot] = [
                    dict(raw, start=fmt_time(seg_start), intent=self.intent_id(raw.get("intent"), m_bot))
                    for _idx, raw, seg_start, _seg_end, _st_code in bot_kept
                ]
            all_intent_ids = {str(x.get("id")) for x in (self.world.intentions() or []) if x.get("id")}
            errors.extend(validate_step_segments_money(
                money_book,
                self._things_moment(tasks, flat),
                self.world,
                norm_segs,
                seaside=seaside,
                label_fn=self.label,
                valid_intents=all_intent_ids,
            ))

        return errors

    def apply(self, data: dict, tasks: dict, now: datetime.datetime):
        with fact_batch(self):
            return self._apply(data, tasks, now)

    def _apply(self, data: dict, tasks: dict, now: datetime.datetime):
        """落库（不搞关键词兜底瞎猜，完全按模型已通过校验的结构化字段落盘），返回（每人写进去的段，没写到此刻之后的人）。"""
        flat = now.replace(tzinfo=None)
        date_text = flat.date().isoformat()
        self._apply_stop_results(data.get("stopped_results"), date_text)
        moves = self._intent_moves(data.get("intent_moves"), tasks, flat)
        segments = data.get("segments") if isinstance(data.get("segments"), dict) else {}
        end_current = data.get("end_current") if isinstance(data.get("end_current"), dict) else {}
        bots = list(self.raw_log.enabled_bots())
        written, missed = {}, []
        day_seed = self._seed_for(date_text) if self.seed_log is not None else None
        win_seed = getattr(self, "_last_window_seed", None)
        cand_map = getattr(self, "_last_cand_map", None)
        seed = self._merge_origin_seeds(win_seed, day_seed)
        pools_data = self._pools()
        store = getattr(self, "things", None)
        try:
            shelf = self._shelf_at(self._things_moment(tasks, flat)) if store is not None else None
        except Exception:
            logger.warning("[super_enhance/world] 补写算家里的吃的失败", exc_info=True)
            shelf = None
        master_cur = self.world.current("master", flat)
        companion = getattr(self, "companionship", None)
        master_sleeping = (master_cur is not None and is_sleep(master_cur)) or bool(
            companion is not None and companion.safe("resting", flat, default=False)
        )

        # 第一遍：先按每人 MAX_SEGMENTS / horizon / until 截断保留段，杜绝截断段提前预占 ID
        kept_plans: dict[str, tuple[list, datetime.datetime, datetime.datetime | None]] = {}
        for bot, info in tasks.items():
            start = info["start"]
            if info["mode"] == "open":
                cur = info["cur"]
                cur_start = parse_time(cur.get("start")) or flat
                end = parse_time(norm_time(end_current.get(bot), date_text)) if end_current.get(bot) else None
                if end is None:
                    raw = (segments.get(bot) or [None])[0]
                    if isinstance(raw, dict):
                        end = parse_time(norm_time(raw.get("start"), date_text))
                floor = flat + datetime.timedelta(minutes=5)
                if end is None or end < floor:
                    end = floor
                end = self._limit(cur_start, end, is_sleep(cur))
                self.world.update(
                    self.world.date_of(cur.get("id"), bot, flat) or cur_start.date().isoformat(),
                    cur.get("id"), end=fmt_time(end),
                )
                start = end
            items = [x for x in (segments.get(bot) or []) if isinstance(x, dict)]
            items.sort(key=lambda x: str(norm_time(x.get("start"), date_text) or ""))
            cursor = start
            horizon = max(start, flat) + datetime.timedelta(hours=HORIZON_HOURS)
            until = info.get("until") if info["mode"] == "fill" else None
            if len(items) > MAX_SEGMENTS:
                logger.warning("[super_enhance/world] 补写给%s写了 %d 段，一次只收前 %d 段，后面的下次再写",
                               self.label(bot) or bot, len(items), MAX_SEGMENTS)
            bot_plan = []
            for raw in items[:MAX_SEGMENTS]:
                state = state_of(raw)
                idle, sleep = state == "idle", state == "sleep"
                raw_start = parse_time(norm_time(raw.get("start"), date_text)) if raw.get("start") else None
                if raw_start is not None and raw_start > cursor:
                    cursor = raw_start
                if not sleep and cursor >= horizon and cursor > flat:
                    break  # 写得够远了，后面的下次再写
                if until is not None and cursor >= until:
                    break  # 空档补满了
                seg_end = parse_time(norm_time(raw.get("end"), date_text)) if raw.get("end") else None
                if seg_end is not None and seg_end <= cursor:
                    later = seg_end + datetime.timedelta(days=1)
                    seg_end = later if later > cursor else None
                _texts, minutes = parse_steps(raw.get("steps"))
                parsed_steps = [{"text": text, "minutes": value} for text, value in zip(_texts, minutes)] if minutes else list(_texts)
                if minutes and state == "do":
                    seg_end = cursor + datetime.timedelta(minutes=sum(minutes))
                seg_end = self._limit(cursor, seg_end, sleep)
                if idle:
                    seg_end = min(seg_end, cursor + datetime.timedelta(minutes=MAX_IDLE_MINUTES))
                if until is not None:
                    seg_end = min(seg_end, until)
                bot_plan.append((raw, cursor, seg_end, state, idle, sleep, _texts, parsed_steps))
                cursor = seg_end
            kept_plans[bot] = (bot_plan, cursor, until)

        # 第二遍：只为保留下来的段预占新物品 ID
        thing_keys: dict = {}
        item_keys: dict = {}
        if store is not None and shelf is not None:
            for bot, (bot_plan, _cursor, _until) in kept_plans.items():
                for raw, _seg_start, _seg_end, state, _idle, _sleep, _texts, _parsed_steps in bot_plan:
                    if state != "do":
                        continue
                    for effect in raw.get("things") or []:
                        if isinstance(effect, dict) and effect.get("new") and effect.get("name"):
                            what = norm_step(effect.get("what"))
                            if what not in START_STEPS:
                                continue
                            if what == "原来就有":
                                kind = norm_kind(effect.get("kind"))
                                if kind not in MEASURED_KINDS or had_before(shelf.values(), clean_text(effect["name"], 30)) is not None:
                                    continue
                            self._claim(thing_keys, str(effect["new"]).strip(), clean_text(effect["name"], 30), bot,
                                        store.reserve_id)
                    for effect in raw.get("items") or []:
                        if isinstance(effect, dict) and effect.get("new") and effect.get("name"):
                            if norm_step(effect.get("what"), CAT_ITEM) not in ITEM_START_STEPS:
                                continue
                            self._claim(item_keys, str(effect["new"]).strip(), clean_text(effect["name"], 30), bot,
                                        lambda: store.reserve_id(CAT_ITEM))

        # 第三遍：组装条目、规整主动消息与 step_no 夹紧、落库
        for bot, (bot_plan, final_cursor, until) in kept_plans.items():
            staged_entries = []
            for raw, seg_start, seg_end, state, idle, sleep, _texts, parsed_steps in bot_plan:
                raw_with = [str(x) for x in (raw.get("with") or [])]
                with_list = [x for x in raw_with if x in bots and x != bot]
                intent = "" if idle else self.intent_id(raw.get("intent"), bot)
                if intent in moves and not moves[intent]["done"]:
                    intent = ""
                training = self.training_id(raw.get("training"), bot)
                prev = staged_entries[-1] if staged_entries else self.world.latest_for(bot, before=fmt_time(seg_start))
                prev_with_master = bool(prev) and "master" in (prev.get("with") or []) and not prev.get("sleep")
                at_meal = bool(raw.get("meal")) and raw.get("at_home") is not False and not master_sleeping
                if not master_sleeping and not idle and not sleep and "master" in raw_with and (
                    self._intent_with_master(intent) or prev_with_master or at_meal
                ):
                    with_list.append("master")
                msg_copy = {
                    "send_msg": raw.get("send_msg"),
                    "msg_at": raw.get("msg_at"),
                    "msg_purpose": raw.get("msg_purpose"),
                    "doing": raw.get("doing"),
                    "result": raw.get("result"),
                    "state": raw.get("state"),
                    "sleep": sleep,
                    "idle": idle,
                    "at_home": raw.get("at_home"),
                    "meal": raw.get("meal"),
                }
                _normalize_proactive_fields(msg_copy, prev_entry=prev)
                if "master" in with_list:
                    allow_meal_call = bool(
                        at_meal and not prev_with_master and msg_copy.get("msg_at") == "start"
                    )
                    if not allow_meal_call:
                        msg_copy["send_msg"] = False
                        msg_copy["msg_at"] = ""
                        msg_copy["msg_purpose"] = ""
                things_list = (
                    self._thing_steps(raw.get("things"), thing_keys, shelf, bot)
                    + self._item_steps(raw.get("items"), item_keys, shelf, bot, seg_place=str(raw.get("place") or ""))
                ) if state == "do" else []
                money_list = normalize_step_money_list(raw.get("money")) if state == "do" else []
                for effect in things_list + money_list:
                    if not _texts:
                        effect.pop("step", None)
                    else:
                        raw_no = step_no(effect.get("step"))
                        if raw_no <= 0:
                            raw_str = str(effect.get("step") or "").strip()
                            effect["step"] = 1 if raw_str in ("0",) or raw_str.startswith("-") else len(_texts)
                        else:
                            effect["step"] = max(1, min(raw_no, len(_texts)))
                raw_stove = str(raw.get("stove") or "").strip().lower()
                stove_val = raw_stove if raw_stove in ("charcoal", "wood") and not sleep and not idle else ""
                entry = {
                    "who": bot,
                    "kind": "activity",
                    "start": fmt_time(seg_start),
                    "end": fmt_time(seg_end),
                    "place": raw.get("place"),
                    "doing": raw.get("doing"),
                    "steps": None if state != "do" or raw.get("steps") is None else parsed_steps,
                    "result": None if state != "do" else raw.get("result"),
                    "idle": idle,
                    "sleep": sleep,
                    "stove": stove_val,
                    "importance": raw.get("importance"),
                    "send_msg": msg_copy["send_msg"],
                    "msg_at": msg_copy["msg_at"],
                    "msg_purpose": msg_copy["msg_purpose"],
                    "at_home": raw.get("at_home"),
                    "meal": raw.get("meal"),
                    **({"lamp": True} if raw.get("lamp") in (True, "true") and not sleep else {}),
                    **({"kerosene": True} if raw.get("kerosene") in (True, "true") else {}),
                    **({"water": True} if raw.get("water") in (True, "true") and not sleep else {}),
                    "mood": str(raw.get("mood") or "").strip(),
                    "outfit": clean_text(raw.get("outfit") or "", 100),
                    "public": raw.get("public"),
                    "with": with_list,
                    "training": training,
                    "intent": intent,
                    "origin": origin_of(
                        raw.get("from"), bot, seed, intent=intent, training=training,
                        pools_data=pools_data, cand_map=cand_map,
                    ),
                    "things": things_list,
                    **({"money": money_list} if money_list else {}),
                    "seen_by": [bot] + with_list,
                    "source": "step",
                    "created": fmt_time(flat),
                }
                staged_entries.append(entry)

            _sanitize_proactive_nodes(staged_entries)
            rows = []
            cursor = tasks[bot]["start"] if not bot_plan else final_cursor
            last_ok_end = tasks[bot]["start"]
            for entry in staged_entries:
                seg_start = parse_time(entry["start"])
                record = self.world.add_entry(seg_start.date().isoformat(), entry)
                if record is None:
                    break
                rows.append(record)
                last_ok_end = parse_time(record.get("end")) or last_ok_end
            cursor = last_ok_end if rows else tasks[bot]["start"]
            if until is not None and rows and cursor < until <= cursor + datetime.timedelta(minutes=FILL_STRETCH_MINUTES):
                last = rows[-1]
                self.world.update(str(last.get("start"))[:10], last.get("id"), end=fmt_time(until))
                last["end"] = fmt_time(until)
                cursor = until
            written[bot] = rows
            if (until is None and cursor <= flat) or (until is not None and not rows):
                missed.append(self.label(bot) or bot)
        _dedup_joint_proactive(written, now=flat, world=self.world)
        self._apply_intent_moves(moves, written, flat)
        self._record_used_origins(date_text, written, pools_data, foiled=(win_seed or {}).get("foiled"))
        return written, missed

    def _live_inventory_state(self, tasks: dict | None, flat: datetime.datetime, shelf) -> dict:
        """提取实时燃料、油量、补给船未领与煤油灯余量，供纯结构化校验与环境感知使用。"""
        ctx = getattr(self, "_last_live_ctx", None)
        win_start = min((x["start"] for x in (tasks or {}).values() if isinstance(x, dict) and x.get("start")), default=None) or flat
        if not isinstance(ctx, dict) or not ctx.get("has_inventory"):
            ctx = build_live_context(
                start=win_start,
                end=max(win_start, flat) + datetime.timedelta(hours=HORIZON_HOURS),
                home=self.home,
                weather_of=self._weather_text_of(),
                world=self.world,
                store=getattr(self, "things", None),
                shelf=shelf,
            )
        lamp_ok = True
        if self.home == "海边小镇":
            try:
                ll = supply_mod.lamp_left(win_start, self.world, self._weather_text_of(), self.home)
                if ll is not None and ll <= 0.05:
                    lamp_ok = False
            except Exception:
                pass
        return {
            "charcoal_g": float(ctx.get("charcoal_g") or 0.0) if ctx.get("has_inventory") else 999.0,
            "wood_bundles": float(ctx.get("wood_bundles") or 0.0) if ctx.get("has_inventory") else 2.0,
            "oil_ml": float(ctx.get("oil_ml") or 0.0) if ctx.get("has_inventory") else None,
            "ship_unpicked": bool(ctx.get("ship_unpicked")),
            "lamp_ok": lamp_ok,
            "ctx": ctx,
        }

    def _seaside_living_nudges(self, tasks: dict | None, flat: datetime.datetime, shelf) -> str:
        """海边船居四大硬日常纯结构化感知（柴炉暖舱清灰 / 菜篮断鲜 / 淡水见底 / 煤油见底）：只看结构化字段与库存，零正文关键词扫描。"""
        if self.home != "海边小镇":
            return ""
        win_start = min((x["start"] for x in (tasks or {}).values() if isinstance(x, dict) and x.get("start")), default=None) or flat
        win_end = max(win_start, flat) + datetime.timedelta(hours=HORIZON_HOURS)
        inv = self._live_inventory_state(tasks, flat, shelf)
        ctx = inv["ctx"]
        groups = group_map(self._pools())
        nudges: list[str] = []

        # 1. 柴炉取暖与炉灰清理（纯读 entry["stove"] == "wood" 与台账草木灰状态）
        recent_wood = False
        wood_count_30h = 0
        cutoff_4h = win_start - datetime.timedelta(hours=4)
        cutoff_30h = win_start - datetime.timedelta(hours=30)
        cutoff_36h = win_start - datetime.timedelta(hours=36)
        fetched_water_36h = False

        for back in (2, 1, 0):
            d_str = (win_start.date() - datetime.timedelta(days=back)).isoformat()
            for e in self.world.entries(d_str):
                if real_stop(e):
                    continue
                en_t = parse_time(e.get("end")) or parse_time(e.get("start"))
                if en_t is None or en_t > win_start:
                    continue
                if en_t >= cutoff_36h:
                    orig = e.get("origin") if isinstance(e.get("origin"), dict) else {}
                    orig_txt = str(orig.get("text") or "")
                    if e.get("water") is True or (orig_txt and cool_key(orig_txt, groups) in ("取淡水", "存雨水")):
                        fetched_water_36h = True
                if en_t >= cutoff_30h and str(e.get("stove") or "") == "wood":
                    wood_count_30h += 1
                    if en_t >= cutoff_4h:
                        recent_wood = True

        if ctx.get("has_weather"):
            min_temp = float(ctx.get("min_temp") or 18.0)
            max_rain = float(ctx.get("max_rain") or 0.0)
            evening_or_night = win_start.hour >= 17 or win_end.hour >= 18 or win_start.hour < 6
            freezing_cold = min_temp <= 6.0
            chilly = freezing_cold or (min_temp <= 13.5) or (evening_or_night and min_temp <= 15.5) or (max_rain >= 0.3 and min_temp <= 16.0)
            th_win = weather_mod.cabin_temp_info(win_start, self.world, self._weather_text_of(), self.home)
            cabin_cool = th_win["mode"] != "burning" and th_win["delta"] < 2.5
            wood_ways = (
                "别老只跑坡上薪柴地，按海边地图换着路子弄柴：去坡上薪柴地拾枯枝或帮镇民修枝清道分柴、"
                "去船坞帮老船匠收场换干净实木边料与刨花，或去坡上柴料场与干燥棚买柴、以物换柴、帮工换柴"
            )
            if freezing_cold:
                if cabin_cool and not recent_wood:
                    if inv["wood_bundles"] > 0:
                        nudges.append(
                            "- 严寒生炉过冬：屋外已跌至约 %.0f℃，船舱此刻也降到了约 %.0f℃（%s）——早起舱冷、外出吹透海风回来或入夜前，顺着冷意抱干柴把铁桶柴炉生旺暖舱（生火/添柴段必填 \"stove\": \"wood\"），睡前拨小封火门压火留余温。"
                            % (min_temp, th_win["cabin"], th_win["stove_desc"])
                        )
                    else:
                        bed_word = "被褥" if ctx.get("has_thick_quilt") else "薄被和旧毛毯"
                        nudges.append(
                            "- 严寒断柴转折：屋外低至约 %.0f℃、船舱仅约 %.0f℃，冻得想点铁桶柴炉，去摸柴架却发现干柴烧空了——白天无大雨大雪就%s（items 写 new 燃柴入库）再生炉；夜深了就紧塞门缝裹紧%s，记着明早头一件事去弄柴。"
                            % (min_temp, th_win["cabin"], wood_ways, bed_word)
                        )
                elif 0 < inv["wood_bundles"] < 1.5 and 7 <= win_start.hour <= 16 and max_rain < 0.5:
                    nudges.append(
                        "- 入冬囤柴提醒：天冷早晚都要烧铁桶柴炉，家里干柴只剩 %.2g 捆不够烧两天了——趁白天没雨雪，顺手排一段%s（items 写 new 燃柴入库，别总去同一个地方）。"
                        % (inv["wood_bundles"], wood_ways)
                    )
                if not ctx.get("has_winter_gear") and 8 <= win_start.hour <= 16 and max_rain < 1.0:
                    nudges.append(
                        "- 单衣过冬与请示当家人：天已骤冷，船屋里还没有厚冬衣和厚棉被——未经主人点头不许擅自动家底买大件！白天可先去半淹商店街旧衣铺或集市布摊打听好厚棉衣与旧棉被的价钱，并在 ask_master 里填 kind=\"decision\" 报给主人拍板先买哪样。"
                    )
            elif chilly and cabin_cool and not recent_wood:
                if inv["wood_bundles"] > 0:
                    nudges.append(
                        "- 天凉生炉暖舱：这阵子船舱返潮发冷（最低约 %.0f℃），别写成全知提前预判，写成坐着觉得手脚发凉时起身抱干柴把铁桶柴炉生起来暖舱（该段必填 \"stove\": \"wood\"），睡前记得压火看炉。"
                        % min_temp
                    )
                else:
                    nudges.append(
                        "- 天凉缺柴转折：这阵子船舱发冷（约 %.0f℃），想生铁桶柴炉去摸干燥架却发现干柴烧空了——若在白天且无大雨就临时%s（items 写 new 燃柴入库），夜深了就多裹铺盖、记着明早先去弄柴。"
                        % (min_temp, wood_ways)
                    )
        if wood_count_30h >= 2 and not ctx.get("has_stove_ash"):
            nudges.append(
                "- 炉膛积灰细节：铁桶柴炉这两天烧了几回，炉膛积了冷草木灰且家里还没筛存干灰——再次生火前或白天顺手清一清炉膛（筛出的干草木灰可在 items 里写 new 入库，留着埋存鲜蛋生姜）。"
            )

        # 2. 正餐前菜篮断鲜感知（纯读食物 kind 枚举）
        store = getattr(self, "things", None)
        if store is not None and shelf is not None:
            fresh_kinds = {
                "生鱼虾水产", "生肉禽肉", "生内脏", "禽蛋类", "鲜水豆腐", "豆干千张",
                "娇嫩细叶菜", "耐储大叶菜", "茄果瓜豆菜", "菌菇类", "耐储根茎瓜菜",
            }
            cooked_kinds = {"熟菜炒菜", "熟汤羹粥", "熟肉卤味"}
            fresh_cnt = 0
            cooked_cnt = 0
            for item in store._local_items(shelf.values(), win_start):
                if is_ended(item) is not None or cat_of(item) != CAT_FOOD:
                    continue
                kd = str(item.get("kind") or "")
                if kd in fresh_kinds:
                    fresh_cnt += 1
                elif kd in cooked_kinds:
                    cooked_cnt += 1
            hours_spanned = {(win_start + datetime.timedelta(hours=h)).hour for h in range(int(HORIZON_HOURS) + 1)}
            near_meal_prep = bool(hours_spanned & {9, 10, 11, 15, 16, 17})
            if fresh_cnt == 0 and cooked_cnt < 2 and near_meal_prep:
                nudges.append(
                    "- 菜篮断鲜转折：眼下家里除了米面干货调料，已没有半点鲜鱼鲜菜鲜蛋了——别凭空下锅或顿顿干啃咸鱼，写成备饭前掀开菜篮/水桶发现没鲜菜了，临时挎上篮子先去集市挑鱼买菜、趁退潮去礁石滩撬牡蛎海螺、或拉蟹笼收货（必须在 things 里写 new 入库），回来再开火做饭（对外买菜必须排给外人看得见的亚托莉单独立段出门，place 写外头地点、at_home 填 false，不排丛雨独自去集市）。"
                )

        # 3. 36h 淡水补给与凉白开闭环（纯读 entry["water"] 布尔值与结构化池组）
        hours_day = {(win_start + datetime.timedelta(hours=h)).hour for h in range(int(HORIZON_HOURS) + 1)}
        if not fetched_water_36h and bool(hours_day & set(range(7, 19))):
            if float(ctx.get("max_rain") or 0.0) >= 0.3:
                nudges.append(
                    "- 淡水见底逢雨：近两天没补过淡水、水桶快见底了，正好外头落雨，顺手把桶盆摆到甲板接雨水、滤净烧开灌进水壶存成凉白开（该段必填 \"water\": true）。"
                )
            else:
                nudges.append(
                    "- 淡水见底转折：近两天没打过水也没接过雨水——写成洗漱或备饭舀水时发现淡水桶见底了，临时拎桶去蓄水塔、山上老井或半淹车站打水回来，顺手烧一壶凉白开备着喝（打水那段必填 \"water\": true；须排给外人看得见的亚托莉单独立段出门，place 写水塔/老井/车站、at_home 填 false，不排丛雨独自去公共取水点）。"
                )

        # 4. 夜里掌灯发现煤油快见底（纯读 lamp_left 数值）
        if win_start.hour >= 18 or win_end.hour >= 19 or win_start.hour < 5:
            try:
                ll = supply_mod.lamp_left(win_start, self.world, self._weather_text_of(), self.home)
                if ll is not None and 0.05 < ll <= 4.0:
                    nudges.append(
                        "- 油灯见底细节：天黑掌灯时晃了晃煤油灯，发现油壶快见底了（只够省着点一晚）——细活早点收手吹灯，靠炉火微光够吃饭说话就别一直点着灯。"
                    )
            except Exception:
                pass

        if not nudges:
            return ""
        return "船居实况与待办转折（按眼下真实现况触发，挑合时宜的自然融进段落动作与心情里，别写成机械汇报）：\n" + "\n".join(nudges)

    @staticmethod
    def _merge_origin_seeds(win_seed: dict | None, day_seed: dict | None) -> dict:
        if not isinstance(win_seed, dict):
            return day_seed if isinstance(day_seed, dict) else {}
        if not isinstance(day_seed, dict):
            return win_seed
        merged = dict(day_seed)
        for key in ("twists", "night_twists", "pastimes", "idles", "night_idles"):
            a = win_seed.get(key) if isinstance(win_seed.get(key), dict) else {}
            b = day_seed.get(key) if isinstance(day_seed.get(key), dict) else {}
            merged[key] = {
                bot: list(dict.fromkeys(list(a.get(bot) or []) + list(b.get(bot) or [])))
                for bot in set(a) | set(b)
            }
        merged["together"] = list(dict.fromkeys(list(win_seed.get("together") or []) + list(day_seed.get("together") or [])))
        merged["accident"] = win_seed.get("accident") or day_seed.get("accident") or ""
        return merged

    def _record_used_origins(
        self,
        default_date: str,
        written: dict,
        pools_data: dict | None = None,
        foiled: list | None = None,
    ) -> None:
        if self.seed_log is None or not written:
            return
        by_day: dict[str, dict[str, list]] = {}
        for bot, rows in (written or {}).items():
            for row in rows or []:
                if not isinstance(row, dict) or real_stop(row):
                    continue
                d_str = str(row.get("start") or "")[:10] or default_date
                by_day.setdefault(d_str, {}).setdefault(bot, []).append(row)
        for d_str, day_written in by_day.items():
            try:
                day_foiled = foiled if d_str == default_date else None
                self.seed_log.record_used(d_str, day_written, pools=pools_data, foiled=day_foiled)
            except Exception:
                logger.warning("[super_enhance/step] 记录当日已采用插曲失败", exc_info=True)
