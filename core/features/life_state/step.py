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
from .companionship import LIVING_NOTE, invitation_text
from .living_rules import LIFECYCLE_RULES
from .things import FactConflict
from .things import (CAT_FOOD, CAT_ITEM, END_STEPS, ITEM_END_STEPS, ITEM_MID_STEPS, ITEM_START_STEPS, KEEPS, KINDS, MEASURED_KINDS, MID_STEPS,
                     SPOTS, START_STEPS, _same_name, cat_of, checked_place, had_before, is_ended, norm_kind, norm_state, spots_for,
                     norm_step, split_keep, step_no, MAX_INSIDE)
from . import weather as weather_mod
from . import weathering
from . import supply as supply_mod
from .homewords import localize
from .menu import meal_slots, meals_text
from .variety import default_pools, twists_of, when_map, with_when
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

STEP_TEMPLATE = """现在是 {now}。下面这几位角色接下来做什么，由你来安排。

角色（key = 平台 id）：
{roster}

角色设定：
{personas}

{seed_text}

{home_block}

作息骨架：
{routine_block}

{context_block}

这次要你写的：
{task_block}

规则：

1. 只写「这次要你写的」里列出的人。每人从上面给的时刻开始，按时间顺序写几段，首尾相接、不重叠；最后一段的结束时间必须晚于现在（{now}）。一次最多写到 {horizon} 左右，不要把后面一整天都写完（睡觉那段除外，可以一直写到第二天起床）。
   **地方也要接上**：每人这次的第一段，从她上一段结束时人在哪、在干什么接下去（「这次要你写的」里写着那时候她在哪、结果怎样）。上一段结束时她还在路上、还在外面的，先把路走完——写一段走回去，或者这一段第一步就是走回去——到了地方才做下一件；不许上一段还在外面、下一段一开头就已经在屋里。段和段之间换到远一点的地方，中间也要写路上（在宿舍里换个屋子不用单写）。出门在外的事写到她到了地方为止，不许停在半路；快到 {horizon} 时正在路上的那段可以多写一点，把路走完。
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
12. 段里做的是「各自的打算」里她的某一条，intent 填那条的编号（例如 "i2"）；不是就留空。标着「现在就去」的，是她刚答应马上去做的：这次就从给你的时刻起先排它，别排在别的事后面；她人还不在做这件事的地方、世界线上也还没在往那边去的，先写一段走过去；可以分成几段（比如先去买、再回来做），每段 intent 都填这个编号。
   标着「挂了几天了」的打算，一直搁着没动：这次给她排一件事把它往前推或了结，照她的性子和眼下的实际情况挑，别每回都是同一种办法。比如：要等主人定日子的，去问主人一句什么时候方便；自己能做的，自己去做了；换个人一起、换个做法；想了想觉得算了，放下了。去问主人只写她去问了，主人怎么答不许替他编。再在 intent_moves 里用编号写这条后来怎样：做完了、放下了的写 "done": true，排它的那几段 intent 填这个编号；还没了结、往前推了一步的写 "done": false，text 写推进以后的打算（照原来的写法：做什么、跟谁、怎么说起的，再加一句这次推进了什么，例如「11-01 问过主人什么时候去，等他回话」），有了新的哪天、几点就写 date、window，排它的那几段 intent 留空。没标的打算不写。
13. 每段写 from：这段是照上面哪一样写的。照「可能碰上的小事」「手边的消遣」「闲着的样子」「今天会碰上一件意外」里的某一样写的，照抄那一样的原文；照打算、约定、规矩、在练的事、作息（起床、三餐、睡觉）写的，写「打算」「约定」「规矩」「在练」「作息」；接着前面已经做过的某件事往下写的，写「接着：」再加那件事；都不是、自己想的，写「自己想的」。
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
   - 做饭用料先用家里有的，先用快不行了的；新鲜的料（豆腐、菜、鱼肉、蛋、水果）家里没有，就在做饭之前排一段去买回来。米面油盐、调料干货、腌菜这些按量记的（清单里写着「还剩多少克 / 毫升」），用到的也要写：写进那盘菜的 from，再各写一步「用掉一部分」，used 写这次用了多少（「250 克」「3 克」「15 毫升」，照这道菜平常的用量）；标着「快用完了」的，排一段去买（买回来的写 amount，例如「5 斤」「500 毫升」）。家里一直备着、清单里还没记的常备调料（糖、醋这类），头一回用到就写一件 new，what 写「原来就有」，amount 写估计还有多少；别的清单里没有的料要先买。两人一起做的饭、一起吃的饭，things 写在其中一个人那段里。
   - 清单里写了口感的（叶子蔫了、饭发硬了、凉透了、串了味、熟过头了……），排做饭、吃东西时顺手照着处理：凉了的热一热，标着中间没熟透的先回锅煎透、煮透（写一步「热了一下」）再吃，发硬的饭炒成炒饭，蔫了的菜泡会儿冷水，熟过头的香蕉做成饼，串了味的拿去做红烧、麻辣这种重口的菜；标着有毒的（土豆发芽发绿）扔掉。放东西也照常理：剩菜盖好收着；气味冲的（葱蒜、卤味、鱼）跟豆腐、米饭、点心分开放或者都盖好；生肉包好，跟熟食分开放；炸的晾凉了再盖。
   - 段内是否开火（stove，三选一）：每段填 `"stove": ""`（没点火：备菜、洗碗、吃饭、聊天、外出、睡觉填 `""`；仅站在灶边商量菜单也没点火）、`"charcoal"`（船屋灶台小炭炉真点着开火做饭、烧水、热菜、煎蛋）或 `"wood"`（船屋柴炉真点着烧柴取暖、烘烤衣物/鱼干或烟熏；穗织住处统一填 `""`）。小炭炉烧闷炭/木炭由程序按顿自动从家里闷炭库存扣减，不用在 items 里手写扣炭；若闷炭已用光为 0，在弄回新炭之前小炭炉点不着火（`stove` 不能填 `"charcoal"`）。闷炭产出不会自动增加（光烧柴炉不处理只会烧成白灰），快见底时须安排：① 趁柴炉硬柴烧成红炭尚未化灰时专门动手拿火钳夹进陶罐盖严闷灭（from 填所烧干柴编号，items 写 new 入库），或 ② 去煮盐棚帮着挑海水添柴换一罐，或 ③ 去坡上柴料场/集市花零钱买木炭、赶上澡堂烧水日搭把手换半罐。点柴炉（`stove="wood"`）时记得消耗干柴（items 记用了哪捆柴），冷柴炉烘不干鱼也熏不熟东西。给还在睡或不在家的主人留饭时，必须盖好保温（熬粥、饭团盖在锅里温着，汤面只留汤头等主人醒了现下面条，绝不把煮好的汤面敞口摆在矮桌上泡几个钟头）。
   - kind 从这几类里挑：{kinds}；keep（怎么放）从这几样里挑：{keeps}；spot（放在哪，管冷热）从这几样里挑：{spots}。
19. **被打断的事后来怎么样了**：上面「被打断、还没交代后来怎么样的事」里每一件，在 stopped_results 里用编号写一句后来怎么样了：撂下不做了、交给谁接着做完了、等手上的事忙完回头接着做完了、或者赶紧几下做完了，挑合情理的写。只写这个人自己怎么处理这件事，不替别人定事实：别人那件做没做完、做出了什么，以世界线上别人那段写的为准（标着「做到……先放下了」的就是没做完），拿不准就写撂下了或者等回头再说。要和你这次排的段对得上：写了回头接着做，就把接着做的那段排上；写了交给对方，对方那段就是接着做这件事。没列出来的不写。
20. **家里的东西**（吃的以外）：上面「家里的东西」是此刻家里有的（编号、样子、在哪、在谁手上）。段里做了、用了、挪了、交给别人了、接着做了一截的东西，写进这段的 items（没动到就是空数组）：
   只是拿来读、翻看、摊开看一眼，之后原样放回去，地方、样子、数量都没变的，不算动到它，不写 items；别为了让校验通过硬编一条物品变化。船屋里隔音有限，主人睡觉时严禁在船舱里或头顶甲板上敲铁、凿木头、叮当拆修铁器。
   - 已有的写 id、what（挪地方 / 交给 / 用了 / 清点 / 做了一截 / 做好了 / 变了样 / 送人了 / 丢了 / 坏了扔了 / 用完了）、place、holder（交到谁手上）、state（这段做完它什么样，比如「织到两尺长」）；消耗品可顺带写 amount（清点数量）/ used（本次用量）/ left（剩余数量）。接着做没做完的那件（比如接着织那条围巾），就是清单里那一件，写「做了一截」或「做好了」，不要另建一件。清单里标着「已做好」的已经做完了，不要再排接着做它，也别给它写「做了一截」「做好了」；想再做一样的，就另起一件新的。
   - 新做出来的、买回来的、捡到的写 new（临时代号 x1、x2……，这一次里每件一个，两个人、几段之间也不重复：丛雨那段用了 x1，亚托莉那段的新东西就从 x2 起）、name（叫得出区别的名字，例如「兔子窗花」）、kind（它是什么，一个词：窗花、围巾、鱼漂……）、look（样子）、owner（归谁）、origin（哪来的，如「杂货铺」「废铁站」「海滩漂流物」）、from（用哪几件原料做的，没有留空）、what（开始做 / 做好了 / 买回来 / 收到 / 捡到）、amount（有明确数量的可填，如「1捆」「3张」）、place、state。清单里还没有、可前面早就有了的（比如前几天起了头的围巾），也写 new，what 写「原来就有」，state 写它现在的样子。
   - 上面写了字、画了东西的（给主人留的纸条、信、便笺、清单、贺卡、画）：look 里把上面写的原话一字不差地写出来，用「」括起来；画的写清楚画了什么。例如 look 写「黄纸条，铅笔写的：『主人，粥在灶上温着，亚托莉去码头了，中午前回来』」，别只写「一张写给主人的纸条」。这段的 steps 里写到她写纸条时也用这句原话。后来谁拿起来看、念给主人听，都照这句说，不能另编。
   - 封着送来的（寄来的信、没拆的包裹、捆好的箱子）：look 写外面看得到的（信封、邮戳、收信人、包裹多大多沉），里面是什么写进 inside——信写原话，用「」括起来；包裹、箱子写装了哪些东西。state 写「封着没拆」。这段的 doing、steps、result 里写到它时，照外面的样子写。拆开留到聊天里，那时候她们照 inside 说。
   - place 和第 18 条一样拆开写，例如 {{"area": "卧房", "at": "针线笸箩", "detail": ""}}、{{"area": "堂屋", "at": "书架", "detail": "第二格"}}。
   - 每一步也写 step（第几步，同第 18 条）。
   - 同一种东西有好几件的（清单里标着「长得像，认准编号」），照清单认准是哪一件。
21. **刚挨了主人训、主人刚生她的气**（上面「已经发生的」里有主人训她、骂她、生她气、跟她闹别扭这类事，后来也没说气消了、原谅她了）：她后面的安排要顺着这件事、她的性格和当时的情况来排，不能像没事人一样照常出门玩、去买东西、坐下来做手工。具体怎么样由你按实际情况定（可能闷在屋里、想办法跟主人道歉、做点事想讨他欢心、做事心不在焉……）；主人后来气消了、原谅她了，再照常排。
22. **丛雨怎么移动**：丛雨平时是飘着的，脚不沾地，赶路就飘过去，累了或者撒娇时才落地走两步。她那几段里换地方、出门、回来、凑到谁跟前，都照这个写。
23. **每段写 mood：这段她是什么心情、因为什么**，一句话，由头写在里头（比如「收音机拆开擦了半天还是不响，有点不服气」「主人还没回来，心里惦记着」「早上那锅鱼馊了，还有点闷」「被主人夸了，心里美滋滋的」）。聊天时她只看得到这一句，光写「有点不服气」「有点闷」，她不知道是冲着什么，就会往主人身上撒；所以冲着哪件事、哪样东西，就写明哪件事、哪样东西。心情跟着事走：从她上一段的心情接着往下写（上面已经发生的、手上的事后面写着当时的心情；今天头一段从起床时的底色起），这段碰上的事、主人说的话、过了多久都会让它变——闹别扭的气会慢慢消，高兴的劲头也会过去，别一整天都是同一个心情。心情不好要有由头，由头得是世界线或聊天记录里真有的事（可以就是这段里写出来的一件小事）；起床就带着、说不上由头的，写明「说不上为什么」，过一阵就淡了。睡觉那段写睡前的心情。
24. **给外人帮忙、干活不白帮**：她们给镇上的人（家里人以外）搭手干活、跑腿、修东西、搬东西、照看东西，人家总会给点什么：几个钱、一点吃的、一样用得上的东西，或者讲好的工钱、打捞费。写进那段的 steps 和 result（谁给的、给了什么）；拿回来的吃的、东西照第 18、20 条记进 things。顺手扶一把、递个东西这种小事不算。丛雨一般人看不见，人家不知道是她帮的，别排她一个人给外人干活；跟亚托莉一起干的，回报给亚托莉。
{world_notes}
{step_notes}
要问主人的（ask_master）：who 写谁来问，question 写一句话。已接下的小事只是做法没定时，kind 写 decision，先按合适做法把整份写完，程序可短等回话再调整。她自己想邀请主人一起做点什么、分享发现或一起待一会儿，也可以主动提出，kind 写 invite；只排自己的准备和其他生活，不写主人已经答应或共同做完。邀请会单独保留，主人晚些回复也能接上，不必让整段生活停着等。主人明确休息时考虑声音、活动位置、早餐和醒来后的相处，普通问题留到合适时候；既不替他醒来，也不要求她们整天守着。没合适的问题就留空，不为凑互动硬问。

注意：**不要输出 intentions，也不要写任何「接下来打算」**。上面「各自的打算」里如果有，是之前说好要做的，照着走就行；标着「挂了几天了」的，只在 intent_moves 里写它后来怎样（第 12 条）。

输出 JSON（end_current 只给上面说了「先定做完时刻」的人写；intent_moves 只写标着「挂了几天了」的打算，没有就留空）：

{{
  "end_current": {{"napcat": "{example_end}"}},
  "ask_master": {{"who": "", "question": "", "kind": "decision 或 invite"}},
  "stopped_results": {{}},
  "intent_moves": {{"i3": {{"done": false, "text": "推进以后的打算", "date": "", "window": ""}}}},
  "segments": {{
    "napcat": [{{"start": "{example_start}", "end": "{example_end}", "place": "", "doing": "一句话写这件事", "steps": [{{"text": "这一阵子她一直在干什么", "minutes": 5}}, {{"text": "第二步", "minutes": 15}}, {{"text": "收尾：用过的东西收好、归位", "minutes": 5}}], "result": "这件事的结果", "state": "做事", "stove": "", "send_msg": false, "msg_at": "", "msg_purpose": "", "at_home": true, "meal": false, "public": "", "with": [], "training": "", "intent": "", "from": "照上面哪一样写的", "mood": "这段她什么心情、因为什么", "things": [], "items": []}},
               {{"start": "{example_end}", "end": "{example_end2}", "place": "", "doing": "下一件事", "steps": [{{"text": "第一步", "minutes": 10}}, {{"text": "收尾", "minutes": 5}}], "result": "这件事的结果", "state": "做事", "stove": "", "send_msg": true, "msg_at": "start", "msg_purpose": "要出门去码头看一眼，跟主人说一声", "at_home": false, "meal": false, "public": "", "with": [], "training": "", "intent": "", "from": "照上面哪一样写的", "mood": "这段她什么心情、因为什么", "things": [], "items": []}}],
    "napcat2": []
  }}
}}
"""


# 溯源的种类（网页按这个显示）
ORIGIN_LABELS = {
    "twist": "插曲", "night_twist": "晚上的插曲", "together": "两人一起", "accident": "意外",
    "pastime": "消遣", "idle": "闲着的样子", "night_idle": "晚上闲着的样子",
    "intent": "打算", "promise": "约定", "rule": "规矩", "training": "在练", "routine": "作息",
    "continue": "接着前面", "self": "自己想的", "unmatched": "说是抽到的、对不上", "": "没写",
}
ORIGIN_WORDS = {"打算": "intent", "约定": "promise", "规矩": "rule", "约束": "rule", "在练": "training",
                "作息": "routine", "自己想的": "self"}


def _squash(text) -> str:
    """比对用：去掉空白和标点。"""
    return "".join(ch for ch in str(text or "") if ch.isalnum())


def origin_of(value, bot: str, seed, intent: str = "", training: str = "") -> dict:
    """补写说的 from 对照当天抽到的核一遍：{"kind": 种类, "text": 那一条的原文或补写写的话}。
    抽到的那几样按原文认（互相包含也算）；说了是抽到的却对不上，记成 unmatched，原话留着。"""
    text = clean_text(value, 80)
    if intent:
        return {"kind": "intent", "text": intent}
    if training:
        return {"kind": "training", "text": training}
    if not text:
        return {"kind": "", "text": ""}
    probe = _squash(text)
    seed = seed if isinstance(seed, dict) else {}
    pools = []
    for key, kind in (("twists", "twist"), ("night_twists", "night_twist"), ("pastimes", "pastime"),
                      ("idles", "idle"), ("night_idles", "night_idle")):
        value = seed.get(key)
        items = (value.get(bot) or []) if isinstance(value, dict) else (value or [])
        pools.extend((kind, str(x)) for x in items if isinstance(items, list))
    pools.extend(("together", str(x)) for x in seed.get("together") or [])
    if seed.get("accident"):
        pools.append(("accident", str(seed["accident"])))
    for kind, item in pools:
        squashed = _squash(item)
        if squashed and probe and (probe == squashed or (len(probe) >= 4 and (probe in squashed or squashed in probe))):
            return {"kind": kind, "text": item}
    for word, kind in ORIGIN_WORDS.items():
        if probe.startswith(word):
            return {"kind": kind, "text": text}
    if text.startswith("接着"):
        return {"kind": "continue", "text": text[2:].lstrip("：:").strip()}
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
    def _seed_text(seed: dict, labels: dict, night: bool = False) -> str:
        """当天的随机起点，按补写的口径写：一天要补十几回，小事和意外都只用一次。"""
        lines = ["今天的情况（系统随机给的，用来让每天不一样）："]
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
        # 只在特定时候才做得自然的插曲带上「什么时候」；老种子没存就按默认池子查
        when = seed.get("when") if isinstance(seed.get("when"), dict) else when_map(default_pools())
        per_bot = {bot: [with_when(x, when) for x in items] for bot, items in per_bot.items()}
        together = [with_when(x, when) for x in together]
        rows = ["%s：%s" % (labels.get(bot) or bot, "；".join(items)) if bot else "；".join(items)
                for bot, items in per_bot.items() if items and (not bot or bot in labels)]
        if together:
            rows.append("两人一起：%s" % "；".join(together))
        if rows:
            lines.append(
                "- %s可能碰上的小事：\n%s\n  她们自己说过的打算优先；打算之外的空档里才用，"
                "这次补写每人最多用一件，上面今天的条目里写过的别再写。" % (
                    "今晚" if night else "今天", "\n".join("  - " + row for row in rows))
            )
        pastimes = seed.get("pastimes") if isinstance(seed.get("pastimes"), dict) else {}
        rows = ["%s：%s" % (labels.get(bot) or bot, "；".join(items))
                for bot, items in pastimes.items() if items and bot in labels]
        if rows:
            lines.append("- 手边的消遣（今天在家闲下来时会拿起来做的）：\n%s" % "\n".join("  - " + row for row in rows))
        # 天黑以后用晚上那份；老种子没有晚上那份就还用白天的
        idles = seed.get("night_idles") if night and isinstance(seed.get("night_idles"), dict) else seed.get("idles")
        idles = idles if isinstance(idles, dict) else {}
        rows = ["%s：%s" % (labels.get(bot) or bot, "；".join(items))
                for bot, items in idles.items() if items and bot in labels]
        if rows:
            lines.append("- %s闲着的样子（歇着的时候人在哪、怎么待着）：\n%s" % (
                "今晚" if night else "今天", "\n".join("  - " + row for row in rows)))
        if seed.get("accident") and not night:
            lines.append(
                "- 今天会碰上一件意外：%s。上面今天的条目里还没有的话，挑个合适的时段写进去，当天能收场；"
                "已经写过就别再写。" % seed["accident"]
            )
        meals = meals_text(seed.get("meals"))
        if meals:
            lines.append(meals)
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
            for field in ("amount", "used", "left", "keep", "spot"):
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

    def _history_blocks(self, bots: list, flat: datetime.datetime) -> list[str]:
        if getattr(self, "things", None) is not None:
            self.things.view(flat)
        blocks = []
        for date_offset in (1, 0):
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

    def _context_block(self, bots: list, now: datetime.datetime, tasks: dict | None = None) -> str:
        parts = []
        flat = now.replace(tzinfo=None)
        companion = getattr(self, "companionship", None)
        if companion is not None:
            parts.append(LIVING_NOTE)
            for bot in bots:
                text = companion.safe("render", bot, flat, default="")
                if text:
                    parts.append("%s：\n%s" % (self.label(bot) or bot, text))
        parts.extend(self._history_blocks(bots, flat))
        proactive_text = self._today_proactive_block(bots, flat)
        if proactive_text:
            parts.append(proactive_text)
        # 今天真被打断、还没交代后来怎么样的事：补写在 stopped_results 里写一句（见第 19 条）
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
            parts.append("被打断、还没交代后来怎么样的事（最前面是编号）：\n" + "\n".join(stops))
        now_lines = []
        for bot in bots:
            cur = self.world.current(bot, flat)
            if cur is None:
                now_lines.append("- %s：这会儿没有记录" % (self.label(bot) or bot))
                continue
            pct = None if cur.get("idle") else progress_of(cur, flat)
            now_lines.append("- %s：%s %s%s（%s 起%s%s）%s" % (
                self.label(bot) or bot, cur.get("place") or "", cur.get("doing") or "",
                "（闲着）" if cur.get("idle") else "（睡着）" if is_sleep(cur) else event_text_at(cur, flat),
                timeutil.day_hm(cur.get("start")),
                "，预计 %s 做完" % timeutil.day_hm(cur.get("end")) if cur.get("end") else "，还没定什么时候做完",
                "，做了 %d%%" % pct if pct is not None else "",
                "",
            ))
        parts.append("她们此刻手上的事：\n" + "\n".join(now_lines))
        # 这次不写的人，她后面已经排好的段也给出来，免得把她拉进她那边没有的事里；
        # 从后面某个时刻才开始写的人（after），那个时刻之前排好的也给出来
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
            parts.append("已经排好、这次不改的：\n" + "\n".join(planned))
        constraints = self.world.constraints(active_only=True, scene=self.home)
        if constraints:
            parts.append(
                "生效中的约束：\n"
                + "\n".join(
                    "- %s（%s，%s 起）：%s" % (x.get("id"), x.get("who"), x.get("since"), x.get("text"))
                    for x in constraints
                )
            )
        intentions = [x for x in self.world.intentions() if str(x.get("who")) != "master"]
        if intentions:
            parts.append("各自的打算：\n" + "\n".join(self._intention_line(x, flat.date()) for x in intentions))
        plans = self.world.intentions("master")
        if plans:
            parts.append(
                "主人说过他要做的事（写着和她们一起、要她们做的，照着排进去；里头怎么做还没说定的，先填 ask_master 问主人）：\n"
                + "\n".join("- %s %s：%s%s" % (
                    str(x.get("date") or "")[5:] or "日子没说", x.get("window") or "", x.get("text"), why_text(x))
                    for x in plans)
            )
        promises = self._promise_lines(flat)
        if promises:
            parts.append("还没做的约定：\n" + "\n".join(promises))
        store = getattr(self, "things", None)
        if store is not None:
            try:
                # 按这次补写的起点现算：世界线里那时已经做完的段标的吃的算进去（提前几分钟补写时，正在收尾那段做的菜也算）
                moment = self._things_moment(tasks, flat)
                when = "此刻" if moment <= flat else "到 %s" % timeutil.day_hm(moment)
                shelf = self._shelf_at(moment)
                parts.append("家里的吃的（%s；每件前面是编号）：\n" % when + store.food_list(moment, shelf, self.label))
                parts.append("家里的东西（吃的以外，%s；每件前面是编号）：\n" % when + store.item_list(moment, shelf, self.label))
                if hasattr(store, "charcoal_summary"):
                    charcoal_line = store.charcoal_summary(moment, shelf)
                    if charcoal_line:
                        parts.append(charcoal_line)
                if hasattr(store, "cooking_staple_alert"):
                    staple_line = store.cooking_staple_alert(moment, shelf)
                    if staple_line:
                        parts.append(staple_line)
            except Exception:
                logger.warning("[super_enhance/world] 补写算家里的吃的失败", exc_info=True)
        # 主人的动向：他自己说的，加上她们写着和主人一起的段（陪他出门时主人跟着这些段走）
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
            # 最近一条常常没写地点（比如只记了他说的话），地点另取最近一条写了的
            where = next((x for x in reversed(trail) if str(x.get("place") or "").strip()), None)
            owner = str(master_latest.get("who") or "")
            parts.append(
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
            parts.append("主人最近的动向（只作参考，不许改写、不许替他编）：" + sleep_tail.lstrip("\n"))
        book = getattr(self, "trainings", None)
        if book is not None:
            try:
                for item in book.items():
                    if str(item.get("status")) != "open":
                        continue
                    parts.append(
                        "在练的事 #%s（%s）：%s；段里在练它就把 training 填成 #%s"
                        % (item.get("id"), item.get("bot"), item.get("what"), item.get("id"))
                    )
            except Exception:
                pass
        return "\n\n".join(parts)

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
        if real_stop(entry):
            stopped = clean_text(entry.get("stop_result") or "", 60)
            return out + ("（停下时：%s）。" % stopped if stopped else "。")
        return out + result_text(entry) + "。"

    async def build_prompt(self, tasks: dict, now: datetime.datetime) -> str:
        bots = list(self.raw_log.enabled_bots())
        roster = "\n".join(
            "- key = %s（%s）" % (bot, self.label(bot) or bot) for bot in bots
        )
        labels = {bot: (self.label(bot) or bot) for bot in bots}
        date_text = now.date().isoformat()
        seed = self._seed_for(date_text) if self.seed_log is not None else None
        # 这次从晚上开始写：随机的小事只给晚上那份，也不给意外
        first = min((x["start"] for x in tasks.values() if x.get("start")), default=None) or now
        night = first.hour >= NIGHT_FROM or first.hour < NIGHT_UNTIL
        seed_text = self._seed_text(seed, labels, night) if seed else ""
        try:
            seed_text += self._weather_block(min((x["start"] for x in tasks.values() if x.get("start")), default=None) or now, now)
        except Exception:
            logger.warning("[super_enhance/step] 天气时间线没拼上", exc_info=True)
        first = next(iter(tasks.values()))
        example_start = first["start"] or (now + datetime.timedelta(minutes=10))
        return localize(STEP_TEMPLATE.format(
            now=fmt_time(now),
            roster=roster,
            personas=await self._personas(bots),
            home_block=(
                "她们现在住的地方：\n"
                "- 她们住在【%s】。写出来的地点和活动都在%s这一带，具体去哪儿，从上面人设里这一带的地图里挑。\n"
                "- 人设里写的其它地方（比如从前待过的镇子）只是背景，人现在不在那儿，不要写成此刻发生的事。"
                % (self.home, self.home)
            ) + self._arrived_line(now),
            seed_text=seed_text,
            world_notes=self.world_notes,
            step_notes=self.step_notes,
            routine_block=self._routine_block(bots, now),
            context_block=self._context_block(bots, now, tasks),
            task_block=self._task_block(tasks),
            kinds=" / ".join(KINDS),
            keeps=" / ".join(KEEPS),
            spots=" / ".join(spots_for(self.home)),
            example_start=fmt_time(example_start),
            example_end=fmt_time(example_start + datetime.timedelta(minutes=25)),
            example_end2=fmt_time(example_start + datetime.timedelta(minutes=40)),
            horizon=fmt_time(max(min(x["start"] or now for x in tasks.values()), now) + datetime.timedelta(hours=HORIZON_HOURS)),
        ) + LIFECYCLE_RULES, self.home)


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
            if front is None:
                continue
            edge, last = front
            # 手动补写（force）：不看有没有缺口，也不推翻已排好的，直接从她排到的最后一段往后接
            if force or flat < edge <= reach - datetime.timedelta(minutes=JOIN_ROOM_MINUTES):
                tasks[bot] = {"mode": "after", "start": edge, "cur": last}
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

    async def run(self, now: datetime.datetime | None = None, force: bool = False) -> bool:
        # 世界线里的时间都是不带时区的本地时间，这里统一成一样的口径
        moment = (now or self._now()).replace(tzinfo=None)
        self._run_cache = {}
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
            prompt = await self.build_prompt(tasks, moment)
            text, thought = await llm.text_chat(
                self.context,
                prompt,
                provider_id=str(self.cfg.get("llm_provider_id") or "").strip(),
                timeout=self.timeout,
                session_id="super_enhance_lifestep",
                system_prompt=SYSTEM_PROMPT,
                attempts=2,
                log_label="补写",
            )
            # 等模型的这几分钟里网页可能改过种子、存货：落库时重新读
            self._run_cache = {}
            data = llm.extract_json(text)
            if not isinstance(data, dict):
                self._save_thought(moment, thought, False, "返回内容不是 JSON")
                self.world.note_error("step", "返回内容不是 JSON")
                self.world.set_last_step_attempt(fmt_time(moment))
                return False
            self._save_thought(moment, thought, True)
            data, text = await self._ask_master(data, prompt, text, tasks)
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

    async def _ask_master(self, data: dict, prompt: str, text: str, tasks: dict):
        """补写填了 ask_master：让她借主动对话问主人那一句，等主人回话；回了就把回话交给模型照着重写一遍。
        没回、没问出去、出错，都照用第一次写的（她自己拿主意）。返回 (data, 模型原文)。"""
        ask = data.get("ask_master")
        question = clean_text(ask.get("question"), 120) if isinstance(ask, dict) else ""
        if not question:
            return data, text
        bots = list(self.raw_log.enabled_bots())
        who = str(ask.get("who") or "").strip()
        if who not in bots:
            who = next((bot for bot in tasks if bot in bots), bots[0] if bots else "")
        planner = getattr(self, "planner", None)
        companion = getattr(self, "companionship", None)
        if companion is not None:
            now = self._now().replace(tzinfo=None)
            is_invite = ask.get("kind") == "invite" or (ask.get("kind") != "decision" and invitation_text(question))
            if is_invite:
                # 邀请不占住后台两分钟，也不把沉默当同意；缺可选记录时其余生活照常落库。
                starts = [x.get("start") for x in tasks.values() if isinstance(x.get("start"), datetime.datetime)]
                when = max(now, min(starts)) if starts else now
                idea = companion.safe("wish", who, question, when)
                if (idea and idea["bot"] == who and idea["history"][-1]["status"] == "idea" and when <= now
                        and not companion.safe("resting", now, default=False) and planner is not None):
                    if await planner.ask_now(who, question, invitation_id=idea["id"]):
                        companion.safe("attempted", idea["id"], now)
                return data, text
            if companion.safe("resting", now, default=False):
                return data, text
        targets = planner.targets_of(who) if planner is not None and planner.enabled and who else []
        if not targets:
            logger.info("[super_enhance/world] 补写想让%s问主人「%s」，可主动对话没开或没配会话，照第一次写的",
                        self.label(who) or who, question)
            return data, text
        umo = targets[0]
        since = time.time()
        if not await planner.ask_now(who, question):
            return data, text
        asked = await self._wait_asked(umo, since)
        if asked is None:
            logger.warning("[super_enhance/world] 补写让%s问主人「%s」，%d 秒里没见她问出去，照第一次写的",
                           self.label(who) or who, question, ASK_SEND_SECONDS)
            return data, text
        replies = await self._wait_replies(umo, float(asked.get("ts") or since))
        name = self.label(who) or who
        if not replies:
            logger.info("[super_enhance/world] %s %s 问了主人「%s」，%d 秒里主人没回，照第一次写的（她自己拿主意）",
                        name, asked.get("time") or "", clean_text(asked.get("text"), 60), ASK_WAIT_SECONDS)
            return data, text
        said = "；".join("%s「%s」" % (x.get("time") or "", clean_text(x.get("text"), 200)) for x in replies)
        note = ("%s %s 问了主人：「%s」。主人回了：%s。照主人说的把整份 JSON 重新写出来（跟你原来写的一样就照原样），ask_master 留空。"
                % (name, asked.get("time") or "", clean_text(asked.get("text"), 200), said))
        logger.info("[super_enhance/world] 补写问主人有回话，照着重写：%s", note[:200])
        retry = prompt + "\n\n你上一次的输出：\n" + str(text or "").strip() + "\n\n" + note
        try:
            again, _thought = await llm.text_chat(
                self.context, retry, provider_id=str(self.cfg.get("llm_provider_id") or "").strip(),
                timeout=self.timeout, session_id="super_enhance_lifestep", system_prompt=SYSTEM_PROMPT,
                attempts=2, log_label="补写照主人回话重写",
            )
        except Exception as e:
            logger.warning("[super_enhance/world] 补写照主人回话重写失败，照用第一次的：%s", e)
            return data, text
        self._run_cache = {}
        fixed = llm.extract_json(again)
        if not isinstance(fixed, dict) or not isinstance(fixed.get("segments"), dict):
            logger.warning("[super_enhance/world] 补写照主人回话重写的不是 JSON，照用第一次的")
            return data, text
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

    def _dedup_joint_proactive(self, written_by_bot: dict, now: datetime.datetime | None = None) -> None:
        _dedup_joint_proactive(written_by_bot, now=now, world=self.world)

    def apply(self, data: dict, tasks: dict, now: datetime.datetime):
        with fact_batch(self):
            return self._apply(data, tasks, now)

    def _apply(self, data: dict, tasks: dict, now: datetime.datetime):
        """校验并落库，返回（每人写进去的段，没写到此刻之后的人）。"""
        flat = now.replace(tzinfo=None)
        date_text = flat.date().isoformat()
        self._apply_stop_results(data.get("stopped_results"), date_text)
        moves = self._intent_moves(data.get("intent_moves"), tasks, flat)
        segments = data.get("segments") if isinstance(data.get("segments"), dict) else {}
        end_current = data.get("end_current") if isinstance(data.get("end_current"), dict) else {}
        bots = list(self.raw_log.enabled_bots())
        written, missed = {}, []
        seed = self._seed_for(date_text) if self.seed_log is not None else None
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
                for effect in things_list:
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
                    "mood": str(raw.get("mood") or "").strip(),
                    "public": raw.get("public"),
                    "with": with_list,
                    "training": training,
                    "intent": intent,
                    "origin": origin_of(raw.get("from"), bot, seed, intent=intent, training=training),
                    "things": things_list,
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
            if cursor <= flat or (until is not None and not rows):
                missed.append(self.label(bot) or bot)
        _dedup_joint_proactive(written, now=flat, world=self.world)
        self._apply_intent_moves(moves, written, flat)
        return written, missed
