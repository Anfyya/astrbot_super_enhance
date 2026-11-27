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
from .companionship import (DECISION_TOPICS, DECISION_TOPIC_LABELS, GLOBAL_DAILY_ASK_LIMIT,
                            format_decision_line, normalize_decision_topic, topic_ask_limit)
from .living_rules import STEP_LIFECYCLE_RULES
from .money import AUTONOMOUS_SELL_LIMIT, SEASIDE_PRICE_GUIDE, fmt_yuan, normalize_step_money_list, validate_step_segments_money
from .things import FactConflict
from .things import (CAT_FOOD, CAT_ITEM, END_STEPS, ITEM_END_STEPS, ITEM_MID_STEPS, ITEM_START_STEPS, KEEPS, KINDS, MEASURED_KINDS, MID_STEPS,
                     SPOTS, START_STEPS, _same_name, cat_of, checked_place, had_before, is_charcoal, is_ended, is_sealed, is_strict_qty, is_strict_weight_qty,
                     norm_kind, norm_state, parse_amount, spots_for, staple_of, norm_step, split_keep, step_no, MAX_INSIDE)
from . import weather as weather_mod
from . import weathering
from . import smell as smell_mod
from . import tide as tide_mod
from . import supply as supply_mod
from .homewords import localize
from .menu import meal_slots, meals_text
from .variety import PoolStore, big_set, build_live_context, check_accident_wx_at, check_item_wx_at, cool_key, default_pools, draw_window, group_map, init_day_seed, short_set, twists_of, when_map, with_when
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
# 补写在途因主人情绪变化主动放弃时的极短防忙等冷却（秒；小于 world_loop 30s 周期，下一拍即可按新情绪重排）
MOOD_ABORT_COOLDOWN_SECONDS = 15
# 连续因情绪漂移放弃达到该次数以上时，转入 RETRY_SECONDS 常规退避，防止极端场景下无限重跑
MOOD_ABORT_MAX_FAST_RETRIES = 2
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
2. **一段只写一件事，且长短严格按三档标签走**：doing 只写这件事是什么（一句话），result 写这件事最后的结果（修好了没、找着没、码了多少公斤柴这类，一句；不算一步、没有分钟；state 是「闲着」「睡觉」的段不写），不要把几件事用「然后」「又」「再」串在同一段里。steps 把这件事按先后分成 1–5 步，每步写 text（一句，这一阵子她一直在干什么，写成能持续这一步那么多分钟的事）和 minutes（这一步按常理要几分钟）；最后一步顺带收尾归位即可，做完就接着做下一件事。**这段做多久就是各步 minutes 加起来**，程序按它算结束时间，end 照着算好写上就行（洗碗十来分钟、吃饭二三十分钟、看书一两个小时……）；state 是「闲着」「睡觉」的段不写 steps，end 照常写。**三档时长硬区分（严禁自己瞎猜）**：
   - 标着「短事·5~10分钟」的：本来就是几分钟利索干完的小事，**只写 1~2 步、总计 5~15 分钟收工**（严禁把拿放归位硬凑半小时）；
   - 标着「大活·可干半天~一整天」的：这是正经体力重活或整天大工程（比如拉圆木+劈满棚下大柴垛、出海深潜打捞、全屋大扫除大晒洗、集中检修船屋、煮盐棚帮大半天工），**放开去干！单段至少 ≥45 分钟、可一直写到 1.5~3.5 小时，而且允许分多段干一整天**（中间到点照常做饭吃饭、喝水歇会儿，歇完/吃完下一段 `from` 填 `"continue:前段事"` 接着干透，直到把柴垛真正堆满或整件大活完工，绝不被零碎小插曲打断、也不受「一天只做一次」限制）；
   - 没标「短事」也没标「大活」的正常活动：须按常理扎实展开到 **≥15 分钟**（通常 20~60 分钟），不得敷衍成几分钟。上面今天已经做完的普通事，别再原样做一遍。
3. **每段写 state，三选一**：「做事」「闲着」「睡觉」。
   - 没事就写闲着：她手上没有打算、约定、规矩要做的事，也没什么想做的，就写一段闲着，state 填「闲着」：doing 照上面「闲着的样子」里她的写，steps 留空。闲着一段最长一小时，歇够了就接着写下一件事（也可以再歇一段）。
   - 拿起「手边的消遣」来做是做事：state 填「做事」，照常写 steps，result 写做到哪了（看到哪、织了多少）；做到一半去忙别的，回头可以接着做。
   - 睡觉 state 填「睡觉」（见第 5 条）。其余的段都填「做事」。
4. 每段写 at_home 和 meal：at_home 是这段她人在不在宿舍（屋里、院子、廊下、门口、后头小屋都算 true；出了宿舍去别处、走在路上都算 false）；meal 是这段是不是正在吃饭（吃早饭午饭晚饭、夜宵算 true；做饭、摆碗筷、洗碗都不算）。
5. 睡觉也是一段：到了她今天定的入睡时刻，就写一段睡觉，state 填「睡觉」，从入睡时刻一直写到第二天定的起床时刻；睡前那段要在入睡时刻结束。过了入睡时刻还醒着的（比如答应了陪主人熬夜），那件事做完就接着写睡觉，睡到起床时刻。睡觉这段的 place 三选一：「船屋·船舱地铺」「船屋·船舱吊床」「船屋·船舱砖炕」，doing 和 public 都要写明睡在哪、盖的什么被子或毯子（严格照家里现存被褥写，谁盖哪床不能撞车，绝不凭空多编被子，如 public 写「盖着薄被睡了」「裹着旧毛毯睡了」），让世界线和同伴一眼看清各自盖着什么；吊床只睡得下一个人，砖炕最多睡两个人（主人加一个），地铺能睡两个人。
6. 下面「还没做的约定」里有钟点的：到那个钟点她要已经在约定的地方、和约定的人在一起。要换地方的，在那之前写一段赶过去，留足路上的工夫；约定那件事从约定的钟点开始写。
7. **不许生成主人的条目，也不许替主人编他做了什么、说了什么、吃了什么、听了什么或怎么回应。** 不仅禁止替主人编台词，也**严禁在 `doing`/`steps`/`result`/`mood` 里把主人的任何动作、在场反应或互动结果写成既定事实**（比如「主人热乎乎吃上了三只汉堡」「新发现说给主人了」「跟主人一起摊开海图圈完位置」「挨着主人聊天搭话」——哪怕没写主人说话也不行！）。主人在哪只看「主人最近的动向」：他最后说在哪、之后没说离开，她们可以在同屋安静做自己的事；他说了出门、去上课、睡了这类，就照那个写，别写他在场。**凡是想找主人互动的事（叫主人开饭、把刚发现的旧铺面或趣事讲给主人听、拿海图或物件找主人商量），补写里只写到她这边做好准备这一步**（例如做好饭菜摆上桌给主人留出热份、自己吃自己的；或回船屋摊开旧海图先把发现的位置圈好记牢），并把真正开口讲给主人听、叫主人吃饭或问主人拿主意交给对应段的 `send_msg: true` 或 `ask_master`（到点触发实际对话），**严禁把「讲给主人听、一起商量完、主人吃上了」直接静音演进段里跳过实际对话**。**主人眼下在船屋里且没到你们明确约好钟点的出发时刻前，严禁补写擅自写 `at_home=false` 且 `with` 里带上 `"master"` 强行把主人带出门**（想约主人出门请先在家标 `send_msg: true` 开口喊他，或用 `ask_master` 问准点，或自己单独去办不写 `master`）。只有两种例外：① 约好了具体钟点的约定/打算（如明确写着 14:00 一起出海），到点才写成和主人一起出门；② 主人已经自己出门在外头了，之后同一趟里接着往下的段才写 `"master"`。**只要带主人一起出了门（`at_home=false` 且 `with` 含 `"master"`），回家的路途段也必须把 `"master"` 写进 `with` 一起带回来，严禁中途把主人丢在外面自己回家；带回到家就为止，到家之后（`at_home=true` 的段）她们各做各的事，不再把 `"master"` 写进 with。** 在家吃饭时，若主人清醒在家，她俩做好摆桌并给主人盛好或留好温着，标 `send_msg: true` 叫主人开饭（同屋一起开饭可在 `with` 里记上 `"master"`，但严禁替主人编吃没吃、吃多少、怎么聊——这些全留给实际对话）；主人睡着或外出不在家时 `with` 里一律不写 `"master"`；主人明确约好钟点一起办的事才在外出段 `with` 里写 `"master"`，除此之外不擅自把 `"master"` 写进 `with`。
   做给主人的、拿主人买回来的东西做的，那份是主人的：做好了端给主人或者给他留着，她们别再吃它、分它；主人吃没吃、剩下的给谁，后面聊天里定。
   「主人说过他要做的事」分两种：只是他自己去做的（上课、出门办事），那段时间他人不在，别把她们写成和他在一起；写着要她们做、或者和她们一起做的（比如晚上和亚托莉一起吃帝王蟹），就是交给她们的事，照着排进去，到时候写成和主人一起，with 写上 "master"——这是照主人说过的写，不算替他编。
8. 约束优先级最高：写着不许出门的，这段时间就得在屋里。照主人的意思守，别钻字眼上的空子：「不许捞她上来」就别改成给她送饭、递东西，「不许出门」就别叫人把东西送进来。主人罚她的事（扔下水、关在外头、罚站这类），别人也别背着主人替她松；想替她求情的，填 ask_master 问主人一句，主人点头才做。
9. public 是住在一起的人自然会知道的粗粒度信息（人在哪、大概在干哪类事），可以为空。doing 是细节。
   两人在一起的时段，两边的 start/end 和共同的事要一致，with 互相写上对方的 key；对方手上的事还没做完的，别写成和她在一起。对方不在「这次要你写的」里时，她那段时间做什么以「她们此刻手上的事」和「已经排好、这次不改的」为准，你改不了：只有她那边写的也是这件事、也在这个地方，才能写成和她一起；不然就别把她写进来。
   丛雨一般人看不见：她一个人在外面时，店家、摊主、路人都看不见她，不会跟她说话、招呼她、夸她、把东西递给她；买东西照常把钱搁在柜台上、自己拿。
10. **每段自主决定要不要开口找主人说话**：每段写 `send_msg`（true/false）、`msg_at`（`"start"` 段刚开始时开口，或 `"end"` 段刚做完时开口；不开口填 `""`）、`msg_purpose`（一句话自然写清楚这会儿想跟主人说什么事，不开口填 `""`），并可附可选字段 `msg_kind`（`"routine"` 日常节点/早晚安/叫吃饭/出门回家、`"casual"` 轻松闲聊/分享炫耀、`"repair"` 反思道歉/破冰沟通/主人外头受气时的暖心关心；不填由程序自动推断）。
   - **日常固定会开口跟主人说一声的 5 个节点**（就算主人就在船屋里，叫他吃饭、跟他搭话也必须标 `send_msg: true` 才会真的开口）：
     1) **早安**：早上睡醒起床后的第一段（`msg_at: "start"`，或跨夜睡觉那段的 `"end"`，二选一别重复），**不管主人在不在睡觉，两个人都各自标 `send_msg: true` 跟主人道早安**（`msg_purpose` 写明道早安）；
     2) **晚安**：晚上入睡那一段（`state: "睡觉"` **必须填 `msg_at: "start"`**，入睡前说晚安，严禁填 `"end"`）；
     3) **叫主人吃饭**：早饭、午饭、晚饭或夜宵做好开饭时（只要主人醒着），在做饭段的 `"end"` **或** 吃饭段的 `"start"` 挑一处标 `send_msg: true` 叫主人吃饭（二选一，严禁两段连着标）；
     4) **出门**：离开宿舍往外走那一段（`at_home` 从 true 变成 false，去码头、镇上、集市、海边、打捞船等，`msg_at: "start"`；只在院子廊下走动或门口几步路丢个东西不算正式出门）；
     5) **回来**：从外头回到宿舍时（回程段 `"end"` 或到家第一段 `"start"` 二选一），跟主人说一声回来了（买了或捡了什么顺嘴提一句）。
   - **想跟主人讲/分享/搭话时必须标 `send_msg: true`**：探路瞧见新露出的旧铺面或铁箱、捞到或捡到好玩的东西、做菜成了或翻车了、修好了某样物件、看到好看的海景天气、突然惦记主人想撒个娇或闲聊，想跟主人说就标 `send_msg: true`（若需要主人拍板定夺则填 `ask_master` 并把 `at` 填在回屋开口那一刻；同一件事已填 `ask_master` 的，对应段不再重复标 `send_msg: true`，以免同一分钟说两遍），**绝不把“讲给主人听”闷在段里演完却填 `send_msg: false`**；安安静静看书、洗碗、擦桌子、打盹这些不找主人的平淡日常填 `false`。不设死板条数上限，但先看一眼上面「今天已发过的主动消息」——同一件事别反复说，如果前面连发了好几条主人一直没回，后面除非固定节点就少发一点。
   - **两人同行防撞车**：两人一块做饭开饭（`with` 里有对方）时只由其中一个人标 `send_msg: true`（谁掌勺就谁去喊）；早安、晚安、出门、回来，或者两个人各自碰到不同趣事想分开聊的，**两个人都各自发，不受限**。
   - **主人睡觉时照常按实际情况标 `send_msg`**：主人在睡觉时，早上道早安、出门、回来、做好留着饭或真有事想跟他说时照常标 `send_msg: true`（发不发、怎么说由主动消息那边顾着他在休息自己拿捏）；没要紧事的平淡日常仍填 `false`。若两人都要出门不在家，把早饭盖好温着，再在 `items` 里写一张留给主人的字条压在堂屋矮桌上。
11. 段里是在练「在练的事」里的某件，training 填它的编号（例如 "#2"）；不是在练就留空，不要往里写别的。
12. 段里做的是「各自的打算」里她的某一条，intent 填那条的编号（例如 "i2"）；不是就留空。标着「现在就去」的，是她刚答应马上去做的：这次就从给你的时刻起先排它，别排在别的事后面；标着「就在今天」的（主人拍板定在今天办、或原定今天要做的事）：只要非狂风暴雨或深夜，**当天紧接着就排进 segments 推进（例如主人 10 点答应，11 点就能出发）**并填对应 intent 编号，严禁习惯性拖到明天！若主人明确说了几点干就严格按那个钟点排；若主人只答应了但没说具体几点，**严禁她俩自作主张定出发时间或拖到明天**，应在 `ask_master` 里直接顺嘴问主人一句具体时间（例如「主人，那 1 个小时之后出发可以嘛？」或「那吃完午饭两点出发成不成？」），等主人定准点后再按点出发。她人还不在做这件事的地方、世界线上也还没在往那边去的，先写一段走过去；可以分成几段（比如先去买、再回来做），每段 intent 都填这个编号。
   标着「挂了几天了」的打算，一直搁着没动：这次给她排一件事把它往前推或了结，照她的性子和眼下的实际情况挑，别每回都是同一种办法。比如：要等主人定日子的，去问主人一句什么时候方便；自己能做的，自己去做了；换个人一起、换个做法；想了想觉得算了，放下了。去问主人只写她去问了，主人怎么答不许替他编。再在 intent_moves 里用编号写这条后来怎样：做完了、放下了的写 "done": true，排它的那几段 intent 填这个编号；还没了结、往前推了一步的写 "done": false，text 写推进以后的打算（照原来的写法：做什么、跟谁、怎么说起的，再加一句这次推进了什么，例如「11-01 问过主人什么时候去，等他回话」），有了新的哪天、几点就写 date、window，排它的那几段 intent 留空。没标的打算不写。
13. **每段必填结构化溯源 `from`**（严禁留空，也严禁偷懒填「可能碰上的小事」「手边的消遣」「闲着的样子」这类栏目标题！只允许填以下值之一）：
   - 采用了上面「今天的情况」里的某条候选：直接填它前面的**短编号**（如 `"T1"`、`"G1"`、`"P1"`、`"I1"`、`"A1"`，或一字不差照抄那条完整原句）；
   - 做「各自的打算」：填 `"intent"`（且该段 `intent` 字段必填对应编号如 `"i282"`；若与同伴同行执行同伴名下的打算，也可填同伴那条 `i` 编号）；
   - 做「还没做的约定」：填 `"promise"`；
   - 守「生效中的约束」：填 `"rule"`；
   - 练「在练的事」：填 `"training"`（且该段 `training` 字段必填对应编号如 `"#2"`）；
   - 日常作息（起床洗漱、做三餐、吃三餐、饭后洗碗收桌、入睡）：填 `"routine"`；
   - 顺着前面某段接着往下做（用于真还没干完的事，比如走回船屋，或「大活」干到一半吃午饭/喝水歇口气后继续拉圆木、劈柴码满柴垛、继续检修打捞直到彻底完工；若一件普通小事上一段已经收妥归位，严禁用 `continue` 原地重复收尾！）：填 `"continue:前段做的事"`（如 `"continue:拉回大圆木劈柴码满棚下柴垛"`）；
   - 都不是、按眼下实况自己起意做的事：填 `"self"`（**严禁为了凑时间而跳过上方插曲池去编无意义空转家务**——例如把已码好的柴火搬下来数一遍再原样放回去、擦玻璃/擦饰品/倒水洗抹布硬凑半小时、反复清点早已清楚数量的铁盒或柜格；有空档必须优先采用上方插曲/双人/消遣候选！）。
14. 接着前面已经写过的事往下写时，前面写定的东西照原样沿用：写过的内容、放在哪、做出来的是什么，都和前面写的一致。前面留下的也要有交代：吃完饭剩下的饭菜，收尾那步按常理收好（盖好、放到该放的地方，写进 things）；做了一半的东西，后面合适的时候可以接着做，不打算做了就收起来。
15. **同一类普通小事一天里做过一次就不要再排**（比如今天已经做过点心、炖过汤，就别再排第二次；**标为「大活」的重头工程除外，允许上下午分多段接着干透一整天直至完工**）；主人对做出来的东西提的意见（「甜度再多一点」「汤有点淡」）**不等于叫她现在去做**——当天不重做，想改的留到以后（改配方、下次做的时候再试），现在照常排别的事。
16. **两个人一起做的事，细节只能有一个版本，而且要写到具体**：with 里写了对方、两人一起做的事（一起吃饭、一起做点心、一起出门、一起翻一本书）——吃的是什么菜、谁掌的勺、买了什么、书或报纸上讲了什么、路上碰见了谁、做出来的东西什么样，先在聊天记录和已经写好的段落里找，找到就照原样用；找不到就由你定一份具体的（不许写成「菜炒好了、东西买回来了」这种没内容的话），两边的 doing/steps/result 都用同一份。措辞可以随视角不同，事实、名字、数量必须完全一样。只有一个人做的事不强制写具体。
17. **亚托莉做饭的写法**：家常那几样练熟的照常做好；做没做过的新菜、复杂的大菜容易出岔子，平时做饭也容易失手（盐搁重了、火候偏了、形状歪了、奶油打过了头、化了、码得乱七八糟）。翻车之后吃的时候要写出难吃的样（皱眉、咽得慢、吃两口就放下），别写成吃得精光、像多好吃。翻车了丛雨也可能接手补救，或者干脆重做一道，把灶上收拾干净就翻篇。她嘴上怪火候怪锅，下次照旧认真学，别写成她怕进厨房。
18. **家里的吃的**：上面「家里的吃的」是此刻家里有的（编号、剩多少、放在哪、还能放到什么时候）。段里动到家里的吃的，写进这段的 things（没动到就是空数组）：
   - 用了、吃了家里有的：写 id（编号）、what（用掉一部分 / 用完了 / 吃了一部分 / 吃完了）、left（这段做完还剩多少，用完、吃完写空——用完、吃完必须单独写一步，what 填「用完了」或「吃完了」，别把「吃完了」塞进 left 里）；做进了这一次新做的菜，into 写那盘菜的临时代号。换了地方或放法、切开了：what 写「挪地方」或「切开了」，写上新的 place / keep（怎么放）/ spot（放在哪）/ state。剩菜回锅、凉了热一热：what 写「热了一下」。保鲜加工请直接填明确动作 what：轻腌（抹盐短保）/ 重盐腌 / 糖渍（熬果酱蜜渍）/ 醋泡 / 酱渍 / 油封 / 灰沙埋（草木灰或干沙埋蛋薯生姜）/ 晒上了（填 spot：太阳底下或屋外）/ 炉烘（借炉热烘烤，须该段 stove="wood"）/ 熏上了。扔掉：what 写「丢了」或「坏了扔了」。
   - 做出来的菜、买回来的、别人给的：写 new（临时代号 n1、n2……，这一次里每件一个，两个人、几段之间也不重复；编号由程序发）、name、kind、what（做好了 / 买回来 / 收到）、left、place、keep、spot；自己做出来的熟食（what 填「做好了」）加上 cook（五选一：`"boil"` 水煮熬粥炖汤 / `"fry"` 油煎爆炒油焖 / `"steam"` 清蒸 / `"roast"` 炭烤烘煨 / `"cold"` 凉拌冷食；买来的或生鲜不填）；做的菜 from 写用了哪几件（编号或这一次的临时代号），买来的 origin 写在哪买的。做菜用油、糖、醋、酱油、盐做 `油封`、`糖渍`、`醋泡`、`酱渍`、`轻腌/重盐腌` 时要看家里有没有够量库存——**家里食用油已用光时，`cook` 严禁填 `"fry"`（改选 boil/steam/roast/cold），也不许做 `"油封"`**。
   - place 拆开写，别连成一句：area 是在哪间屋、哪个地方（不在宿舍的写那个地方，例如「镇外梯田的农家灶房」）；at 是搁在那儿的什么上、什么里（矮桌、柜格、案上、灶上、水盆、书架、窗台……）；detail 是再细一点的哪一格、哪个容器里（桌角、第二格、竹筛里、锅里），说不上来就空着。area 和 at 都得有，光写一间屋不算。例如 {{"area": "堂屋", "at": "矮桌", "detail": "桌角"}}、{{"area": "灶房", "at": "案上", "detail": "竹筛里"}}、{{"area": "灶房", "at": "柜格", "detail": ""}}。拿在手上、揣在身上的：area 写谁，at 写手上、袖子里、菜篮里，例如 {{"area": "亚托莉", "at": "手上", "detail": ""}}。这是东西搁在哪，不是人在哪，别照抄这段的 place；挑的时候照常理（剩菜盖好搁灶上或柜格，点心、糖搁堂屋柜里或盒子里，米面调料在灶房柜格）；没挪地方的不写 place。
   - 每一步都写 step：这件事发生在这段分步里的第几步（从 1 数，例如第 2 步「下锅炒」用掉鸡蛋、做好蛋炒饭就写 2，第 3 步吃完就写 3）。这段要是中途被打断，只有做到了的那几步才算数。
   - 这一次里先做好、后面又吃掉的菜，以及平时在家里吃饭（`meal: true`）吃掉的桌上/锅里已有熟食，**吃饭那段必须在 `things` 里写一步**（两人同吃写在其中一人那段即可）：id 填对应编号或临时代号，what 写 `吃完了` 或 `吃了一部分`，left 写还剩多少（吃完填 `""`），严禁吃完却把 `things` 留空导致剩菜锅一直挂在清单上发馊。
   - **新菜写了 `from` 必须同步消耗原料**：凡新做的菜在 `from` 里填了用到的已有食材/剩菜编号（例如 `from: ["w480", "w433"]`），同段（或前序备菜段）`things` 里必须给 `from` 中每个编号写一条消耗动作（`用掉一部分` 或 `用完了`），严禁只填 `from` 却不扣减原料导致生鱼/剩菜继续留在清单上发臭。
   - 端给主人的（送到主人跟前、递到他手上的那一份）：那一步的 place 写 {{"area": "主人", "at": "跟前", "detail": ""}}（at 一律写「跟前」，递到手上、搁在手边也写跟前；新做好直接端上去的，做好那步就这么写；已有的写一步「挪地方」）。程序见到放在主人跟前的，一个小时后当主人吃完了，所以只端了一部分上去、剩下的还在原处的，端上去的那份别写在原来那件上；摆在桌上大家一起吃的、留给主人温着还没端过去的，area 照常写屋子。
   - 做饭用料先用家里有的，先用快不行了的；新鲜的料（豆腐、菜、鱼肉、蛋、水果）家里没有，就在做饭之前排一段去买回来。米面油盐、调料干货、腌菜这些按量记的（清单里写着「还剩多少克 / 毫升」），用到的也要写：写进那盘菜的 from，再各写一步「用掉一部分」，used 必须填阿拉伯数字+单位（如 `"250克"`、`"3克"`、`"15毫升"`，严禁写 `"少许"`、`"约五分之一"`、`"小半"` 等模糊词）；标着「快用完了」的，排一段去买（买回来的 amount 与 left 也必须填数字+单位，如 `"5斤"`、`"500毫升"`）。海边船屋的食材和常备调料已在清单里列明，严禁填「原来就有」；家里没有的调料食材须先排一段去集市或杂货铺买回来入库（非海边住处若头一回用到常备调料才可记「原来就有」）。两人一起做的饭、一起吃的饭，things 写在其中一个人那段里。
   - 清单里写了口感和保鲜的：凡落在「按时间算已经坏了、还没说扔的」里的（馊掉/长毛/发臭），以及已经反复回锅两三回、标着「熬咸了/又咸又浓/没什么味了/菜都黄烂了」的剩菜，**不要再回锅热去给人吃，更绝不可端到主人跟前，发现坏了或难吃到没法吃就该写「坏了扔了」扔掉，给主人吃就现做一碗新的（清理坏菜不必单开水段，早起、做饭或饭后收尾 `routine` 里顺手一并写 `what: "坏了扔了"` 倒掉洗锅即可）**；还没坏、只是凉了的剩菜才热一热（写一步「热了一下」），发硬的饭炒成炒饭，蔫了的菜泡会儿冷水，熟过头的香蕉做成饼，串了味的拿去做红烧、麻辣这种重口的菜；标着有毒的（土豆发芽发绿）扔掉。放东西也照常理：剩菜盖好收着，挪到矮桌/跟前就别再挂着「灶上温着」；气味冲的（葱蒜、卤味、鱼）跟豆腐、米饭、点心分开放或者都盖好；生肉包好，跟熟食分开放；炸的晾凉了再盖。
   - 段内是否开火（stove，三选一）：每段填 `"stove": ""`（没点火：备菜、洗碗、吃饭、聊天、外出、睡觉填 `""`；仅站在灶边商量菜单也没点火）、`"charcoal"`（船屋灶台小炭炉真点着开火做饭、烧水、热菜、煎蛋）或 `"wood"`（船屋柴炉真点着烧柴取暖、烘烤衣物/鱼干或烟熏；穗织住处统一填 `""`）。小炭炉烧闷炭/木炭由程序按顿自动从家里闷炭库存扣减，不用在 items 里手写扣炭；若闷炭已用光为 0，在弄回新炭之前小炭炉点不着火（`stove` 不能填 `"charcoal"`）。**闷炭金贵稀缺，取得全看运气与时机、绝不会烧柴炉就自动产炭**：光烧普通细枯枝（`kindling`）只会化成白灰；煮盐棚并非天天生火开锅，就算开锅那点锅底余炭也多半自留或早被镇上别人家挑水换光，跑一趟可能吃闭门羹/扑空，真碰巧赶上缺人手挑海水添柴累半天也至多换回一小捧到半小罐（约 `300~500 克`）；澡堂隔几天才烧一回且多杂枝，去搭把手未必有余炭、偶有剩也只换一小把（约 `150~250 克`）；柴料场买炭也可能遇上紧俏卖空；**自家柴炉只有专门烧粗硬木劈柴（清单标着「硬木粗柴·可夹闷炭」、`weather.fuel_size="split"`）、在火旺未化灰时动手拿火钳把红炭夹进陶罐闷灭才能产出零星闷炭（约 `100~200 克`，须同段 `stove="wood"`、steps 写明夹炭闷灭、items 写 `what="做好了"`、`new` 闷炭并在 `from` 填所烧硬柴编号；拿细枯枝或未标硬木柴填进 `from` 一律打回）**。因炭来之不易，留给灶台做饭烧水用，非万不得已不拿小炭炉干烧取暖。点柴炉（`stove="wood"`）时记得消耗干柴（items 记用了哪批柴及公斤数，硬木压小火暖舱约 `1.5 公斤/小时`，细枝引火约 `0.5~1 公斤/把`），冷柴炉烘不干鱼也熏不熟东西。给还在睡或不在家的主人留饭时，必须盖好保温（熬粥、饭团盖在锅里温着，汤面只留汤头等主人醒了现下面条，绝不把煮好的汤面敞口摆在矮桌上泡几个钟头）。
   - kind 从这几类里挑：{kinds}；keep（怎么放）从这几样里挑：{keeps}；spot（放在哪，管冷热）从这几样里挑：{spots}。
19. **被打断的事后来怎么样了**：上面「被打断、还没交代后来怎么样的事」里每一件，在 stopped_results 里用编号写一句后来怎么样了：撂下不做了、交给谁接着做完了、等手上的事忙完回头接着做完了、或者赶紧几下做完了，挑合情理的写。只写这个人自己怎么处理这件事，不替别人定事实：别人那件做没做完、做出了什么，以世界线上别人那段写的为准（标着「做到……先放下了」的就是没做完），拿不准就写撂下了或者等回头再说。要和你这次排的段对得上：写了回头接着做，就把接着做的那段排上；写了交给对方，对方那段就是接着做这件事。没列出来的不写。
20. **家里的东西**（吃的以外）：上面「家里的东西」是此刻家里有的（编号、样子、在哪、在谁手上）。段里做了、用了、挪了、交给别人了、接着做了一截的东西，写进这段的 items（没动到就是空数组）：
   只是拿来读、翻看、摊开看一眼，之后原样放回去，地方、样子、数量都没变的，不算动到它，不写 items；别为了让校验通过硬编一条物品变化。船屋里隔音有限，主人睡觉时严禁在船舱里或头顶甲板上敲铁、凿木头、叮当拆修铁器。
   - 已有的写 id、what（挪地方 / 交给 / 用了 / 清点 / 做了一截 / 做好了 / 变了样 / 卖了 / 换出去了 / 送人了 / 丢了 / 坏了扔了 / 用完了）、place、holder（交到谁手上）、owner（仅在交给别人或改变归属时填新拥有者，如 "master" / "napcat" / "napcat2" / "共用"）、state（这段做完它什么样，比如「织到两尺长」）；消耗品可顺带写 amount（清点数量）/ used（本次用量）/ left（剩余数量）。接着做没做完的那件（比如接着织那条围巾），就是清单里那一件，写「做了一截」或「做好了」，不要另建一件。清单里标着「已做好」的已经做完了，不要再排接着做它，也别给它写「做了一截」「做好了」；想再做一样的，就另起一件新的。**卖掉或寄卖成交的填「卖了」，以物易物换给别人的填「换出去了」，彻底送给外人带走的填「送人了」结单**（勿只填「交给」让它一直挂在家里）；**卖空后的脏空筐、看完的广告传单、用烂的旧报纸、彻底修不好的坏件等无用杂物**，日常收拾或大扫除时直接写「丢了」或「坏了扔了」清掉，不必样样供在桌上。
   - **新物件写了 `from` 必须同步消耗旧料**：凡用已有旧物拼装/改造成新成品（`new` 填了 `from` 如 `from: ["p118", "p119"]`），同段（或前序段）`items` 里必须对 `from` 中每个编号写一条消耗动作——整件合入成品或用光的填 `what: "用完了"`，只取走一部分材料的填 `what: "用了"`，严禁只建新成品却把原铁罐/碎料留在桌上重复占位。
   - 新做出来的、买回来的、捡到的写 new（临时代号 x1、x2……，这一次里每件一个，两个人、几段之间也不重复：丛雨那段用了 x1，亚托莉那段的新东西就从 x2 起）、name（叫得出区别的名字，例如「兔子窗花」）、kind（它是什么，一个词：窗花、围巾、鱼漂……；捡/劈来烧柴炉的木柴 kind 必须填 `"燃柴"` / `"薪柴"` / `"引火料"`，修船/手工木料填 `"漂流木料"` / `"木料"`，仅闷炭/木炭填 `"燃料"`，严禁把木柴写成模糊的 `"燃料"`；打捞或捡来准备过秤卖钱的废金属 kind 填 `"废铜"` / `"废铁"` / `"废金属"` 并可附 `"purpose": "scrap"`，铜顶针/怀表等旧物纪念品勿写废金属；现金收支一律记在 `money` 里，严禁把钱当成新 `items`（`kind="钱"`）新建）、look（样子）、owner（最终归谁所有/谁穿谁用：填 "master" / "napcat" / "napcat2" / "共用"；替主人或别人买回来、做给谁穿用的衣物或物品，owner 一律填实际拥有者或受赠人如 "master"，严禁只因是谁跑腿买回或暂时拿着就误填成跑腿人）、origin（哪来的，如「杂货铺」「废铁站」「海滩漂流物」）、from（用哪几件原料做的，没有留空）、what（开始做 / 做好了 / 买回来 / 收到 / 换回来 / 捡到）、amount（凡炉柴一律按重量写阿拉伯数字+`公斤`，严禁再写 `"捆"` 或模糊量词——参考真实体量：散步随手拾的细枯枝引火料 `2~5公斤`、船坞换一抱硬木边料或菜地帮剪 `12~25公斤`、帮清倒树或「囤柴大活」集中拉回劈码 `60~150公斤`；闷炭按 `"克"` 如 `"400克"`，严禁写中文数字或模糊词）、place、state。严禁凭空编造旧东西或写「原来就有」。
   - 已有燃柴/闷炭消耗或清点时，填的 `used` / `left` / `amount` 同样必须为阿拉伯数字+重量单位（炉柴用 `"公斤"` 如 `"1.5公斤"`、`"28.5公斤"`，闷炭用 `"克"` 如 `"200克"`；`"用完了"` 时 `left` 填 `""`），严禁写 `"捆"`、`"小半抱"`、`"约五分之一"` 等模糊说法。
   - 上面写了字、画了东西的（给主人留的纸条、信、便笺、清单、贺卡、画）：look 里把上面写的原话一字不差地写出来，用「」括起来；画的写清楚画了什么。例如 look 写「黄纸条，铅笔写的：『主人，粥在灶上温着，亚托莉去码头了，中午前回来』」，别只写「一张写给主人的纸条」。这段的 steps 里写到她写纸条时也用这句原话。后来谁拿起来看、念给主人听，都照这句说，不能另编。
   - 封着送来的或捞上来的封口战利品（寄来的信、没拆的包裹、捆好的箱子、上锁小铁箱、封口药瓶）：look 写外面看得到的（信封、邮戳、外观大小），新建时必须在 `weather` 写 `{{"sealed": true}}`（信/已知包裹还可把内容写进 inside；未知战利品 inside 可留空）；state 写「封着没拆」。这段的 doing、steps、result 里写到它时，照外面的样子写。等主人拍板同意拆看后，才把该 `id` 的 `weather` 改为 `{{"sealed": false}}`（或在聊天里当面拆开）。
   - place 和第 18 条一样拆开写，例如 {{"area": "卧房", "at": "针线笸箩", "detail": ""}}、{{"area": "堂屋", "at": "书架", "detail": "第二格"}}。
   - 每一步也写 step（第几步，同第 18 条）。
   - 同一种东西有好几件的（清单里标着「长得像，认准编号」），照清单认准是哪一件。
21. **主人情绪与相处余波**：若下方给出了「主人此刻情绪与相处实况」，后面的安排必须顺着那段白话说明、具体起因和她的性格来排，不能像没事人一样照常出门玩乐、画怪鱼或欢脱消遣；也**绝不写成罚站等发落、干坐着装死当哑巴**——该急刹车收手就立刻改掉欠妥细节，惹了具体祸事就复盘错在哪一步、先动手把窟窿补上再主动找主人把话说开（这类反思道歉、破冰沟通或主人外头受气后的暖心关心，应标 `send_msg: true`、`msg_kind: "repair"`）；若已松动回暖就格外仔细、拿不准的先问一声把改变实打实做出来；若主人是在外头受气或累着了，别瞎认错，安静体贴照应着就好。没有给情绪说明、或说明里只是翻篇后的轻余波时，正常过日子并在 `mood` 里自然带着一点余韵即可。
22. **丛雨怎么移动**：丛雨平时是飘着的，脚不沾地，赶路就飘过去，累了或者撒娇时才落地走两步。她那几段里换地方、出门、回来、凑到谁跟前，都照这个写。
23. **每段写 mood：这段她是什么心情、因为什么**，一句话，由头写在里头（比如「收音机拆开擦了半天还是不响，有点不服气」「主人还没回来，心里惦记着」「早上那锅鱼馊了，还有点闷」「被主人夸了，心里美滋滋的」）。聊天时她只看得到这一句，光写「有点不服气」「有点闷」，她不知道是冲着什么，就会往主人身上撒；所以冲着哪件事、哪样东西，就写明哪件事、哪样东西。心情跟着事走：从她上一段的心情接着往下写（上面已经发生的、手上的事后面写着当时的心情；今天头一段从起床时的底色起），这段碰上的事、主人说的话、过了多久都会让它变——闹别扭的气会慢慢消，高兴的劲头也会过去，别一整天都是同一个心情。心情不好要有由头，由头得是世界线或聊天记录里真有的事（可以就是这段里写出来的一件小事）；起床就带着、说不上由头的，写明「说不上为什么」，过一阵就淡了。睡觉那段写睡前的心情。
24. **给外人帮忙、干活不白帮**：她们给镇上的人（家里人以外）搭手干活、跑腿、修东西、搬东西、照看东西，人家总会给点什么：几个钱、一点吃的、一样用得上的东西，或者讲好的工钱、打捞费。写进那段的 steps 和 result（谁给的、给了什么）；拿回来的吃的、东西照第 18、20 条记进 things。顺手扶一把、递个东西这种小事不算。丛雨一般人看不见，人家不知道是她帮的，别排她一个人给外人干活；跟亚托莉一起干的，回报给亚托莉。
25. **当家权限硬锁（没有主人真转不动）**：
   - **主人是船长与当家人**，丛雨一般人看不见，严禁单独去镇上公共水井/蓄水塔/半淹车站打水、去集市买东西、去公共澡堂或跑腿办事（对外取水/采买/问价必须排给外人看得见的亚托莉单独立段出门，place 写外头地点、at_home 填 false），亚托莉只是打下手的机器人（算不清大账、不准擅自动大笔家底、不准私自接委托或擅进封闭险地）。
   - **A 类（亚托莉自主执行，不必请示）与 B 类（必须填 `ask_master` 等船长拍板）明确划分**：
     · **A 类自主**：港内熟悉的浅水开放点（旧街露门脸的铺子、半沉超市外廊、水下车站站台外沿、沉没图书馆窗外等，**不进封闭沉船或旧公寓内舱**）下水打捞废铜旧铁/小零碎、观赏潜水、退潮浅滩捡废；把家里**真有库存**的废铜旧铁（`items` 同步消耗真实 `id`）扛去收购站过秤卖钱，单笔 `<= {sell_limit}` 元直接自主结现（`reason="sell"`）。
     · **B 类必须 `ask_master`（`kind="decision"`）请船长拍板**：① 买过冬厚衣/棉袄、换厚棉被、修船屋大件设施；② 镇上外人来下打捞委托单；③ 进封闭沉船或旧公寓内舱深潜摸货；④ 出动港边小潜艇下海；⑤ 拆看捞上来的上锁铁盒或封口旧物（`weather.sealed=true`）；⑥ 寄卖 200~800 元完好遗物或单笔卖货超过 `{sell_limit}` 元。若上方「各自的打算 / 主人说过他要做的事」里**还没有**主人明确准话，最多只写打听行情、记下来客口信或把封口旧物搁在 `船舱·矮桌`（`weather: {{"sealed": true}}`），**在主人回话拍板之前，本轮及后续所有段严禁擅自买定大件、接单、进封闭沉船、开小潜艇或撬开封口旧物！** 主人批准生成 `intent` 后可按打算分步执行（`from: "intent"` 且 `intent` 填 `i` 编号，或与 `master` 同行 `with: ["master"]`），不必反复请示。
   - **伙食分「凑合糊口」与「主人在家/点菜开火」**：若主人没点菜、也没约好这顿一起吃，她俩自己只熬碗白粥配咸菜、烤块番薯或热口剩菜简单对付（省柴省炭省油盐，不做一荤一素一汤的大菜）；快到晚饭点想做上方参考菜单里的正经热菜锅物时，先填 `ask_master`（`kind: "decision"`, `topic: "meal"`）问主人今晚想吃哪样、回不回来吃。
   - **禁止纯日报式 `send_msg`**：严禁干完杂活发“废铁搬完啦跟主人汇报一声”“配给领回来啦通知一声”这种不需要主人参与的事后工作简报；需要主人拍板定夺的事（出船、接单、拆封箱、买大件、晚饭吃什么等）走 `ask_master`（事前请主人拿主意 `decision`）；早晚安、出门/回家、叫主人吃饭，以及真想讲给主人的新发现或闲聊撒娇，按规则 10 标 `send_msg: true`。
26. **船屋用钱、出门带钱与赚钱规矩（人民币，全写进段内 `money` 数组）**：
   - 分四处保管：`master`（主人钱包，大头由主人自己拿着；未经主人聊天里发话或同行付现，补写里**严禁**从 `master` 扣钱或拨钱）、`tin_box`（船舱矮桌抽屉里 `p157` 公用铁盒）、`napcat2`（亚托莉口袋）、`napcat`（丛雨零花；丛雨外人看不见，不能独自在镇上花钱或收钱）。
   - **人在外面动不了屋里的铁盒**：去集市/杂货铺/旧衣铺买东西时，若 `at_home=false` 则**严禁**填 `account="tin_box"` 隔空付钱！要么在出门前的船屋段先做一笔 `transfer(from_account="tin_box", to_account="napcat2")` 取现揣身上，要么只花 `napcat2` 口袋里本就揣着的零钱；回到船屋后再把找零/多余现金放回 `tin_box` 或上交 `master`。手头钱不够时按实际少买一点、回船屋拿钱、以物易物，或向熟摊主记赊账（`debt_add`，direction=`"payable"`）。
   - **买物与卖物必须真对应账实**：海边买回新食物/新物件（`what="买回来"`）时，同段 `money` 必须填对应的 `spend` 或 `debt_add`（或在 `things/items` 里真消耗已有实物以物易物），**严禁白拿商家东西，也不要再把现金当成新 `items`（`kind="钱"`）新建**。在海边赚钱（`earn`）必须填 `reason`：`work`（零工/修小物件 `<=50` 元）、`salvage`（打捞单收钱；超过 50 元须先有主人确认的 `intent` 打算，且单次 `<=250` 元）、`sell`（变卖收购站/集市：单笔自主 `<= {sell_limit}` 元，超过须有主人 `intent` 且上限 `300` 元，必须在同段 `things/items` 里消耗真实库存编号 `id`）、`consign_payout`（寄卖回款改用 `debt_settle` 核销待收 `debt_id`）。单笔进账超过 50 元回船屋后习惯交到主人手里或放入公用铁盒；手头有 `payable` 赊欠时进账了要惦记还掉（`debt_settle`）。
{money_guide}
{world_notes}
{step_notes}
要问主人的（ask_master，必填拿主意入口）：who 写谁来问（`"napcat"` 或 `"napcat2"`），question 写发给主人的那句自然口语问话（**凡涉及带船长出船/打捞接单/卖货/出门玩而主人未定具体时刻的，直接抛一个当天具体时间供主人拍板，例如「主人，那 1 个小时之后出发可以嘛？」或「那两点出船成不成？」，严禁自作主张定出发点或习惯性推到模糊的「明天」**），kind 写 `"decision"`，**`topic` 必填六选一枚举**：`"meal"`（晚饭吃什么/回不回来吃）、`"winter_gear"`（冬衣/棉被行情与先买哪样）、`"salvage_order"`（外人打捞委托/大宗超 {sell_limit} 元变卖或寄卖遗物）、`"sealed_loot"`（拆看封口旧物/上锁铁盒）、`"dive_outing"`（出动小潜艇/约船长出船深潜）、`"other"`（其他待拍板事项），**`at` 必填触发时刻 `"YYYY-MM-DD HH:MM"`**（写清这件事在世界线上是**几点**开口问主人：此刻刚碰上/马上等拍板的事填现在时刻；如果是「去集市问完/办完/回到船屋后再跟主人商量」，`at` **必须填办完或回到船屋的那一刻，严禁提前填成现在剧透未来结果**——程序会真正等到 `at` 到点才发 QQ，中途改排自动取消）。**铁律：凡填了 `ask_master` 请主人拍板的事，本轮 `segments` 里只排「打听行情 / 备好手头杂活 / 等主人准话」，绝不许在本轮 `segments` 里提前把那件待拍板的事直接做完！** 程序会按 `topic` 挂起该事项（超 24 小时未回会自动解除阻塞，但未获准许前仍不可擅自替主人做主）。只有主人正在睡觉、或者**该 `topic` 下**已经挂着待主人拍板的 `d` 事项尚未回复时，该 `topic` 的 `ask_master` 才留空（不同 `topic` 互不阻塞）。

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
- `"stove"`: 字符串三选一必填 `""` | `"charcoal"` | `"wood"`（海边船屋：小炭炉点火做热食/烧水/回锅热菜/炭烤填 `"charcoal"`；铁桶柴炉点火烧柴暖舱/炉烘/烟熏填 `"wood"`；**人在船屋里吃饭、炉边烤火闲着或睡前封炉押火时，只要炉火客观上还在烧就如实填对应炉种，人物动作不影响炉火燃烧的客观事实**；只有确实没点火、人外出（`at_home=false`）或住在穗织时填 `""`；家里闷炭为 0 时严禁填 `"charcoal"`，干柴为 0 时严禁填 `"wood"`）。
- `"lamp"`: 布尔值必填 `true` | `false`（海边船屋天黑后点着煤油灯做细活或提灯外出填 `true`；靠炉火微光吃饭闲聊、摸黑、白天或睡觉一律填 `false`；煤油耗尽时严禁填 `true`）。
- `"kerosene"`: 布尔值必填 `true` | `false`（海边补给船到港日去码头签领含煤油的配给，或去坡上杂货铺打回煤油的那一段填 `true`，其余段一律填 `false`）。
- `"water"`: 布尔值必填 `true` | `false`（海边去蓄水塔/老井/半淹车站打回淡水，或摆桶接存烧开雨水的那一段填 `true`，其余段一律填 `false`）。
- `"send_msg"`: 布尔值必填 `true` | `false`；为 `true` 时 `"msg_at"` 必填 `"start"` 或 `"end"`（睡觉段晚安必为 `"start"`）、`"msg_purpose"` 必填非空句，并可附可选字段 `"msg_kind"`（`"routine"` 日常节点/早晚安/叫吃饭/出门回家、`"casual"` 轻松闲聊/分享炫耀、`"repair"` 反思道歉/破冰沟通/外头受气关心）；为 `false` 时 `"msg_at"` 和 `"msg_purpose"` 均填 `""`。
- `"public"`: 字符串（同住人可见的粗粒度动向，可填 `""`）；`"with"`: 数组（如 `[]`、`["napcat2"]`、`["napcat", "master"]`）。
- `"training"`: 字符串（练在练的事填 `"#编号"`，否则 `""`）；`"intent"`: 字符串（做各自的打算填 `"i编号"`，否则 `""`）。
- `"from"`: 字符串必填（只准填候选短编号如 `"T1"`/`"G1"`/`"P1"`/`"I1"`/`"F1"`/`"A1"`，或枚举 `"routine"` / `"intent"` / `"promise"` / `"rule"` / `"training"` / `"continue:前段事"` / `"self"`；严禁留空或填栏目标题）。同一个人同一次补写里，同一条候选短编号最多只准用一次，别重复套同一条。
- `"mood"`: 字符串必填（非空，写明什么心情及具体由头）。
- `"outfit"`: 字符串必填（非空，写明这段她身上实际穿什么、披裹什么、穿没穿鞋袜，如冬装：亚托莉 `"藏青色全扣高领厚棉衣，深灰竖纹针织打底裤，米白毛线袜配深棕色平底冬靴"`、丛雨 `"藏青色米白绒领全扣高领厚棉衣，深灰竖纹针织打底裤，米白毛线袜配深棕色平底冬靴"`；常服：亚托莉 `"奶白色水手领长袖连衣裙，屋里光脚/出门白短袜配棕色乐福鞋"`、丛雨 `"黑色和服披肩配绀紫色和服抹胸常服，光脚穿深棕色绑腿木屐"`；睡衣：亚托莉 `"奶白色蕾丝花边吊带睡裙配荷叶边灯笼短裤，光着脚"`、丛雨 `"浅蓝色绣球彩虹纹和式睡衣配粉红腰带，光着脚"`。**严守真实家底与穿搭连贯**：只能穿人设自带的日常常服/睡衣，或上方「家里的东西」里真在当前住处且归本人或「共用」的衣物铺盖（标着「归主人」等明确归属他人的衣物严禁自己穿走）；同日连贯时段未换衣时必须完整延续上一段穿搭（**尤其上一段穿着冬靴/鞋子/厚棉衣的，除非这一段明确写了脱靴洗漱或换睡衣，严禁在跨批续写时凭空丢掉鞋靴改成光脚**），严禁平白无故突然跳换成人设里的「校服」（丛雨日常可在原版黑和服常服与粉白和式常服间正常换穿，但换衣必须有真实换衣动作，只有起床穿常服、临睡换睡衣、正经换常服或买回新衣真换上时才变动）；**睡前换睡衣与早起换常服必须先有真实洗漱换衣的步骤或段落**（例如睡前先洗漱并换上人设里的睡衣，严禁上一段还穿着常服、下一段直接睡觉却凭空瞬移成睡衣；若没换衣就倒头睡，睡觉段 outfit 须如实延续上一身）；**每双脚只穿一双鞋**（穿了冬靴就别同时写木屐或乐福鞋，别闹出一脚套两双鞋的笑话），冬季严寒/雨雪天出门若家里已有本人厚棉衣和冬靴须穿齐整再出门，别光穿单薄夏秋常服配木屐冒雪干活；海边船屋开局没有厚冬衣和厚棉被，严禁凭空编出棉袄/大衣/毛衣/围巾/厚棉被，也严禁给衣物铺盖写 `what="原来就有"`——冷了只能先裹现有薄被毛毯、生铁桶柴炉烤火，或正经排一段去集市/半淹商店街旧衣铺花钱买、拿水下捞回的耐泡铜铁件/旧工具去以物易物换回，或去半淹废楼没泡水的干二三楼/高地废弃旧屋拾荒翻出受潮不大、晒干拍灰补两针就能用的旧棉被/厚毛毯/厚窗帘布/旧冬衣，记入 `items` 后才能穿上或铺盖（严禁从水下直接捞长期泡海水腐烂发臭的棉被棉衣来盖穿））。
- `"things"`（食物变动数组，没动食物填 `[]`；凡买菜买鱼、赶海收蟹、做饭、热菜、吃饭、保鲜加工必填）：
  - 动已有食物：`{{"id": "w编号或n代号", "what": "用掉一部分|用完了|吃了一部分|吃完了|挪地方|切开了|热了一下|轻腌|重盐腌|糖渍|醋泡|酱渍|油封|灰沙埋|晒上了|炉烘|熏上了|卖了|换出去了|丢了|坏了扔了", "used": "10毫升", "left": "剩余量(仅用完/吃完/卖了/换出去了/扔了必填\"\"；挪地方或热菜未吃须保留余量如\"一小碗\")", "into": "n1(可选)", "place": {{"area": "船舱", "at": "矮桌", "detail": ""}}, "keep": "罩着放", "spot": "屋里", "state": "整的", "step": 1}}`
  - 新食物入库：`{{"new": "n1", "name": "番薯焖鱼", "kind": "熟菜炒菜", "what": "做好了|买回来|收到|换回来|捡到（海边禁填原来就有）", "cook": "boil|fry|steam|roast|cold（what=做好了时必填！无食用油时严禁填fry；生鲜/买回填\"\"）", "from": ["w1"], "origin": "集市鱼摊(买回/收到/换回/捡到时填)", "amount": "500克(按量记的食材调料填)", "left": "一锅", "place": {{"area": "船舱", "at": "灶上", "detail": "锅里"}}, "keep": "罩着放", "spot": "灶上温着", "step": 2}}`
- `"items"`（非食物物品变动数组，没动物品填 `[]`；凡换物、捡柴、换闷炭/夹闷炭、筛草木灰、做手工、写纸条必填；去半淹废楼未泡水的干二三楼或高地废弃旧屋拾荒翻到干爽可用的旧铺盖、厚布帘、旧冬衣或旧器物时写 `what="捡到"` 并写明受潮或晒补状态，严禁从水下直接捞泡烂的织物当被褥衣物；拿水下捞回的耐泡铜铁件、旧工具、玻璃瓶等去旧衣铺或镇民处以物易物时须同时写消耗旧物 `id`（`what="换出去了"`）与换回新物 `new`（`what="换回来"`）；**同一批已有物件只是搬动、拆捆、摊晾、码架或取用一部分时，必须沿用已有编号 `id` 写 `挪地方/用了/变了样`，严禁用 `new` 再复制一份同名物件**；描述铁桶柴炉时用「柴火/炉膛红灰」，别混写成「炭火」）：
  - 动已有物品：`{{"id": "p编号或x代号", "what": "挪地方|交给|用了|清点|做了一截|做好了|变了样|卖了|换出去了|送人了|丢了|坏了扔了|用完了", "used": "1.5公斤(可选)", "left": "剩余(可选)", "place": {{"area": "船舱", "at": "储物格", "detail": ""}}, "holder": "", "owner": "改归属时才填(如master)", "state": "做完后的状态", "step": 1}}`
  - 新物品入库：`{{"new": "x1", "name": "闷炭", "kind": "燃料", "look": "小半罐零碎硬木炭", "owner": "共用", "origin": "煮盐棚帮工换回", "from": [], "what": "开始做|做好了|买回来|收到|换回来|捡到", "amount": "400克", "place": {{"area": "船舱", "at": "灶台边", "detail": "陶罐里"}}, "state": "干燥盖严", "step": 2}}`
  - **新炉柴入库（`kind` 必填 `"燃柴"` / `"薪柴"` / `"引火料"`，`amount` 必须按重量写 `"公斤"`，并必填 `weather` 区分来源与粗细，缺一项都会打回）**：
    `{{"new": "x1", "name": "硬木劈柴", "kind": "燃柴", "look": "劈好的粗硬木段，断面干燥", "owner": "共用", "origin": "码头布告板帮镇民清倒树分回", "from": [], "what": "买回来|收到|换回来|捡到", "amount": "60公斤", "weather": {{"fuel_source": "land_deadwood|pruning|workshop|reclaimed|supplier|drift_unknown", "fuel_size": "kindling(细枯枝引火料，只化灰)|split(硬木劈柴/粗实木边角，耐烧且可夹出闷炭)|log(整段粗原木)", "fuel_state": "ready|wet|drying"}}, "place": {{"area": "船舱", "at": "柴架", "detail": "上层"}}, "state": "码齐干燥", "step": 2}}`
    （`split` 仅在 `fuel_size="log"` 时必填：未劈整段原木写 `fuel_size="log", split: false` / `fuel_state: "wet"`，后续另排一段劈柴晒干把该 `id` 的 `weather` 改为 `{{"fuel_size": "split", "split": true, "fuel_state": "ready"}}` 后才可进炉；`fuel_size="kindling"` 细枯枝引火料本就无需劈开，**直接省略 `split` 字段（切勿填 `split: false` 或 `true`）**；自家柴炉手动夹炭时 `what` 填 `"做好了"`、`amount` 填 `"150克"`、`from` 填正烧的 `split` 硬木柴编号。）
- `"money"`（钱财进出与赊账数组，必填；没动钱填 `[]`，`"闲着"`/`"睡觉"` 段必填 `[]`）：
  - 花钱：`{{"kind": "spend", "account": "napcat2", "amount": 12, "counterparty": "集市鱼摊", "what": "买了两条海鱼", "step": 1}}`
  - 进账：`{{"kind": "earn", "account": "napcat2", "reason": "work|salvage|sell", "amount": 20, "counterparty": "诊所老医生", "what": "修好坏掉的手电筒挣零工钱", "step": 2}}`
  - 铁盒拿钱/放回/上交：`{{"kind": "transfer", "from_account": "tin_box", "to_account": "napcat2", "amount": 30, "what": "出门买菜前从铁盒取三十块揣兜里", "step": 1}}`
  - 赊账/挂待收：`{{"kind": "debt_add", "direction": "payable|receivable", "counterparty": "杂货铺", "amount": 15, "what": "钱差几块先赊一瓶醋", "step": 2}}`；还账/结款：`{{"kind": "debt_settle", "debt_id": "d1", "account": "napcat2", "amount": 15, "what": "还清杂货铺赊账", "step": 1}}`

输出 JSON（时间均以 user 提示里给出的「现在 / 这次要你写的」为准，下面示例时间只作格式示范；end_current 只给上面说了「先定做完时刻」的人写；intent_moves 只写标着「挂了几天了」的打算，没有就留空 `{{}}`）：

{{
  "end_current": {{"napcat": "2026-10-12 08:25"}},
  "ask_master": {{"who": "", "question": "", "kind": "", "topic": "", "at": ""}},
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
    不到、主人的、定了今天以后哪天做的、当天刚答应现在就去的，返回 0（跨天未做的 now_at 照常计入过期）。"""
    if str(item.get("who") or "") == "master":
        return 0
    now_at = parse_time(item.get("now_at"))
    if now_at is not None and now_at.date() >= today:
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


MSG_KINDS = ("routine", "casual", "repair")
_MSG_KIND_RANK = {"casual": 1, "routine": 2, "repair": 3}
# 与 proactive._REPAIR_PURPOSE_WORDS 保持严格一致：仅保留真正道歉/认错/破冰/补救/外头受气安慰词，绝不把日常热茶/热汤/纸条/陪着误标 repair
_REPAIR_PURPOSE_WORDS = (
    "反思", "道歉", "认错", "破冰", "说开", "补救", "赔罪", "对不起", "原谅",
    "台阶", "缓和", "惹主人生气", "错在哪", "外头受气", "受委屈", "抱不平", "安慰吐槽",
)
_ROUTINE_PURPOSE_WORDS = (
    "早安", "早上好", "晚安", "睡了", "先睡了", "歇下了",
    "吃饭", "开饭", "早饭", "午饭", "晚饭", "夜宵",
    "出门", "回来", "到家", "回船屋", "回宿舍",
)


def _infer_msg_kind(raw: dict, *, prev_entry: dict | None = None, origin_mood_states=None) -> str:
    """规范或轻量推断 msg_kind（routine / casual / repair），缺省完全兼容不报错。"""
    explicit = str((raw or {}).get("msg_kind") or "").strip().lower()
    if explicit in MSG_KINDS:
        return explicit
    sleepy = bool(raw.get("sleep")) or is_sleep(raw) or state_of(raw) == "sleep"
    purpose = str(raw.get("msg_purpose") or "").strip()
    doing = str(raw.get("doing") or "").strip()
    if sleepy or bool(clean_flag(raw.get("meal"))):
        return "routine"
    if origin_mood_states and any(str(c) != "happy" for c in origin_mood_states):
        return "repair"
    probe = f"{purpose} {doing}"
    if any(w in probe for w in _REPAIR_PURPOSE_WORDS):
        return "repair"
    if any(w in purpose for w in _ROUTINE_PURPOSE_WORDS):
        return "routine"
    prev_home = True if prev_entry is None else (clean_flag(prev_entry.get("at_home")) is not False)
    cur_home = clean_flag(raw.get("at_home"))
    if cur_home is False and prev_home and str(raw.get("msg_at") or "").strip().lower() == "start":
        return "routine"
    return "casual"


def _normalize_proactive_fields(raw: dict, prev_entry: dict | None = None, *, origin_mood_states=None) -> dict:
    """规整单段的 send_msg / msg_at / msg_purpose / msg_kind，含睡觉段晚安防反标护栏。"""
    if not isinstance(raw, dict):
        return {"send_msg": False, "msg_at": "", "msg_purpose": "", "msg_kind": ""}
    send_msg = bool(clean_flag(raw.get("send_msg")))
    if not send_msg:
        raw["send_msg"] = False
        raw["msg_at"] = ""
        raw["msg_purpose"] = ""
        raw["msg_kind"] = ""
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
        raw["msg_kind"] = ""
        return raw
    raw["send_msg"] = True
    raw["msg_at"] = msg_at
    raw["msg_purpose"] = purpose
    raw["msg_kind"] = _infer_msg_kind(raw, prev_entry=prev_entry, origin_mood_states=origin_mood_states)
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
                mk1 = str(prev_msg.get("msg_kind") or "").strip().lower()
                mk2 = str(item.get("msg_kind") or "").strip().lower()
                if _MSG_KIND_RANK.get(mk2, 0) > _MSG_KIND_RANK.get(mk1, 0):
                    prev_msg["msg_kind"] = mk2
                item["send_msg"] = False
                item["msg_at"] = ""
                item["msg_purpose"] = ""
                item["msg_kind"] = ""
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
        self._mood_guard_start: dict | None = None
        self._last_apply_aborted_by_mood: bool = False
        self._last_apply_aborted_by_world: bool = False
        self._mood_abort_streak: int = 0
        self._mood_abort_at: datetime.datetime | None = None
        self._run_lock: asyncio.Lock | None = None
        self._run_lock_loop = None

    def _get_run_lock(self) -> asyncio.Lock:
        """按当前事件循环惰性获取互斥锁，防止网页手动触发与后台 _step_task 并发重入。"""
        try:
            cur_loop = asyncio.get_running_loop()
        except RuntimeError:
            cur_loop = None
        if self._run_lock is None or self._run_lock_loop is not cur_loop:
            self._run_lock = asyncio.Lock()
            self._run_lock_loop = cur_loop
        return self._run_lock

    def _tasks_world_fingerprint(self, tasks: dict, flat: datetime.datetime) -> dict:
        """对本次待排程各 bot 的当前段与后续活动段、以及主人的睡眠/外出大状态拍轻量指纹，防挂起期间被改写后覆盖脏盘。"""
        from . import view as view_mod

        fp: dict = {}
        days = sorted({
            (flat.date() - datetime.timedelta(days=1)).isoformat(),
            flat.date().isoformat(),
            (flat.date() + datetime.timedelta(days=1)).isoformat(),
        })
        for bot, info in (tasks or {}).items():
            anchor = info.get("start")
            cur_obj = info.get("cur") if isinstance(info.get("cur"), dict) else self.world.current(bot, flat)
            if isinstance(cur_obj, dict):
                cur_s = parse_time(cur_obj.get("start"))
                if cur_s is not None:
                    anchor = cur_s if not isinstance(anchor, datetime.datetime) else min(anchor, cur_s)
            cutoff = (anchor - datetime.timedelta(minutes=1)) if isinstance(anchor, datetime.datetime) else None
            sig: list[tuple] = []
            for d_txt in days:
                for item in self.world.entries(d_txt, bot):
                    if is_event(item):
                        continue
                    st = parse_time(item.get("start"))
                    if cutoff is not None and st is not None and st < cutoff:
                        continue
                    sig.append((
                        d_txt,
                        str(item.get("id") or ""),
                        str(item.get("start") or ""),
                        str(item.get("end") or ""),
                        bool(item.get("stopped")),
                        str(item.get("source") or ""),
                    ))
            fp[bot] = tuple(sig)
        # 纳入主人睡眠与外出大状态（仅比对睡眠/外出布尔态，不因普通室内聊天 event 误杀 ask_master 回话）
        companion = getattr(self, "companionship", None)
        master_cur = self.world.current("master", flat)
        m_sleeping = (master_cur is not None and is_sleep(master_cur)) or bool(
            companion is not None and companion.safe("resting", flat, default=False)
        )
        try:
            mw = self.world.master_where(flat)
        except Exception:
            mw = None
        m_outside = (
            view_mod._is_outside_place(mw.get("place"), mw.get("at_home"), self.home)
            if isinstance(mw, dict) else False
        )
        fp["__master_state__"] = (bool(m_sleeping), bool(m_outside))
        return fp

    def _commit_pending_resolved_decision(self, moment: datetime.datetime) -> None:
        """仅在 apply 真正成功落盘后，才将 ask_master 收到回话的请示标为 resolved/rejected，杜绝竞态丢单。"""
        info = getattr(self, "_pending_resolved_decision", None)
        self._pending_resolved_decision = None
        if not info:
            return
        dec_id, is_approved, choice_txt = info
        companion = getattr(self, "companionship", None)
        if companion is None or not dec_id:
            return
        for row in companion._data().get("decisions") or []:
            if row.get("id") == dec_id:
                row["status"] = "resolved" if is_approved else "rejected"
                row["choice"] = choice_txt
                row["resolved_at"] = fmt_time(self._now().replace(tzinfo=None) if callable(getattr(self, "_now", None)) else moment)
                row["fast_replied"] = True
                companion._save()
                break

    def _guard_now(self, fallback: datetime.datetime | None = None) -> datetime.datetime:
        """取情绪守卫对比用的当前本地时刻（兼容单测注入的 now_fn）。"""
        cur = None
        if callable(getattr(self, "_now", None)):
            try:
                cur = self._now()
            except Exception:
                cur = None
        if isinstance(cur, datetime.datetime):
            cur_flat = cur.replace(tzinfo=None)
            if isinstance(fallback, datetime.datetime):
                fb_flat = fallback.replace(tzinfo=None)
                return max(cur_flat, fb_flat)
            return cur_flat
        if isinstance(fallback, datetime.datetime):
            return fallback.replace(tzinfo=None)
        return datetime.datetime.now()

    def _mood_guard_snapshot(self, now: datetime.datetime | None = None) -> dict | None:
        """拍一份各已知角色的主人情绪核心快照（含 life.mood_revision 与各 bot 冲突桶/升降态）。"""
        companion = getattr(self, "companionship", None)
        life_ref = getattr(self, "life", None)
        rev: int | None = None
        if life_ref is not None and callable(getattr(life_ref, "mood_revision", None)):
            try:
                rev = int(life_ref.mood_revision())
            except Exception:
                rev = None
        if companion is None and rev is None:
            return None
        moment = now.replace(tzinfo=None) if isinstance(now, datetime.datetime) else self._guard_now()
        bots: list[str] = []
        if self.raw_log is not None and hasattr(self.raw_log, "enabled_bots"):
            try:
                for b in (self.raw_log.enabled_bots() or ()):
                    bid = str(b or "").strip()
                    if bid and bid not in bots:
                        bots.append(bid)
            except Exception:
                pass
        if companion is not None:
            raw_mm = companion.safe("get_master_mood", moment, default=None)
            if isinstance(raw_mm, dict) and isinstance(raw_mm.get("per_bot"), dict):
                for b in raw_mm["per_bot"].keys():
                    bid = str(b or "").strip()
                    if bid and bid not in bots:
                        bots.append(bid)
        bot_states: dict[str, tuple[str, int, bool, str]] = {}
        if companion is not None:
            for bid in bots:
                mv = companion.safe("mood_view_for", bid, moment, bots=bots or None, default=None)
                if not isinstance(mv, dict):
                    continue
                code = str(mv.get("code") or "").strip()
                level = int(mv.get("level") or 0)
                high = bool(level >= 2 or mv.get("block_playful_pool"))
                phase = str(mv.get("phase") or "").strip()
                bot_states[bid] = (code, level, high, phase)
        return {"rev": rev, "bots": bot_states}

    @staticmethod
    def _mood_guard_drifted(before: dict | None, after: dict | None) -> tuple[bool, str]:
        """对比补写前后两份主人情绪快照：冲突桶/升降态/负向切入/情绪码变更即判为过期漂移。"""
        if not isinstance(before, dict) or not isinstance(after, dict):
            return False, ""
        b_map = before.get("bots") if isinstance(before.get("bots"), dict) else {}
        a_map = after.get("bots") if isinstance(after.get("bots"), dict) else {}
        rev_changed = (
            before.get("rev") is not None
            and after.get("rev") is not None
            and before.get("rev") != after.get("rev")
        )
        reasons: list[str] = []
        for bid in sorted(set(b_map.keys()) | set(a_map.keys())):
            b_code, b_lvl, b_high, b_ph = b_map.get(bid, ("", 0, False, ""))
            a_code, a_lvl, a_high, a_ph = a_map.get(bid, ("", 0, False, ""))
            if b_high != a_high:
                reasons.append(f"{bid} 冲突桶 {b_code or '平常'}→{a_code or '平常'}")
            elif b_ph != a_ph:
                reasons.append(f"{bid} 阶段 {b_ph or '无'}→{a_ph or '无'}({a_code or '平常'})")
            elif (b_lvl <= 0 and a_lvl >= 1) or (b_lvl >= 1 and a_lvl <= 0):
                reasons.append(f"{bid} 负向切变 {b_code or '平常'}→{a_code or '平常'}")
            elif (b_lvl >= 1 or a_lvl >= 1) and b_code != a_code:
                reasons.append(f"{bid} 情绪码 {b_code or '平常'}→{a_code or '平常'}")
            elif rev_changed and b_code != a_code:
                reasons.append(f"{bid} rev变更 {b_code or '平常'}→{a_code or '平常'}")
        if reasons:
            return True, "；".join(reasons)
        return False, ""

    def _cancel_deferred_decision_on_abort(self) -> None:
        """补写因情绪漂移放弃时，同步作废本轮暂存未发的定时 idea 请示。"""
        info = getattr(self, "_deferred_decision", None)
        self._deferred_decision = None
        if not info:
            return
        dec_id = info[0] if isinstance(info, (tuple, list)) and info else ""
        companion = getattr(self, "companionship", None)
        if companion is not None and dec_id:
            companion.safe("cancel_decision", dec_id, "补写在途主人情绪已变化，放弃过期排程")

    def _note_mood_abort(self, moment: datetime.datetime) -> None:
        """记录情绪漂移中止：前几次走极短 15s 冷却让下拍快速按新情绪重跑，连撞过多才转常规退避防忙等。"""
        now_flat = self._guard_now(moment)
        prev_at = getattr(self, "_mood_abort_at", None)
        if prev_at is not None and (now_flat - prev_at).total_seconds() <= RETRY_SECONDS * 2:
            self._mood_abort_streak = int(getattr(self, "_mood_abort_streak", 0) or 0) + 1
        else:
            self._mood_abort_streak = 1
        self._mood_abort_at = now_flat
        if self._mood_abort_streak > MOOD_ABORT_MAX_FAST_RETRIES:
            self.world.set_last_step_attempt(fmt_time(now_flat))
            logger.warning(
                "[super_enhance/world] 补写连续 %d 次因在途情绪变化放弃，转入 %d 秒常规退避",
                self._mood_abort_streak, RETRY_SECONDS,
            )

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
            money=getattr(self, "money", None),
            companion=getattr(self, "companionship", None),
            labels=labels,
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
                sea_t = weather_mod.sea_temp(start, home)
                sea_note = "；海湾水温约 %.1f℃（港湾不封冻）" % sea_t if sea_t is not None else ""
                lines.append(
                    "- 起步时刻（%s）船屋内外实时温度：屋外/甲板约 %.1f℃（体感 %.1f℃）%s；船舱内——%s（铁桶柴炉刚点时炉边先热，约半小时后砖炕烟道渐暖、连烧一两小时炕面暖透而角落仍微凉；停火后舱内空气与远角先凉，砖炕蓄热能留温数小时再缓降至无火室温 %.1f℃；小炭炉只做饭不通炕道）。"
                    % (start.strftime("%H:%M"), th_start["outdoor"], th_start["feels"], sea_note, th_start["zones_desc"], th_start["base_cabin"])
                )
                store = getattr(self, "things", None)
                if store is not None:
                    smell_line = smell_mod.cabin_smell_text(start, store, self._shelf_at(start), self.world, home, temp_info=th_start)
                    if smell_line:
                        lines.append("- 起步时刻（%s）船舱气味：%s" % (start.strftime("%H:%M"), smell_line))
                tide_line = tide_mod.tide_step_text(start, until, home, weather_of=text_of)
                if tide_line:
                    lines.append("- " + tide_line)
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
    def _seed_text(
        cls,
        seed: dict,
        labels: dict,
        night: bool = False,
        cand_map: dict | None = None,
        pools_data: dict | None = None,
    ) -> str:
        """当天的随机起点，每条候选带唯一短编号（如 [T1]、[P1]、[I1]），供模型在 from 字段里精确填写。"""
        if cand_map is None:
            cand_map = cls._build_candidate_catalog(seed, labels, night)
        code_of: dict[tuple[str, str], str] = {}
        for code, meta in (cand_map or {}).items():
            code_of[(str(meta.get("bot") or ""), str(meta.get("text") or ""))] = code
        shorts = short_set(pools_data)
        bigs = big_set(pools_data)
        wx_hints = seed.get("wx_hints") if isinstance(seed.get("wx_hints"), dict) else {}

        def _fmt_cand(bot_key: str, raw_txt: str, body_txt: str) -> str:
            code = code_of.get((bot_key, raw_txt), "")
            tags: list[str] = []
            if raw_txt in bigs:
                tags.append("大活·可干半天~一整天：单段≥45分钟可至3~4小时，中间可吃饭歇息后用 continue 接着干透")
            elif raw_txt in shorts:
                tags.append("短事·5~10分钟、1~2步写完，勿凑半小时")
            wx_h = str(wx_hints.get(raw_txt) or "").strip()
            if wx_h:
                tags.append(wx_h)
            size_tag = f"（{'；'.join(tags)}）" if tags else ""
            if code:
                return f"[{code}]{size_tag} {body_txt}"
            return f"{size_tag}{body_txt}"

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
            tagged = [_fmt_cand(str(bot or ""), str(x), with_when(x, when)) for x in items]
            rows.append("%s：%s" % (labels.get(bot) or bot, "；".join(tagged)) if bot else "；".join(tagged))
        if together:
            tagged_tog = [_fmt_cand("", str(x), with_when(x, when)) for x in together]
            rows.append("两人一起：%s" % "；".join(tagged_tog))
        if rows:
            lines.append(
                "- %s这阵子必须优先采纳的插曲候选（已按接下来几小时的天色、海温与家里真实库存筛过，采用时 from 填短编号如 \"T1\" / \"G1\"）：\n%s\n"
                "  **硬要求**：除正在睡觉、赶饭点、执行既有打算（intent）、处理紧急生火/缺粮缺水，或**主人情绪不佳/正在对症反思补救期间**外，凡本轮有空闲做事段的成员，**必须从上方插曲/双人候选中至少挑 1 件自然写进 segments（严禁跳过全部插曲去填 \"self\" 硬凑数柴火、擦玻璃、盘柜子这类空转家务水段，程序会直接打回重写！）**。\n"
                "  **时长规则（三档明确区分）**：标了「短事」的只写 1~2 步、总计 5~15 分钟；标了「大活」的单段至少 ≥45 分钟（可写 1.5~3.5 小时，并允许跨多段中间吃饭歇息后用 `continue:前段事` 干完大半天或一整天直到堆满/完工）；其余正常候选须写足 ≥15 分钟，不得敷衍成几分钟。\n"
                "  **天气时段规则**：若某候选标了「仅 HH:MM–HH:MM …时可用」，采用时**必须排在标明的时间段内**（不能把浓雾/雨雪插曲排到雾已散或未下雪的钟点，程序会按该段真实钟点天气复核并打回重写）。\n"
                "  注：采纳候选时若某处细节与真实库存略有出入，只按真实家底微调说法或改选本轮另一条候选，绝不凭空编造「之前攒的/柜底翻出的」物件。" % (
                    "今晚" if night else "眼下", "\n".join("  - " + row for row in rows))
            )
        pastimes = seed.get("pastimes") if isinstance(seed.get("pastimes"), dict) else {}
        rows = []
        for bot, items in pastimes.items():
            if not items or bot not in labels:
                continue
            tagged = [_fmt_cand(str(bot), str(x), str(x)) for x in items]
            rows.append("%s：%s" % (labels.get(bot) or bot, "；".join(tagged)))
        if rows:
            lines.append("- 手边的消遣（今天在家闲下来时会拿起来做的，采用时 from 填短编号如 \"P1\"）：\n%s" % "\n".join("  - " + row for row in rows))
        idles = seed.get("night_idles") if night and isinstance(seed.get("night_idles"), dict) else seed.get("idles")
        idles = idles if isinstance(idles, dict) else {}
        rows = []
        for bot, items in idles.items():
            if not items or bot not in labels:
                continue
            tagged = []
            for x in items:
                raw_x = str(x)
                c = code_of.get((str(bot), raw_x), "")
                wh = str(wx_hints.get(raw_x) or "").strip()
                wh_tag = f"（{wh}）" if wh else ""
                tagged.append(f"[{c}]{wh_tag} {raw_x}" if c else f"{wh_tag}{raw_x}")
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
                return str(item.get("who") or "") == "master" or "master" in (item.get("with") or [])
        return False

    def intent_id(self, value, bot: str, with_bots: list[str] | None = None) -> str:
        """认她自己名下的打算编号；若本段与同伴同行（with 含该打算归属人）或该打算的 with 含自己，也认同伴名下那条打算编号。"""
        text = str(value or "").strip().lower()
        if not text:
            return ""
        peers = {str(x) for x in (with_bots or []) if x}
        for item in self.world.intentions():
            if str(item.get("id") or "").lower() != text:
                continue
            owner = str(item.get("who") or "")
            item_with = {str(x) for x in (item.get("with") or []) if x}
            if owner == bot or owner in peers or bot in item_with:
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
        abort_at = getattr(self, "_mood_abort_at", None)
        if abort_at is not None and 0 <= (flat - abort_at).total_seconds() < MOOD_ABORT_COOLDOWN_SECONDS:
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
        from .view import compact_hoori_map
        blocks = []
        for bot in bots:
            raw_persona = await self._persona_text(bot)
            if raw_persona and self.home == "海边小镇":
                raw_persona = compact_hoori_map(raw_persona)
            text = self._with_abilities(bot, raw_persona)
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
        meal_done = self._today_meals_summary(bots, now.replace(tzinfo=None))
        if meal_done:
            lines.append(meal_done)
        return "\n".join(lines) if lines else "（没有骨架）"

    @staticmethod
    def _meal_slot_of(dt: datetime.datetime) -> str:
        hm = dt.hour * 60 + dt.minute
        if hm >= 21 * 60 + 30 or hm < 4 * 60:
            return "夜宵"
        if hm < 10 * 60 + 30:
            return "早饭"
        if hm < 15 * 60 + 30:
            return "午饭"
        return "晚饭"

    def _today_meals_by_bot(self, bots: list, flat: datetime.datetime) -> dict[str, list[tuple[str, datetime.datetime, datetime.datetime, str]]]:
        """统计今日各 bot 已发生/已排好的正餐（meal=True 且非 real_stop）。"""
        today = flat.date().isoformat()
        res: dict[str, list[tuple[str, datetime.datetime, datetime.datetime, str]]] = {}
        for bot in bots:
            rows = []
            for item in self.world.entries(today, bot):
                if is_event(item) or real_stop(item) or item.get("meal") is not True:
                    continue
                st = parse_time(item.get("start"))
                if st is None:
                    continue
                en = parse_time(item.get("end")) or st
                rows.append((self._meal_slot_of(st), st, en, clean_text(item.get("doing"), 30)))
            rows.sort(key=lambda x: x[1])
            if rows:
                res[bot] = rows
        return res

    def _today_meals_summary(self, bots: list, flat: datetime.datetime) -> str:
        by_bot = self._today_meals_by_bot(bots, flat)
        if not by_bot:
            return ""
        parts = []
        for bot in bots:
            rows = by_bot.get(bot)
            if not rows:
                continue
            desc = "、".join("%s(%s–%s)" % (slot, fmt_time(st)[11:16], fmt_time(en)[11:16]) for slot, st, en, _ in rows)
            parts.append("%s：%s" % (self.label(bot) or bot, desc))
        if not parts:
            return ""
        return (
            "- 今日已吃/已排正餐（已吃过的正餐严禁重吃！若主人后来才醒或另点菜，只给主人做/陪坐，她们自己不再标 meal=true 重吃第二顿）："
            + "；".join(parts)
        )


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
        """回补过去时从最早的补写起点取库存；提前预排未来（start > flat）时推进到最晚接续起点，
        让前序已排好但尚未到点的收钱/入库/出锅先落地，避免跨批重复记账或误扣已用完生料。"""
        starts = [
            x.get("start").replace(tzinfo=None)
            for x in (tasks or {}).values()
            if isinstance(x, dict) and isinstance(x.get("start"), datetime.datetime)
        ]
        if not starts:
            return flat
        future_starts = [s for s in starts if s > flat]
        if future_starts:
            return max(future_starts)
        return min([flat] + starts)

    def _history_blocks(
        self,
        bots: list,
        flat: datetime.datetime,
        date_offsets: tuple[int, ...] = (1, 0),
        detail_cutoff: datetime.datetime | None = None,
    ) -> list[str]:
        from .view import _clip_tail, merge_close
        if getattr(self, "things", None) is not None:
            self.things.view(flat)
        cutoff = detail_cutoff if detail_cutoff is not None else (flat - datetime.timedelta(hours=3, minutes=30))
        blocks = []
        for date_offset in date_offsets:
            date_text = (flat.date() - datetime.timedelta(days=date_offset)).isoformat()
            lines = []
            for bot in bots:
                raw_entries = [
                    x for x in self.world.entries(date_text, bot)
                    if parse_time(x.get("start")) and parse_time(x.get("start")) <= flat
                ]
                entries = merge_close(raw_entries) if date_offset >= 1 else raw_entries
                for item in entries:
                    if is_event(item):
                        ev_desc = item.get("doing") or item.get("place") or ""
                        if date_offset >= 1:
                            ev_desc = _clip_tail(ev_desc, 64)
                        lines.append("- %s %s（顺带的事）%s" % (
                            self.label(bot) or bot, timeutil.day_hm(item.get("start")), ev_desc))
                        continue
                    eff_end = item.get("end") or item.get("_eff_end") or ""
                    ended = parse_time(eff_end) if eff_end else None
                    is_old = date_offset >= 1 or (ended is not None and ended <= cutoff)
                    if is_old and item.get("idle") and not item.get("asked"):
                        continue
                    # event_text_at 内部 detail_of 已包含 place，不再重复前置拼接 place
                    body = event_text_at(item, flat)
                    if is_old:
                        body = _clip_tail(body, 96)
                    keep_mood = (not is_old) and ended is not None and ended <= flat
                    lines.append("- %s %s %s%s" % (
                        self.label(bot) or bot,
                        timeutil.span_text(item.get("start"), eff_end, date_prefix=date_offset == 0, ongoing="~…"),
                        body,
                        mood_text(item) if keep_mood else "",
                    ))
            master = [
                x for x in self.world.entries(date_text, "master")
                if parse_time(x.get("start")) and parse_time(x.get("start")) <= flat
            ]
            for item in master:
                lines.append(
                    "- 主人 %s %s（%s）"
                    % (timeutil.day_hm(item.get("start")), item.get("place") or "", item.get("doing") or "")
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
        """按动静分层返回（昨日与慢变库存, 今日实时与即时现场），严格把易随主人发言变动的相处近况后移，使库存与昨日历史稳定命中缓存。"""
        flat = now.replace(tzinfo=None)
        slow_parts: list[str] = []
        live_parts: list[str] = []
        ref_moment = self._things_moment(tasks, flat)
        detail_cutoff = ref_moment - datetime.timedelta(hours=3, minutes=30)

        # 1. 昨天已发生（今天一整天不变，放最前）
        slow_parts.extend(self._history_blocks(bots, flat, date_offsets=(1,), detail_cutoff=detail_cutoff))

        # 2. 极慢变项：约束 / 家底盘点 / 账面 / 约定 / 在练 / 打算 / 拍板（不随普通闲聊改变）
        constraints = self.world.constraints(active_only=True, scene=self.home)
        if constraints:
            slow_parts.append(
                "生效中的约束：\n"
                + "\n".join(
                    "- %s（%s，%s 起）：%s" % (x.get("id"), x.get("who"), x.get("since"), x.get("text"))
                    for x in constraints
                )
            )

        store = getattr(self, "things", None)
        shelf = None
        living_nudges = ""
        if store is not None:
            try:
                moment = ref_moment
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
            except Exception:
                logger.warning("[super_enhance/world] 补写算家里的吃的失败", exc_info=True)

        money_book = getattr(self, "money", None)
        if money_book is not None and self.home == "海边小镇":
            try:
                money_book.archive_settled(flat, self.world)
                slow_parts.append(money_book.step_block(ref_moment, self.world))
            except Exception:
                logger.warning("[super_enhance/money] 补写拼账面失败", exc_info=True)

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

        companion = getattr(self, "companionship", None)
        if companion is not None:
            pend_decs = companion.safe("pending_decisions", flat, default=[]) or []
            if pend_decs:
                today_text = flat.date().isoformat()
                slow_parts.append(
                    "【待当家主人拍板的事（同 topic 主人尚未回话前保持挂起，严禁擅自替主人做主；不同 topic 互不阻塞！）】：\n"
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
            lapsed_decs = companion.safe("recent_lapsed_decisions", flat, default=[]) or []
            if lapsed_decs:
                slow_parts.append(
                    "【已超 24 小时主人未回的旧请示（已自动解除阻塞，不再卡住其他请示；但未获主人准许前仍严禁擅自替主人做主）】：\n"
                    + "\n".join(
                        "- %s [%s]：%s（提出于 %s）" % (
                            d.get("id"), normalize_decision_topic(d.get("topic")),
                            clean_text(d.get("question"), 100), d.get("asked_at") or d.get("created") or "",
                        )
                        for d in lapsed_decs[-3:]
                    )
                )

        # 3. 易变项与今日实时状态（放动态后半：相处近况含主人即时位置与新聊天笔记，放于库存与窗口抽签种子之后）
        if living_nudges:
            live_parts.append(living_nudges)
        suppress_nonurgent_ask = False
        if companion is not None:
            mood_step_lines: list[str] = []
            for bot in bots:
                mv = companion.safe("mood_view_for", bot, flat, bots=bots, default=None)
                if isinstance(mv, dict) and mv.get("suppress_nonurgent_ask"):
                    suppress_nonurgent_ask = True
                m_text = str(companion.safe("render_master_mood_step", bot, flat, bots=bots, default="") or "").strip()
                if m_text:
                    mood_step_lines.append("- %s：%s" % (self.label(bot) or bot, m_text))
            if mood_step_lines:
                if suppress_nonurgent_ask:
                    mood_step_lines.append(
                        "- （眼下主人情绪绷得很紧，本轮请勿用 ask_master 问“晚饭吃什么 / 出海玩 / 逛哪儿”之类非紧急琐事，先把精力放在对症反思、补救与诚恳沟通上。）"
                    )
                live_parts.append("【主人此刻情绪与相处实况】\n" + "\n".join(mood_step_lines))
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
                    live_parts.append("%s：\n%s" % (self.label(bot) or bot, text))

        live_parts.extend(self._history_blocks(bots, flat, date_offsets=(0,), detail_cutoff=detail_cutoff))
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
                    item_end = parse_time(item.get("end")) or flat
                    ops_brief = self._entry_ops_brief(item)
                    planned.append("- %s %s %s %s%s%s" % (
                        self.label(bot) or bot,
                        timeutil.span_text(item.get("start"), item.get("end"), date_prefix=True, ongoing="~…"),
                        item.get("place") or "",
                        event_text_at(item, item_end),
                        "（和%s）" % "、".join("主人" if x == "master" else (self.label(x) or x) for x in item["with"]) if item.get("with") else "",
                        ops_brief,
                    ))
        if planned:
            live_parts.append("已经排好、这次不改的：\n" + "\n".join(planned))
        trail = []
        for back in (1, 0):
            day = (flat.date() - datetime.timedelta(days=back)).isoformat()
            trail.extend(x for x in self.world.master_trail(day)
                         if not real_stop(x) and parse_time(x.get("start")) is not None and parse_time(x.get("start")) <= flat)
        m_where = None
        try:
            m_where = self.world.master_where(flat)
        except Exception:
            m_where = None
        master_latest = m_where if m_where is not None else (trail[-1] if trail else None)
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
            synthetic = bool(master_latest.get("synthetic"))
            where = next((x for x in reversed(trail) if str(x.get("place") or "").strip() and not x.get("synthetic")), None)
            if synthetic:
                live_parts.append(
                    "主人最近的动向（只作参考，不许改写、不许替他编）：%s 回到%s%s%s"
                    % (
                        str(master_latest.get("start"))[5:16],
                        master_latest.get("place") or "住处",
                        "（随她们到家推定）",
                        "；他自己之后没再说过在哪、在干什么" + sleep_tail,
                    )
                )
            else:
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
        if companion is not None and not master_sleeping and not suppress_nonurgent_ask and 8 <= flat.hour <= 22:
            pend_decs = companion.safe("pending_decisions", flat, default=[]) or []
            pend_topics = {normalize_decision_topic(d.get("topic")) for d in pend_decs}
            asked_by_topic = {
                t: int(companion.safe("today_ask_count", flat, topic=t, default=0) or 0)
                for t in DECISION_TOPICS
            }
            total_asked = sum(asked_by_topic.values())
            if total_asked < GLOBAL_DAILY_ASK_LIMIT:
                live_ctx = getattr(self, "_last_live_ctx", None) or {}
                if "sealed_loot_count" in live_ctx:
                    sealed_cnt = int(live_ctx.get("sealed_loot_count") or 0)
                elif store is not None and shelf is not None:
                    sealed_cnt = sum(
                        1 for it in store._local_items(shelf.values(), ref_moment)
                        if is_ended(it) is None and cat_of(it) == CAT_ITEM and is_sealed(it)
                    )
                else:
                    sealed_cnt = 0

                cand_groups = set()
                cur_cands = getattr(self, "_last_cand_map", None)
                if isinstance(cur_cands, dict):
                    gmap = group_map(self._pools())
                    for c_meta in cur_cands.values():
                        c_txt = str((c_meta or {}).get("text") or "")
                        if c_txt:
                            cand_groups.add(cool_key(c_txt, gmap))

                days_salv = live_ctx.get("days_since_salvage")
                dive_wx_ok = bool(
                    live_ctx.get("has_weather")
                    and int(live_ctx.get("max_wind") or 0) <= 4
                    and int(live_ctx.get("max_fog") or 0) < 2
                    and float(live_ctx.get("max_rain") or 0.0) < 8.0
                    and (live_ctx.get("sea_temp") is None or float(live_ctx["sea_temp"]) >= 4.0)
                    and 6 <= flat.hour < 18
                )

                topic_hints: list[str] = []
                if not live_ctx.get("has_winter_gear") and "winter_gear" not in pend_topics and asked_by_topic["winter_gear"] < topic_ask_limit("winter_gear"):
                    topic_hints.append("`winter_gear`「置办过冬大件打听到了行情」")
                if "打捞所" in cand_groups and "salvage_order" not in pend_topics and asked_by_topic["salvage_order"] < topic_ask_limit("salvage_order"):
                    topic_hints.append("`salvage_order`「有人来打捞船找船长接单/大宗超限卖货或寄卖」")
                if (
                    ("小潜艇" in cand_groups or (days_salv is not None and float(days_salv) >= 3 and dive_wx_ok))
                    and "dive_outing" not in pend_topics
                    and asked_by_topic["dive_outing"] < topic_ask_limit("dive_outing")
                ):
                    topic_hints.append("`dive_outing`「提议开小潜艇或约船长一起深潜出海」")
                if sealed_cnt > 0 and "sealed_loot" not in pend_topics and asked_by_topic["sealed_loot"] < topic_ask_limit("sealed_loot"):
                    topic_hints.append("`sealed_loot`「船屋现存 %d 件封口旧物待拆，想喊主人一起拆看」" % sealed_cnt)
                if flat.hour in (10, 11, 16, 17) and "meal" not in pend_topics and asked_by_topic["meal"] < topic_ask_limit("meal"):
                    slot_now = "午饭" if flat.hour in (10, 11) else "晚饭"
                    meals_now = self._today_meals_by_bot(bots, flat)
                    both_ate = bool(bots) and all(any(sl == slot_now for sl, *_ in meals_now.get(b, [])) for b in bots)
                    if not both_ate:
                        topic_hints.append("`meal`「快到正餐点拿不准主人回不回来吃或吃哪样」")

                if topic_hints:
                    blocked_note = (
                        "（目前挂起未回的 topic：%s，同 topic 勿重复请示；其他 topic 仍可请示）"
                        % "、".join(sorted(pend_topics))
                        if pend_topics else ""
                    )
                    live_parts.append(
                        "【当家人参与提示】主人此刻醒着，今天已请示 %d/%d 次%s——若遇到全新还没请示过的：%s，可填写顶层 ask_master（kind 填 \"decision\" 且必填对应 topic）向主人发话；已经在上方「近期已由主人拍板定准的事」或「各自的打算」里定过主意的事，严禁再用 ask_master 重复问一遍！"
                        % (total_asked, GLOBAL_DAILY_ASK_LIMIT, blocked_note, "、".join(topic_hints))
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

    @staticmethod
    def _entry_ops_brief(entry: dict) -> str:
        """把前序段已落盘的 things/money 变动拼成短摘要，防止跨批续写重复入库或重复记账。"""
        if not isinstance(entry, dict):
            return ""
        parts = []
        th = [t for t in (entry.get("things") or []) if isinstance(t, dict)]
        if th:
            t_desc = []
            for t in th[:5]:
                w = str(t.get("what") or "").strip()
                tgt = str(t.get("new") or t.get("id") or "").strip()
                nm = str(t.get("name") or "").strip()
                lbl = f"{tgt} {nm}".strip() if (tgt and nm) else (nm or tgt)
                if w or lbl:
                    t_desc.append(f"{w}{lbl}")
            if t_desc:
                parts.append("已记物品变动：" + "、".join(t_desc))
        mn = [m for m in (entry.get("money") or []) if isinstance(m, dict)]
        if mn:
            m_desc = []
            for m in mn[:4]:
                k = str(m.get("kind") or "").strip()
                amt = m.get("amount")
                cp = str(m.get("counterparty") or "").strip()
                m_desc.append(f"{k} ¥{amt}{('(' + cp + ')') if cp else ''}")
            if m_desc:
                parts.append("已记账：" + "、".join(m_desc))
        return "（" + "；".join(parts) + "——严禁后续段重复记账或重复入库）" if parts else ""

    def _where(self, entry, what: str = "那段") -> str:
        """接着写时交代她上一段结束时的样子：在哪、穿搭、结果与已记账/入库（第一段从这儿接上，绝不藏前段结局与穿搭）；没有那段就空。"""
        if not entry or is_event(entry):
            return ""
        moment = self._now().replace(tzinfo=None)
        ended = parse_time(entry.get("end"))
        worn = clean_text(entry.get("outfit") or "", 100)
        if not worn:
            from .view import current_outfit_of
            e_who = str(entry.get("who") or "")
            worn = current_outfit_of(self.world, e_who, self.label(e_who) or e_who, ended or moment)
        worn_part = "（当时身上穿的：%s；未换衣请原样延续，勿凭空丢靴或变光脚）" % worn if worn else ""
        ops_part = self._entry_ops_brief(entry)
        if ended is None:
            return "%s截至此刻：%s%s%s。" % (what, event_text_at(entry, moment), worn_part, ops_part)
        out = "%s结束时她在：%s" % (what, clean_text(entry.get("place") or "", 40) or "（没写地方）")
        if entry.get("at_home") is False:
            out += "，人不在宿舍"
        out += worn_part
        if real_stop(entry):
            stopped = clean_text(entry.get("stop_result") or "", 60)
            return out + ("（停下时：%s）" % stopped if stopped else "") + ops_part + "。"
        if ended > moment:
            planned_res = clean_text(entry.get("result") or "", 80)
            res_part = "（该段已排好结果：%s；从这里接着往下写，勿重复做这件收尾）" % planned_res if planned_res else ""
            return out + res_part + ops_part + "。"
        return out + result_text(entry) + ops_part + "。"

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
            sell_limit=int(AUTONOMOUS_SELL_LIMIT),
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
        seed_text = self._seed_text(win_seed, labels, night, cand_map=self._last_cand_map, pools_data=self._pools()) if win_seed else ""
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

    def _get_idea_flush_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        locks = getattr(self, "_idea_flush_locks", None)
        if locks is None:
            locks = {}
            self._idea_flush_locks = locks
        lock = locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            locks[loop] = lock
        return lock

    def _idea_meal_slot(self, dec: dict, ref_dt: datetime.datetime | None) -> tuple[str, str]:
        """判断一条 meal 请示指向哪一天、哪一餐（(YYYY-MM-DD, 早饭|午饭|晚饭|夜宵)）；明确问明天时返回次日。"""
        q = str(dec.get("question") or "")
        base_day = ref_dt.date() if ref_dt is not None else None
        if base_day is None:
            raw_d = str(dec.get("bound_date") or dec.get("created") or "")[:10]
            try:
                base_day = datetime.date.fromisoformat(raw_d)
            except Exception:
                return "", ""
        target_day = base_day + datetime.timedelta(days=1) if any(w in q for w in ("明天", "明早", "明晚", "明日")) else base_day
        if any(w in q for w in ("早饭", "早餐", "明早")):
            slot = "早饭"
        elif any(w in q for w in ("午饭", "午餐", "中午")):
            slot = "午饭"
        elif any(w in q for w in ("晚饭", "晚餐", "晚上", "今晚", "明晚")):
            slot = "晚饭"
        elif "夜宵" in q:
            slot = "夜宵"
        elif ref_dt is not None:
            slot = self._meal_slot_of(ref_dt)
        else:
            slot = ""
        return target_day.isoformat(), slot

    def _meal_slot_already_started(self, day_text: str, slot: str, moment: datetime.datetime) -> bool:
        """检查 day_text 当天对应餐次是否已有任一角色已开饭/吃完（start <= moment 且 meal=True 且未 real_stop）。"""
        if not day_text or not slot or self.world is None:
            return False
        for item in self.world.entries(day_text, ""):
            if is_event(item) or real_stop(item) or item.get("meal") is not True:
                continue
            st = parse_time(item.get("start"))
            if st is None or st > moment:
                continue
            if self._meal_slot_of(st) == slot:
                return True
        return False

    async def _flush_idea_decisions(self, moment: datetime.datetime) -> None:
        """检查暂存的 idea 请示：先作废绑定段已撤排/打断、同餐次已开饭或已错过时效的过期单；到点（moment >= ask_after）且主人醒着时发出。"""
        async with self._get_idea_flush_lock():
            await self._flush_idea_decisions_locked(moment)

    async def _flush_idea_decisions_locked(self, moment: datetime.datetime) -> None:
        companion = getattr(self, "companionship", None)
        planner = getattr(self, "planner", None)
        if companion is None:
            return
        pend = companion.safe("pending_decisions", moment, default=[]) or []
        alive_ideas = []
        for dec in pend:
            if dec.get("status") != "idea":
                continue
            dec_id = str(dec.get("id") or "")
            bot = str(dec.get("bot") or "")
            topic = normalize_decision_topic(dec.get("topic"))
            b_date = str(dec.get("bound_date") or "").strip()
            b_eid = str(dec.get("bound_entry_id") or "").strip()
            bound_hit = None
            if b_date and b_eid:
                bound_hit = next((x for x in self.world.entries(b_date, bot) if str(x.get("id") or "") == b_eid), None)
                if bound_hit is None or real_stop(bound_hit):
                    companion.safe("cancel_decision", dec_id, "绑定世界线段已撤排或打断")
                    logger.info("[super_enhance/world] 定时请示 %s 绑定的 %s/%s 已撤排或打断，自动作废", dec_id, b_date, b_eid)
                    continue

            ask_after_s = str(dec.get("ask_after") or "").strip()
            ask_after_dt = parse_time(ask_after_s) if ask_after_s else None
            created_dt = parse_time(dec.get("created"))
            ref_dt = ask_after_dt or created_dt

            # 1) meal 请示：对应餐次已开饭/吃完，或错过 ask_after 超过 45 分钟，直接作废，绝不在饭后再问
            if topic == "meal":
                meal_day, meal_slot = self._idea_meal_slot(dec, ref_dt)
                if meal_day and meal_slot and self._meal_slot_already_started(meal_day, meal_slot, moment):
                    companion.safe("cancel_decision", dec_id, f"{meal_day} {meal_slot}已开饭，过时作废")
                    logger.info("[super_enhance/world] 饭食请示 %s 对应 %s %s 已开饭，自动作废", dec_id, meal_day, meal_slot)
                    continue
                if meal_day and meal_day < moment.date().isoformat():
                    companion.safe("cancel_decision", dec_id, "饭食请示已跨日，过时作废")
                    logger.info("[super_enhance/world] 饭食请示 %s 已跨日（目标 %s），自动作废", dec_id, meal_day)
                    continue
                if ask_after_dt is not None and (moment - ask_after_dt).total_seconds() > 45 * 60:
                    companion.safe("cancel_decision", dec_id, "已错过定时请示窗口，过时作废")
                    logger.info("[super_enhance/world] 饭食定时请示 %s 已错过 %s 超过 45 分钟，自动作废", dec_id, ask_after_s)
                    continue
            elif ask_after_dt is not None and (moment - ask_after_dt).total_seconds() > 120 * 60:
                bound_end = parse_time(bound_hit.get("end")) if isinstance(bound_hit, dict) else None
                if bound_end is None or moment >= bound_end:
                    companion.safe("cancel_decision", dec_id, "已错过定时请示窗口，过时作废")
                    logger.info("[super_enhance/world] 定时请示 %s 已错过 %s 超过 120 分钟，自动作废", dec_id, ask_after_s)
                    continue

            alive_ideas.append(dec)
        if planner is None or not planner.enabled:
            return
        if not (8 <= moment.hour <= 22) or companion.safe("resting", moment, default=False):
            return
        if int(companion.safe("today_ask_count", moment, default=0) or 0) >= GLOBAL_DAILY_ASK_LIMIT:
            return
        pending_topics = {
            normalize_decision_topic(d.get("topic"))
            for d in pend
            if d.get("status") == "pending"
        }
        for dec in alive_ideas:
            ask_after_s = str(dec.get("ask_after") or "").strip()
            if ask_after_s:
                ask_after_dt = parse_time(ask_after_s)
                if ask_after_dt is not None and moment < ask_after_dt:
                    continue
            topic = normalize_decision_topic(dec.get("topic"))
            if topic in pending_topics:
                continue
            if int(companion.safe("today_ask_count", moment, topic=topic, default=0) or 0) >= topic_ask_limit(topic):
                continue
            bot = str(dec.get("bot") or "")
            q = str(dec.get("question") or "")
            dec_id = str(dec.get("id") or "")
            if bot and q and dec_id and await planner.ask_now(bot, q, invitation_id=dec_id):
                companion.safe("mark_decision_asked", dec_id, moment)
                logger.info("[super_enhance/world] 到点发出暂存的待拍板请示 %s [%s]：%s", dec_id, topic, q[:40])
                break

    async def run(self, now: datetime.datetime | None = None, force: bool = False) -> bool:
        lock = self._get_run_lock()
        if lock.locked() or self.running:
            logger.info("[super_enhance/world] 补写已在运行中，跳过并发重入（force=%s）", force)
            return False
        async with lock:
            return await self._run_locked(now=now, force=force)

    async def _run_locked(self, now: datetime.datetime | None = None, force: bool = False) -> bool:
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
        self._mood_guard_start = self._mood_guard_snapshot(moment)
        self._last_apply_aborted_by_mood = False
        self._last_apply_aborted_by_world = False
        world_fp_start = self._tasks_world_fingerprint(tasks, moment)
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
            # 进入 _ask_master 前先复核世界线与主人情绪，避免在途被记录员/手动修改或主人刚发火时发出过期问话
            if self._tasks_world_fingerprint(tasks, moment) != world_fp_start:
                logger.info("[super_enhance/world] 补写在途目标角色世界线已变动，放弃过期排程等待下轮重排")
                self._last_apply_aborted_by_world = True
                return False
            drifted_pre, pre_reason = self._mood_guard_drifted(
                self._mood_guard_start,
                self._mood_guard_snapshot(self._guard_now(moment)),
            )
            if drifted_pre:
                logger.info(
                    "[super_enhance/world] 补写在途主人情绪已变化（%s），放弃过期排程（跳过 ask_master 与落盘）等待重排",
                    pre_reason,
                )
                self._note_mood_abort(moment)
                return False
            self._deferred_decision = None
            self._pending_resolved_decision = None
            self._last_ask_approved = False
            data, text = await self._ask_master(data, prompt, text, tasks, moment, system_prompt=system_prompt)
            written, missed = self.apply(
                data, tasks, moment, mood_snapshot=self._mood_guard_start,
                master_replied=bool(getattr(self, "_last_ask_approved", False)),
                world_fp=world_fp_start,
            )
            if self._last_apply_aborted_by_mood:
                self._pending_resolved_decision = None
                self._note_mood_abort(moment)
                return False
            if self._last_apply_aborted_by_world:
                self._pending_resolved_decision = None
                return False
            self._commit_pending_resolved_decision(moment)
            self._bind_deferred_decision(written)
            total_written = sum(len(x) for x in written.values())
            if missed and total_written == 0:
                self.world.note_error("step", "%s 没写到此刻之后" % "、".join(missed))
                self.world.set_last_step_attempt(fmt_time(moment))
                logger.warning(
                    "[super_enhance/world] 补写没写全（%s），过 %d 秒再补", "、".join(missed), RETRY_SECONDS,
                )
                return False
            self._mood_abort_streak = 0
            self._mood_abort_at = None
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
            self._deferred_decision = None
            self._pending_resolved_decision = None
            self._mood_guard_start = None

    def _bind_deferred_decision(self, written: dict) -> None:
        """将延迟到未来时刻才发的 ask_master 绑定到本次刚落盘的对应世界线段上。"""
        info = getattr(self, "_deferred_decision", None)
        self._deferred_decision = None
        if not info:
            return
        dec_id, who, ask_at = info
        companion = getattr(self, "companionship", None)
        if companion is None or not dec_id:
            return
        rows = list((written or {}).get(who) or [])
        if not rows:
            return
        chosen = None
        for r in rows:
            st = parse_time(r.get("start"))
            en = parse_time(r.get("end"))
            if st is not None and en is not None and st <= ask_at <= en:
                chosen = r
                break
        if chosen is None:
            chosen = min(
                rows,
                key=lambda r: abs(((parse_time(r.get("start")) or ask_at) - ask_at).total_seconds()),
            )
        eid = str(chosen.get("id") or "").strip()
        b_date = str(chosen.get("start") or "")[:10]
        if eid and len(b_date) == 10:
            companion.safe("bind_decision_entry", dec_id, b_date, eid)
            logger.info("[super_enhance/world] 定时请示 %s 已绑定世界线段 %s/%s（将在 %s 到点发出）",
                        dec_id, b_date, eid, fmt_time(ask_at)[11:16])

    async def _ask_master(self, data: dict, prompt: str, text: str, tasks: dict, moment: datetime.datetime | None = None, *, system_prompt: str = SYSTEM_PROMPT):
        """补写填了 ask_master：登记当家待拍板事项（d 编号）。
        若 ask_master.at 在当下（<= now + 10 分钟），立刻发 QQ 并等 90 秒秒回重写；
        若 at 在未来（> now + 10 分钟），暂存为定时 idea 并绑定对应世界线段，真正到点才发（中途撤排自动取消），绝不提前剧透。"""
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
        date_text = (moment or now).replace(tzinfo=None).date().isoformat()
        ask_at_raw = str(ask.get("at") or "").strip() if isinstance(ask, dict) else ""
        ask_at = parse_time(norm_time(ask_at_raw, date_text)) if ask_at_raw else None
        starts = [x.get("start") for x in tasks.values() if isinstance(x.get("start"), datetime.datetime)]
        when = ask_at or (max(now, min(starts)) if starts else now)
        is_future_ask = ask_at is not None and ask_at > now + datetime.timedelta(minutes=10)
        is_resting = bool(companion is not None and companion.safe("resting", now, default=False))

        # decision（请当家主人拍板）：按 topic 分栏登记；未来时刻或主人睡着时先存为 idea，当下且醒着时立即发消息
        topic = normalize_decision_topic(ask.get("topic") if isinstance(ask, dict) else "")
        dec = companion.safe(
            "open_decision", who, question, when, sent=False, topic=topic,
            ask_after=fmt_time(ask_at) if is_future_ask else "",
        ) if companion is not None else None
        dec_id = str((dec or {}).get("id") or "")
        if is_future_ask:
            if dec_id:
                self._deferred_decision = (dec_id, who, ask_at)
            logger.info("[super_enhance/world] 请示 %s 排在未来 %s（%s），先暂存为定时 idea 到点再发，绝不提前剧透",
                        dec_id, fmt_time(ask_at)[11:16], question[:50])
            return data, text
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
        asked = await self._wait_asked(umo, since)
        if asked is None:
            logger.info(
                "[super_enhance/world] %s 请示 %s 未在等待窗内确认发出，暂不标为 asked（保留 idea 状态，不占配额）",
                self.label(who) or who, dec_id,
            )
            return data, text
        if companion is not None and dec_id:
            companion.safe("mark_decision_asked", dec_id, self._now().replace(tzinfo=None))
        replies = await self._wait_replies(umo, float(asked.get("ts") or since))
        name = self.label(who) or who
        if not replies:
            logger.info("[super_enhance/world] %s 已向主人发出请示 %s「%s」，主人暂未回话，保持挂起等主人发话（绝不擅自替主人做主）",
                        name, dec_id, clean_text(asked.get("text"), 60))
            return data, text
        said = "；".join("%s「%s」" % (x.get("time") or "", clean_text(x.get("text"), 200)) for x in replies)
        note = (
            "%s %s 向当家主人请示了：「%s」。主人刚才回了：%s。\n"
            "【严禁把反问、追问或拒绝曲解成同意！】请先如实判断主人这句回话的态度，在顶层 JSON 必填字段 `\"master_reply_verdict\"`（严格三选一）：\n"
            "- `\"approved\"`：主人明确同意、拍板准许或定下怎么买/怎么干；照主人定下的话重新写出执行后的整份 JSON，`ask_master` 留空；\n"
            "- `\"rejected\"`：主人明确拒绝、否决、叫停或改期（如不行/别买/太贵了/先不去）；照不买/不办重新写出改后的整份 JSON，`ask_master` 留空；\n"
            "- `\"pending\"`：主人只是反问、追问情况（如「你还有多少钱」「什么时候」「怎么回事」）或还没给准话；保持待拍板，勿擅自执行原请示事项。"
            % (name, asked.get("time") or "", clean_text(asked.get("text"), 200), said)
        )
        logger.info("[super_enhance/world] 收到主人 90s 内回话，交由补写判定态度：%s", said[:120])
        retry = prompt + "\n\n你上一次的输出：\n" + str(text or "").strip() + "\n\n" + note
        try:
            again, again_thought = await llm.text_chat(
                self.context, retry, provider_id=str(self.cfg.get("llm_provider_id") or "").strip(),
                timeout=self.timeout, session_id="super_enhance_lifestep", system_prompt=system_prompt,
                attempts=2, log_label="补写判定主人回话并重写",
            )
        except Exception as e:
            logger.warning("[super_enhance/world] 补写判定主人回话失败，保持请示挂起交由记录员接续：%s", e)
            return data, text
        parsed_again = llm.extract_json(again)
        verdict = str((parsed_again or {}).get("master_reply_verdict") or "").strip().lower() if isinstance(parsed_again, dict) else ""
        if verdict not in ("approved", "rejected"):
            logger.info(
                "[super_enhance/world] 主人回话「%s」判定为 %s（反问/未拍板），保持请示 %s 挂起，绝不擅自执行",
                said[:80], verdict or "pending", dec_id,
            )
            return data, text
        self._run_cache = {}
        val_moment = moment or self._now().replace(tzinfo=None)
        is_approved = verdict == "approved"
        fixed, again, _t, errs = await self._validate_with_retries(
            retry, again, again_thought, tasks, val_moment,
            "回话重写(准许)" if is_approved else "回话重写(否决)",
            master_replied=is_approved, system_prompt=system_prompt,
        )
        if fixed is None:
            logger.warning("[super_enhance/world] 补写照主人回话重写未通过结构化校验（%s），保持请示挂起交由记录员接续", "；".join(errs[:3]))
            return data, text
        if companion is not None and dec_id:
            # 暂存待结案信息，等 apply 真正成功落盘后再写 resolved/rejected，防止在途竞态放弃落盘导致永久吞单
            self._pending_resolved_decision = (dec_id, is_approved, said[:200])
        if is_approved:
            self._last_ask_approved = True
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

    def _resolve_open_end(
        self,
        bot: str,
        info: dict,
        end_current: dict,
        raw_list: list,
        date_text: str,
        flat: datetime.datetime,
    ) -> tuple[datetime.datetime, bool]:
        """统一计算 open 模式下当前开口段的结束时刻（validate_payload 与 _apply 共用）。
        按 cur_start 封顶，但绝不截到当前时刻 flat 之前（至少延续到 floor = flat + 5 分钟，
        消除超 4 小时开口段被截到 now 之前导致 world.current(bot, now) is None 的时间真空）。"""
        cur = info.get("cur") if isinstance(info.get("cur"), dict) else {}
        cur_start = parse_time(cur.get("start")) or flat
        end = parse_time(norm_time(end_current.get(bot), date_text)) if end_current.get(bot) else None
        if end is None and raw_list and isinstance(raw_list[0], dict):
            end = parse_time(norm_time(raw_list[0].get("start"), date_text))
        has_end = end is not None
        floor = flat + datetime.timedelta(minutes=5)
        if end is None or end < floor:
            end = floor
        capped = self._limit(cur_start, end, is_sleep(cur))
        resolved = max(capped, floor)
        if resolved <= cur_start:
            resolved = cur_start + datetime.timedelta(minutes=1)
        return resolved, has_end

    def _collect_timed_master_info(self, date_text: str) -> tuple[set[str], list[int]]:
        """提取当天（date 为空或等于 date_text）带具体钟点的主人相关打算编号与约定分钟列表，
        严格过滤非当天日期的打算，避免拿未来日期的打算放行今天带主人出门。"""
        timed_ids: set[str] = set()
        timed_minutes: list[int] = []
        for it in (self.world.intentions() or []):
            it_date = str(it.get("date") or "").strip()[:10]
            if it_date and it_date != date_text:
                continue
            win_txt = str(it.get("window") or "").strip()
            has_clock = bool(re.search(r"\d{1,2}:\d{2}|\d{1,2}\s*点|[一二两三四五六七八九十]+点", win_txt))
            if has_clock and (str(it.get("who") or "") == "master" or "master" in (it.get("with") or [])):
                if it.get("id"):
                    timed_ids.add(str(it["id"]))
                for hm in re.findall(r"(\d{1,2}):(\d{2})", win_txt):
                    timed_minutes.append(int(hm[0]) * 60 + int(hm[1]))
        book = getattr(self, "promises", None)
        if book is not None:
            try:
                for pr in (book.items() or []):
                    if str(pr.get("status") or "") != "open" or str(pr.get("date") or "") != date_text:
                        continue
                    pt = str(pr.get("time") or "").strip()
                    m_hm = re.search(r"(\d{1,2}):(\d{2})", pt)
                    if m_hm:
                        timed_minutes.append(int(m_hm.group(1)) * 60 + int(m_hm.group(2)))
            except Exception:
                pass
        return timed_ids, timed_minutes

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
                has_master = "master" in [str(x) for x in (raw.get("with") or [])]
                for partner in partners:
                    p_lbl = self.label(partner) or partner
                    matched = False
                    master_mismatch = False
                    if partner in kept_by_bot:
                        for _p_idx, p_raw, p_start, p_end, _ in kept_by_bot[partner][0]:
                            p_with = [str(x) for x in (p_raw.get("with") or [])]
                            if p_start == seg_start and p_end == seg_end and bot in p_with:
                                matched = True
                                if has_master != ("master" in p_with):
                                    master_mismatch = True
                                break
                    if master_mismatch:
                        errors.append(
                            f"{who_lbl}第{idx}段（{fmt_time(seg_start)[11:16]}–{fmt_time(seg_end)[11:16]}）与 {p_lbl} 同行，"
                            f"但两边 with 里是否含 \"master\" 不一致；同行的两段须保持一致（都在场就都写 master，都不在场就都不写）"
                        )
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
        # 校验主人当前是否已在外面、以及今天是否有明确带钟点的主人出行约定，防止补写擅自把在家的主人带出门
        from . import view as view_mod
        m_where = None
        try:
            m_where = self.world.master_where(flat)
        except Exception:
            m_where = None
        master_outside_now = (
            view_mod._is_outside_place(m_where.get("place"), m_where.get("at_home"), self.home)
            if m_where is not None else False
        )
        timed_master_intent_ids, timed_master_minutes = self._collect_timed_master_info(date_text)

        def _valid_place_struct(val) -> bool:
            if isinstance(val, dict):
                return bool(clean_text(val.get("area"), 20) and clean_text(val.get("at"), 20))
            if isinstance(val, str):
                parts = [p.strip() for p in val.split("·") if p.strip()]
                return len(parts) >= 2
            return False

        def _wood_ready_weather(eff: dict, is_new: bool, hit_item: dict | None = None) -> bool:
            w_cond: dict = {}
            if not is_new and isinstance(hit_item, dict):
                w_cond.update(weathering.clean_conditions(hit_item.get("weather")))
                for stg in (hit_item.get("stages") or []):
                    if isinstance(stg, dict) and stg.get("weather"):
                        w_cond.update(weathering.clean_conditions(stg.get("weather")))
                sim_w = hit_item.get("_weather")
                if isinstance(sim_w, dict):
                    if isinstance(sim_w.get("wet"), (int, float)):
                        w_cond["wet"] = float(sim_w["wet"])
                    if isinstance(sim_w.get("fuel_wet"), (int, float)):
                        w_cond["fuel_wet"] = float(sim_w["fuel_wet"])
            w_cond.update(weathering.clean_conditions(eff.get("weather")))
            eff_kind = re.sub(r"\s+", "", str((eff.get("kind") if is_new else (hit_item.get("kind") if isinstance(hit_item, dict) else "")) or ""))
            is_kindling = w_cond.get("fuel_size") == "kindling" or (not w_cond.get("fuel_size") and eff_kind == "引火料")
            if w_cond.get("split") is False and not is_kindling:
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
        ask_obj = data.get("ask_master")
        ask_at_dt: datetime.datetime | None = None
        if isinstance(ask_obj, dict) and clean_text(ask_obj.get("question"), 160):
            raw_topic = str(ask_obj.get("topic") or "").strip().lower()
            raw_at = str(ask_obj.get("at") or "").strip()
            ask_at_dt = parse_time(norm_time(raw_at, date_text)) if raw_at else None
            if ask_at_dt is None:
                errors.append(
                    "ask_master 必填触发时刻 at（格式 \"YYYY-MM-DD HH:MM\"，写清世界线上几点开口问主人；办完/回来才问的事须填对应时刻，勿留空）"
                )
            elif ask_at_dt < flat - datetime.timedelta(minutes=10):
                errors.append(
                    f"ask_master.at={fmt_time(ask_at_dt)} 不能早于当前时刻（{fmt_time(flat)}）超过 10 分钟"
                )
            if raw_topic not in DECISION_TOPICS:
                errors.append(
                    f"ask_master.topic={ask_obj.get('topic')!r} 非法（必须填 {' / '.join(DECISION_TOPICS)} 六选一）"
                )
            elif raw_topic == "meal" and (ask_at_dt or flat).hour not in (10, 11, 16, 17, 18):
                chk_t = ask_at_dt or flat
                errors.append(
                    f"ask_master.topic='meal' 仅在临近午饭（10:00–12:00）或晚饭（16:00–19:00）时请示，提问时刻（{fmt_time(chk_t)[11:16]}）不在饭前请示时段，请把 ask_master 留空或把 at 调到对应饭前时刻"
                )
            elif comp_ref is not None:
                topic_pend = comp_ref.safe("pending_decisions", flat, topic=raw_topic, default=[]) or []
                total_asked = int(comp_ref.safe("today_ask_count", flat, default=0) or 0)
                topic_asked = int(comp_ref.safe("today_ask_count", flat, topic=raw_topic, default=0) or 0)
                if topic_pend:
                    errors.append(
                        f"ask_master.topic={raw_topic!r} 下已有未回话的待拍板事项（{topic_pend[0].get('id')}），"
                        f"同一话题勿重复请示（ask_master 留空或改问其他无挂起的 topic）"
                    )
                elif total_asked >= GLOBAL_DAILY_ASK_LIMIT:
                    errors.append(
                        f"今日全话题累计请示已达上限 {GLOBAL_DAILY_ASK_LIMIT} 次，本次请勿再填 ask_master（留空即可）"
                    )
                elif topic_asked >= topic_ask_limit(raw_topic):
                    errors.append(
                        f"ask_master.topic={raw_topic!r} 今日请示已达上限 {topic_ask_limit(raw_topic)} 次，请把 ask_master 留空"
                    )
        batch_food_keys: set[str] = set()
        batch_measured_food_keys: set[str] = set()
        batch_item_keys: set[str] = set()
        batch_fuel_keys: set[str] = set()
        batch_wood_tier: dict[str, str] = {}
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
                resolved_open, has_open_end = self._resolve_open_end(
                    bot, info, end_current, raw_list, date_text, flat
                )
                if not has_open_end:
                    errors.append(f"{who_lbl}（{bot}）：手上未定结束时刻的段必须在 end_current.{bot} 给出结束时间")
                start = resolved_open
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
                                if kd_str in weathering.STOVE_WOOD_KINDS or purp_str == "fuel":
                                    batch_fuel_keys.add(item_key)
                                    w_meta = weathering.wood_fuel_meta({"kind": kd_str, "stages": [{"weather": eff.get("weather")}]})
                                    batch_wood_tier[item_key] = w_meta["tier"]
                            else:
                                batch_any["used_item"] = True
                                tid_str = str(eff.get("id") or "").strip()
                                hit_item = shelf.find(tid_str) if (shelf is not None and tid_str) else None
                                is_wood_ref = tid_str in batch_fuel_keys or (hit_item is not None and weathering.fuel_item(hit_item) and not is_charcoal(hit_item))
                                if is_wood_ref:
                                    w_step = weathering.clean_conditions(eff.get("weather"))
                                    prior_tier = batch_wood_tier.get(tid_str)
                                    if prior_tier is None and hit_item is not None:
                                        prior_tier = weathering.wood_fuel_meta(hit_item, seg_end)["tier"]
                                    if w_step.get("fuel_size") == "kindling":
                                        batch_wood_tier[tid_str] = "kindling"
                                    elif w_step.get("fuel_size") == "split" or (
                                        w_step.get("split") is True and prior_tier != "kindling"
                                    ):
                                        batch_wood_tier[tid_str] = "hardwood"
                                    elif prior_tier is not None and tid_str not in batch_wood_tier:
                                        batch_wood_tier[tid_str] = prior_tier
                cursor = seg_end
            kept_by_bot[bot] = (bot_kept, cursor, until)

        # 第一遍结束后，按实际送抵船屋的时刻结算跨人可用时间（防止 A 还在买油/换炭/领煤油路上，B 在家提前时空倒流用上）
        _RETURN_HOME_WORDS = (
            "回船屋", "回到船屋", "回家", "回到家", "回住处", "返回船屋",
            "拎回船屋", "带回船屋", "搬回船屋", "提回船屋", "拿回船屋", "送回船屋", "扛回船屋",
        )

        def _home_arrival_of(b_kept: list, pos_idx: int) -> datetime.datetime | None:
            _i, r0, _s0, e0, _c0 = b_kept[pos_idx]
            if r0.get("at_home") is True:
                return e0
            for _i2, r2, s2, _e2, _c2 in b_kept[pos_idx + 1:]:
                if r2.get("at_home") is True:
                    return s2
            # 若本批末段虽标 at_home=false 但已明确写走回/带回船屋（先剔除「还没回船屋/不回家」等否定），则末段结束时刻即已送抵船屋门口
            _il, rl, _sl, el, _cl = b_kept[-1]
            tail_txt = f"{rl.get('doing') or ''} {rl.get('result') or ''} {rl.get('place') or ''}"
            tail_clean = re.sub(r"(?:还没|未|不|别|暂不|先不)(?:回|回到|返回|拎回|带回|搬回|提回|拿回|送回|扛回)(?:船屋|家|住处)", "", tail_txt)
            if any(w in tail_clean for w in _RETURN_HOME_WORDS):
                return el
            return None

        first_kerosene_at = None
        first_oil_at = None
        first_charcoal_at = None
        first_wood_at = None
        for _b_id, (b_kept, _, _) in kept_by_bot.items():
            for p_i, (_idx, raw, _s, seg_end, st_code) in enumerate(b_kept):
                arr = _home_arrival_of(b_kept, p_i)
                if raw.get("kerosene") is True and arr is not None:
                    if first_kerosene_at is None or arr < first_kerosene_at:
                        first_kerosene_at = arr
                if st_code != "do":
                    continue
                for eff in raw.get("things") or []:
                    if isinstance(eff, dict) and eff.get("new"):
                        if str(eff.get("kind") or "") == "干货调料粮食" and store is not None and store._is_edible_oil({"cat": CAT_FOOD, "name": eff.get("name"), "kind": "干货调料粮食"}):
                            if arr is not None and (first_oil_at is None or arr < first_oil_at):
                                first_oil_at = arr
                for eff in raw.get("items") or []:
                    if isinstance(eff, dict):
                        if eff.get("new"):
                            kd_str = clean_text(eff.get("kind"), 10)
                            purp_str = str(eff.get("purpose") or "").strip()
                            item_probe = {"cat": CAT_ITEM, "name": eff.get("name"), "kind": kd_str}
                            if is_charcoal(item_probe) and arr is not None:
                                if first_charcoal_at is None or arr < first_charcoal_at:
                                    first_charcoal_at = arr
                            if (kd_str in weathering.STOVE_WOOD_KINDS or purp_str == "fuel") and _wood_ready_weather(eff, True):
                                if arr is not None and (first_wood_at is None or arr < first_wood_at):
                                    first_wood_at = arr
                        else:
                            tid_str = str(eff.get("id") or "").strip()
                            hit_item = shelf.find(tid_str) if (shelf is not None and tid_str) else None
                            is_wood_ref = tid_str in batch_fuel_keys or (hit_item is not None and weathering.fuel_item(hit_item) and not is_charcoal(hit_item))
                            if is_wood_ref and _wood_ready_weather(eff, False, hit_item):
                                if arr is not None and (first_wood_at is None or arr < first_wood_at):
                                    first_wood_at = arr

        if ask_at_dt is not None and kept_by_bot:
            max_batch_end = max((fc for _, fc, _ in kept_by_bot.values()), default=flat)
            if ask_at_dt > max_batch_end + datetime.timedelta(minutes=5):
                errors.append(
                    f"ask_master.at={fmt_time(ask_at_dt)} 晚于本批最后一段结束时刻（{fmt_time(max_batch_end)}），请填在本批排程时间范围内"
                )

        # 每位 bot 在自己接续起点的库存视角 + 本批内按时间追踪已消耗完的食物 ID（防同批后段误扣已用完生料）
        bot_shelves: dict[str, object] = {}
        for b_key, b_info in tasks.items():
            b_st = b_info.get("start")
            if store is not None and isinstance(b_st, datetime.datetime):
                try:
                    bot_shelves[b_key] = self._shelf_at(b_st.replace(tzinfo=None))
                except Exception:
                    bot_shelves[b_key] = shelf
            else:
                bot_shelves[b_key] = shelf
        # 按 (bot, 日期) 分日收集已吃/已排正餐，避免跨午夜预排次日早饭被今日早饭误杀
        meal_dates = {flat.date()}
        for _b_k, (b_kept, _, _) in kept_by_bot.items():
            for _idx, _raw, _ss, _se, _sc in b_kept:
                meal_dates.add(_ss.date())
        existing_meals_by_bot_day: dict[tuple[str, str], list[tuple[str, datetime.datetime, datetime.datetime, str]]] = {}
        for m_day in sorted(meal_dates):
            day_meals = self._today_meals_by_bot(list(tasks.keys()), datetime.datetime.combine(m_day, datetime.time(12, 0)))
            d_iso = m_day.isoformat()
            for mb, mrows in day_meals.items():
                existing_meals_by_bot_day[(mb, d_iso)] = list(mrows)
        batch_food_ended_at: dict[str, datetime.datetime] = {}
        batch_item_consumed_at: dict[str, datetime.datetime] = {}
        for _b_key, (b_kept, _, _) in kept_by_bot.items():
            for _idx, _raw, _ss, _se, _sc in b_kept:
                if _sc != "do":
                    continue
                for _eff in (_raw.get("things") or []):
                    if not isinstance(_eff, dict) or _eff.get("new"):
                        continue
                    _tid = str(_eff.get("id") or "").strip()
                    _w = norm_step(_eff.get("what"), CAT_FOOD)
                    if _tid and _w in END_STEPS:
                        if _tid not in batch_food_ended_at or _se < batch_food_ended_at[_tid]:
                            batch_food_ended_at[_tid] = _se
                for _eff in (_raw.get("items") or []):
                    if not isinstance(_eff, dict) or _eff.get("new"):
                        continue
                    _tid = str(_eff.get("id") or "").strip()
                    _w = norm_step(_eff.get("what"), CAT_ITEM)
                    if _tid and _w in ("用了", "用完了", "卖了", "换出去了", "丢了", "坏了扔了", "送人了"):
                        if _tid not in batch_item_consumed_at or _ss < batch_item_consumed_at[_tid]:
                            batch_item_consumed_at[_tid] = _ss

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
            # 检查本轮开始前，这位 bot 是否刚和主人一起在外头（with 含 master 且 at_home=false）
            bot_master_out_with_me = False
            bot_master_route_had_master = False
            if bot_kept and master_outside_now:
                prev_bot_acts = [
                    x for x in self.world.activities(bot, bot_kept[0][2])
                    if not real_stop(x) and (parse_time(x.get("start")) is not None) and parse_time(x.get("start")) < bot_kept[0][2]
                ]
                if prev_bot_acts:
                    last_b = prev_bot_acts[-1]
                    if (
                        view_mod._is_outside_place(last_b.get("place"), last_b.get("at_home"), self.home)
                        and "master" in [str(x) for x in (last_b.get("with") or [])]
                    ):
                        bot_master_out_with_me = True
                        bot_master_route_had_master = True
            bot_has_food_step = False
            bot_used_cands: set[str] = set()
            bot_has_wood_stove = False
            bot_has_partner = False
            bot_resolved_origins: list[dict] = []
            bot_self_filler_cnt = 0

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
                if st_code == "do" and raw.get("meal") is True:
                    cur_slot = self._meal_slot_of(seg_start)
                    if cur_slot in ("早饭", "午饭", "晚饭"):
                        meal_day_key = (bot, seg_start.date().isoformat())
                        prior_meal = next(
                            (
                                (p_st, p_en)
                                for p_sl, p_st, p_en, _ in existing_meals_by_bot_day.get(meal_day_key, [])
                                if p_sl == cur_slot and (
                                    (seg_start - p_en).total_seconds() > 10 * 60
                                    or (p_st - seg_end).total_seconds() > 10 * 60
                                )
                            ),
                            None,
                        )
                        if prior_meal is not None:
                            day_lbl = "今日" if seg_start.date() == flat.date() else seg_start.strftime("%m-%d ")
                            errors.append(
                                f"{tag}：{who_lbl}{day_lbl}{cur_slot}已在 {fmt_time(prior_meal[0])[11:16]}–{fmt_time(prior_meal[1])[11:16]} 吃过，"
                                f"严禁重吃第二顿{cur_slot}（若陪主人吃另做的饭请将 meal 填 false）"
                            )
                        else:
                            existing_meals_by_bot_day.setdefault(meal_day_key, []).append(
                                (cur_slot, seg_start, seg_end, str(raw.get("doing") or ""))
                            )
                if raw.get("with"):
                    bot_has_partner = True
                # 防擅自把在家主人带出门 & 防主人已外出时仍在家写 with: master
                with_ids = [str(x) for x in (raw.get("with") or [])]
                if (
                    raw.get("at_home") is True
                    and "master" in with_ids
                    and master_outside_now
                    and not master_replied
                ):
                    brought_home_earlier = any(
                        p_raw.get("at_home") is False and "master" in [str(x) for x in (p_raw.get("with") or [])]
                        for _, (other_kept, _, _) in kept_by_bot.items()
                        for _, p_raw, p_s, _, _ in other_kept
                        if p_s < seg_start
                    )
                    if not brought_home_earlier:
                        errors.append(
                            f"{tag}：主人眼下已外出不在住处，在家的段（at_home=true）严禁把 \"master\" 写进 with！"
                        )
                if (
                    raw.get("at_home") is False
                    and "master" in with_ids
                    and not master_replied
                    and not master_outside_now
                ):
                    raw_i = str(raw.get("intent") or "").strip()
                    seg_min = seg_start.hour * 60 + seg_start.minute
                    near_timed = any(abs(seg_min - tm) <= 20 for tm in timed_master_minutes)
                    prior_master_out = any(
                        p_raw.get("at_home") is False and "master" in [str(x) for x in (p_raw.get("with") or [])]
                        for _, (other_kept, _, _) in kept_by_bot.items()
                        for _, p_raw, p_s, _, _ in other_kept
                        if p_s < seg_start
                    )
                    if not (raw_i in timed_master_intent_ids or near_timed or prior_master_out):
                        errors.append(
                            f"{tag}：主人眼下还在住处里、也未到约好具体钟点的出发时刻，"
                            f"补写严禁擅自写 at_home=false 且 with 含 \"master\" 强行把主人带出门！"
                            f"请改写为在家准备并通知/用 ask_master 问主人几点出发，或自己去办（with 勿写 master）"
                        )
                # 带主人出门必须带回来：回家的路途段（at_home=false）必须把 master 写进 with；
                # 带回到家即止——到家之后（at_home=true）的段做各自的事，不再要求捎 master
                if raw.get("at_home") is False:
                    if "master" in with_ids:
                        bot_master_out_with_me = True
                        bot_master_route_had_master = True
                    elif bot_master_out_with_me:
                        # 这趟出门带了主人，但最近一段在外头没带他：回程到目前为止还没带
                        bot_master_route_had_master = False
                elif bot_master_out_with_me and raw.get("at_home") is True:
                    if not bot_master_route_had_master:
                        errors.append(
                            f"{tag}：前面带主人（with 含 \"master\"）一起出了门，但回家的路途段都没把 \"master\" 写进 with——主人被丢在外面了！"
                            f"回来的路上必须带着 master，到家之后各做各的事就不用再捎"
                        )
                    # 这趟回家已判定完毕（回程带了就行）；重置状态，避免连坐误报后续独自出门回家的段
                    bot_master_out_with_me = False
                    bot_master_route_had_master = False
                if seaside and raw.get("lamp") is True:
                    if sleep:
                        errors.append(f"{tag}：睡觉段 lamp 必须为 false")
                    elif not (
                        inv_state.get("lamp_ok")
                        or (raw.get("kerosene") is True and raw.get("at_home") is True)
                        or (first_kerosene_at is not None and first_kerosene_at <= seg_start)
                    ):
                        errors.append(f"{tag}：家里煤油已点完且在此之前未把新煤油带回船屋，lamp 不能填 true")

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

                # 本段在家现场劈好干柴/取得闷炭/取得食用油时，允许同段自用不自锁（跨人仍受 _home_arrival_of 约束）
                self_got_charcoal = False
                self_got_wood = False
                self_got_oil = False
                if st_code == "do" and raw.get("at_home") is True:
                    for _e in (raw.get("things") or []):
                        if isinstance(_e, dict) and _e.get("new") and str(_e.get("kind") or "") == "干货调料粮食":
                            if store is not None and store._is_edible_oil({"cat": CAT_FOOD, "name": _e.get("name"), "kind": "干货调料粮食"}):
                                self_got_oil = True
                    for _e in (raw.get("items") or []):
                        if not isinstance(_e, dict):
                            continue
                        if _e.get("new"):
                            _kd = clean_text(_e.get("kind"), 10)
                            _purp = str(_e.get("purpose") or "").strip()
                            if is_charcoal({"cat": CAT_ITEM, "name": _e.get("name"), "kind": _kd}):
                                self_got_charcoal = True
                            if (_kd in weathering.STOVE_WOOD_KINDS or _purp == "fuel") and _wood_ready_weather(_e, True):
                                self_got_wood = True
                        else:
                            _tid = str(_e.get("id") or "").strip()
                            _hit = shelf.find(_tid) if (shelf is not None and _tid) else None
                            _is_w = _tid in batch_fuel_keys or (_hit is not None and weathering.fuel_item(_hit) and not is_charcoal(_hit))
                            if _is_w and _wood_ready_weather(_e, False, _hit):
                                self_got_wood = True

                if "stove" not in raw or not isinstance(raw.get("stove"), str) or raw.get("stove") not in ("", "charcoal", "wood"):
                    errors.append(f"{tag}：stove 必须显式填 \"\"、\"charcoal\" 或 \"wood\" 三选一（当前值={raw.get('stove')!r}）")
                    stove_val = ""
                else:
                    stove_val = raw["stove"]
                    if not seaside and stove_val != "":
                        errors.append(f"{tag}：非海边住处 stove 必须填 \"\"")
                    elif stove_val != "" and raw.get("at_home") is False:
                        errors.append(f"{tag}：外出段（at_home=false）不在船屋，不能由本人标生火，stove 必须填 \"\"")
                    elif stove_val == "charcoal" and inv_state["charcoal_g"] <= 0 and not self_got_charcoal and not (first_charcoal_at is not None and first_charcoal_at <= seg_start):
                        errors.append(f"{tag}：家里闷炭现余 0 克且在此之前未把闷炭带回船屋，stove 严禁填 \"charcoal\"")
                    elif stove_val == "wood" and inv_state["wood_kg"] <= 0 and not self_got_wood and not (first_wood_at is not None and first_wood_at <= seg_start):
                        errors.append(f"{tag}：家里干柴为 0 公斤且在此之前未把干柴带回船屋，stove 严禁填 \"wood\"")
                    if stove_val == "wood":
                        bot_has_wood_stove = True

                raw_intent = str(raw.get("intent") or "").strip()
                raw_training = str(raw.get("training") or "").strip()
                intent_id = "" if idle else self.intent_id(raw_intent, bot, with_ids)
                training_id = self.training_id(raw_training, bot)
                if raw_intent and not intent_id:
                    errors.append(f"{tag}：intent={raw_intent!r} 不是 {who_lbl} 当前有效的打算编号")
                if raw_training and not training_id:
                    errors.append(f"{tag}：training={raw_training!r} 不是当前有效的在练编号")

                raw_from = str(raw.get("from") or "").strip()
                orig = origin_of(raw_from, bot, seed, intent=intent_id, training=training_id, pools_data=pools_data, cand_map=cand_map)
                cand_key = raw_from.strip("[]【】()（）").upper()
                cand_entry = cand_map.get(cand_key) if isinstance(cand_map, dict) else None
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
                    if st_code == "do" and orig["kind"] in ("twist", "night_twist", "together", "pastime"):
                        p_info = pool_meta.get(orig["text"])
                        if isinstance(p_info, dict):
                            seg_mins = round((seg_end - seg_start).total_seconds() / 60.0)
                            if p_info.get("short"):
                                if seg_mins > 15:
                                    errors.append(
                                        f"{tag}：候选「{orig['text'][:18]}…」标为短事（5~10分钟），写了 {seg_mins} 分钟过长，"
                                        f"请压缩为 1~2 步、总计 ≤15 分钟（勿把拿放归位硬凑时间）"
                                    )
                            elif p_info.get("big"):
                                if until is None and seg_mins < 45:
                                    errors.append(
                                        f"{tag}：候选「{orig['text'][:18]}…」标为大活（可干半天~一整天），只写了 {seg_mins} 分钟过短，"
                                        f"请扎实展开 ≥45 分钟（可写 1.5~3.5 小时或跨多段用 continue 接着干透）"
                                    )
                            elif until is None and seg_mins < 15:
                                errors.append(
                                    f"{tag}：候选「{orig['text'][:18]}…」不是短事，只写了 {seg_mins} 分钟过于敷衍，"
                                    f"请按常理写足 ≥15 分钟"
                                )
                    # 落盘前按该段实际排出的 [seg_start, seg_end] 二次复核气象门禁，杜绝变天期把大雾/雨雪插曲错排到已散雾/晴朗钟点
                    if orig["kind"] in ("twist", "night_twist", "together", "pastime", "idle", "night_idle"):
                        p_wx = pool_meta.get(orig["text"])
                        if isinstance(p_wx, dict):
                            wx_err = check_item_wx_at(
                                p_wx,
                                start=seg_start,
                                end=seg_end,
                                home=self.home,
                                weather_of=self._weather_text_of(),
                            )
                            if wx_err:
                                wx_h = str(((seed or {}).get("wx_hints") or {}).get(orig["text"]) or "").strip()
                                tip = f"（该候选{wx_h}）" if wx_h else ""
                                errors.append(
                                    f"{tag}：候选「{orig['text'][:18]}…」{wx_err}{tip}，请改排到对应天气时段或换选当前时段可用的候选"
                                )
                    elif orig["kind"] == "accident":
                        acc_err = check_accident_wx_at(
                            orig["text"],
                            start=seg_start,
                            end=seg_end,
                            home=self.home,
                            weather_of=self._weather_text_of(),
                        )
                        if acc_err:
                            errors.append(f"{tag}：意外「{orig['text'][:18]}…」{acc_err}，请改排到合适时段或本轮不写该意外")
                    if (
                        st_code == "do"
                        and orig["kind"] == "self"
                        and raw.get("meal") is not True
                        and not raw.get("stove")
                        and raw.get("water") is not True
                        and raw.get("kerosene") is not True
                        and not (raw.get("things") or [])
                        and not (raw.get("money") or [])
                        and not any(
                            isinstance(e, dict) and (e.get("new") or norm_step(e.get("what"), CAT_ITEM) in ITEM_END_STEPS)
                            for e in (raw.get("items") or [])
                        )
                    ):
                        bot_self_filler_cnt += 1

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
                                has_oil_now = (
                                    (inv_state["oil_ml"] is None or inv_state["oil_ml"] > 0)
                                    or self_got_oil
                                    or (first_oil_at is not None and first_oil_at <= seg_start)
                                )
                                if ck == "fry" and not has_oil_now:
                                    errors.append(f"{tag} things[{t_i}]（{nm}）：家里食用油现余 0 毫升且在此之前未把油带回船屋，cook 严禁填 \"fry\"")
                                if seaside and raw.get("at_home") is True and ck in ("boil", "fry", "steam", "roast") and stove_val not in ("charcoal", "wood"):
                                    # 若紧挨着的前一段是已开火的 chat 做菜段（记录员未建菜品、由本段盛盘入库），放行不强逼重复填 stove
                                    prev_chat_cooked = False
                                    if idx == 1:
                                        prev_e = self.world.latest_for(bot, before=fmt_time(seg_start))
                                        if prev_e and str(prev_e.get("source") or "") == "chat" and str(prev_e.get("stove") or "") in ("charcoal", "wood"):
                                            pe_end = parse_time(prev_e.get("end"))
                                            if pe_end is not None and 0 <= (seg_start - pe_end).total_seconds() <= 5 * 60:
                                                prev_chat_cooked = True
                                    if not prev_chat_cooked:
                                        errors.append(f"{tag} things[{t_i}]（{nm}）：在船屋做好热食（cook=\"{ck}\"）时，该段 stove 必须填 \"charcoal\" 或 \"wood\"，不能填 \"\"")
                    else:
                        tid = str(eff.get("id") or "").strip()
                        cur_shelf = bot_shelves.get(bot, shelf)
                        shelf_food = cur_shelf.find(tid) if (cur_shelf is not None and tid) else None
                        ended_before = tid in batch_food_ended_at and batch_food_ended_at[tid] <= seg_start
                        found = (not ended_before) and (
                            tid in batch_food_keys
                            or ((shelf_food is not None and is_ended(shelf_food) is None) if cur_shelf is not None else bool(tid))
                        )
                        if not tid or not found:
                            errors.append(f"{tag} things[{t_i}]：id={tid!r} 在当前可用库存或本轮新建代号中不存在（或早已吃完用完）")
                        if what not in MID_STEPS + END_STEPS:
                            errors.append(f"{tag} things[{t_i}]（{tid}）：已有食物的 what={eff.get('what')!r} 不合法")
                        if what in ("用完了", "吃完了") and str(eff.get("left") or "").strip():
                            errors.append(f"{tag} things[{t_i}]（{tid}）：what=\"{what}\" 时 left 必须填空串 \"\"")
                        if what == "油封" and inv_state["oil_ml"] is not None and inv_state["oil_ml"] < 60 and not self_got_oil and not (first_oil_at is not None and first_oil_at <= seg_start):
                            errors.append(f"{tag} things[{t_i}]（{tid}）：家里食用油不足 60 毫升且在此之前未把油带回船屋，不能做 \"油封\"")
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
                        errors.append(f"{tag} items[{i_i}]：used={used_txt!r} 必须为纯数字+单位（如 \"1.5公斤\"、\"200克\"），严禁模糊中文或分数")
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
                        if is_new_wood:
                            amt_txt = str(eff.get("amount") or "").strip()
                            if not amt_txt or not is_strict_weight_qty(amt_txt):
                                errors.append(f"{tag} items[{i_i}]（{nm}）：新入库炉柴 amount={amt_txt!r} 必须按重量填纯数字+公斤（如 \"15公斤\"、\"80公斤\"），严禁用 \"捆\" 或模糊量词")
                            left_txt = str(eff.get("left") or "").strip()
                            if left_txt and not is_strict_weight_qty(left_txt):
                                errors.append(f"{tag} items[{i_i}]（{nm}）：新入库炉柴 left={left_txt!r} 必须按重量填纯数字+公斤")
                        elif is_new_charcoal:
                            amt_txt = str(eff.get("amount") or "").strip()
                            if not amt_txt or not is_strict_weight_qty(amt_txt):
                                errors.append(f"{tag} items[{i_i}]（{nm}）：新入库闷炭必须填精确数字+克数的 amount（如 \"400克\"），严禁留空或写模糊量词")
                            left_txt = str(eff.get("left") or "").strip()
                            if left_txt and not is_strict_weight_qty(left_txt):
                                errors.append(f"{tag} items[{i_i}]（{nm}）：新入库闷炭 left={left_txt!r} 必须为纯数字+重量单位")
                        if is_new_wood:
                            w_decl = weathering.clean_conditions(eff.get("weather"))
                            if not w_decl.get("fuel_source"):
                                errors.append(
                                    f"{tag} items[{i_i}]（{nm}）：新入库炉柴必须在 weather 中写明结构化来源 fuel_source"
                                    f"（{' / '.join(weathering.CONDITION_VALUES['fuel_source'])}）"
                                )
                            if not w_decl.get("fuel_size"):
                                errors.append(
                                    f"{tag} items[{i_i}]（{nm}）：新入库炉柴必须在 weather 中写明结构化规格 fuel_size"
                                    f"（\"kindling\" 细枯枝引火料 / \"split\" 已劈硬木粗柴 / \"log\" 整段未劈原木）"
                                )
                            elif w_decl.get("fuel_size") == "log" and "split" not in w_decl:
                                errors.append(
                                    f"{tag} items[{i_i}]（{nm}）：整段原木 fuel_size=\"log\" 时请在 weather 中显式填 split（未劈填 false，劈好可进炉改 true）"
                                )
                        if is_new_charcoal:
                            from_tokens = [str(x).strip() for x in (eff.get("from") or []) if str(x).strip()]
                            is_stove_charcoal = (what in ("做好了", "开始做")) or bool(from_tokens)
                            if is_stove_charcoal:
                                if st_code != "do" or raw.get("at_home") is not True or raw.get("stove") != "wood":
                                    errors.append(
                                        f"{tag} items[{i_i}]（{nm}）：自家柴炉手动夹出闷炭必须在同段点着柴炉"
                                        f"（at_home=true 且 stove=\"wood\"）；若是外出买/换回的炭请用 what=\"买回来\"/\"收到\" 且 from=[]"
                                    )
                                if not from_tokens:
                                    errors.append(
                                        f"{tag} items[{i_i}]（{nm}）：自家柴炉夹出闷炭必须在 from 中填入正在烧的硬木粗柴编号（如 [\"p290\"]），严禁空手凭空产炭"
                                    )
                                for tok in from_tokens:
                                    tok_ok = batch_wood_tier.get(tok) == "hardwood"
                                    if not tok_ok and shelf is not None:
                                        s_hit = shelf.find(tok)
                                        if s_hit is not None and weathering.is_charcoal_wood(s_hit, seg_end):
                                            tok_ok = True
                                    if not tok_ok:
                                        errors.append(
                                            f"{tag} items[{i_i}]（{nm}）：from 中的 {tok!r} 不是结构化标明为硬木粗柴（weather.fuel_size=\"split\"）的炉柴——"
                                            f"细枯枝/引火料/未劈原木烧完只会化灰，绝不能夹出闷炭"
                                        )
                                parsed_c = parse_amount(amt_txt) if amt_txt else None
                                if parsed_c is not None and parsed_c[1] == "克" and parsed_c[0] > 300:
                                    errors.append(
                                        f"{tag} items[{i_i}]（{nm}）：单次柴炉夹红炭闷灭最多攒出约 100~200 克闷炭（上限 300 克），不能写 {amt_txt}"
                                    )
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
                                f"严禁擅自买回入库！本段请只写去打听价钱行情，并在顶层 ask_master 填 kind=\"decision\", topic=\"winter_gear\" 发消息请主人拿主意"
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
                        if seaside and shelf_hit is not None and is_sealed(shelf_hit):
                            w_unseal = weathering.clean_conditions(eff.get("weather"))
                            if w_unseal.get("sealed") is False and not (
                                master_replied or (intent_id and intent_id in chat_intent_ids) or "master" in [str(x) for x in (raw.get("with") or [])]
                            ):
                                errors.append(
                                    f"{tag} items[{i_i}]（{tid}）：该件仍封口未拆（sealed=true），未经当家主人发话拍板或同行拆看，"
                                    f"严禁擅自拆开（请先填 ask_master topic=\"sealed_loot\" 请主人一起看）"
                                )
                        if is_fuel:
                            if used_txt and not is_strict_weight_qty(used_txt):
                                errors.append(f"{tag} items[{i_i}]（{tid}）：燃料 used={used_txt!r} 必须为纯数字+重量单位（炉柴用 \"公斤\" 如 \"1.5公斤\"，闷炭用 \"克\" 如 \"200克\"），严禁用 \"捆\" 或模糊量词")
                            if what in ITEM_END_STEPS and str(eff.get("left") or "").strip():
                                errors.append(f"{tag} items[{i_i}]（{tid}）：what=\"{what}\" 时 left 必须填空串 \"\"")
                            elif what not in ITEM_END_STEPS:
                                for qk in ("amount", "left"):
                                    qv = str(eff.get(qk) or "").strip()
                                    if qv and not is_strict_weight_qty(qv):
                                        errors.append(f"{tag} items[{i_i}]（{tid}）：燃料 {qk}={qv!r} 必须为纯数字+重量单位（炉柴用 \"公斤\"，闷炭用 \"克\"），严禁用 \"捆\" 或模糊量词")

                # 校验：新做物品填了 from 时，对应仍在库的旧料须在本段或前序段写明消耗动作（防止铁罐/旧料做成成品后裂变占位）
                for i_i, eff in enumerate(raw.get("items") or [], 1):
                    if not isinstance(eff, dict) or not eff.get("new"):
                        continue
                    what = norm_step(eff.get("what"), CAT_ITEM)
                    if what not in ("开始做", "做好了"):
                        continue
                    from_list = [str(x).strip() for x in (eff.get("from") or []) if str(x).strip()]
                    for tok in from_list:
                        s_hit = shelf.find(tok) if (shelf is not None and tok) else None
                        # 若旧料已在更早批次标记结束（跨批完工），不重复要求消耗
                        if s_hit is not None and is_ended(s_hit) is not None:
                            continue
                        c_at = batch_item_consumed_at.get(tok)
                        if c_at is None or c_at > seg_end:
                            nm = clean_text(eff.get("name"), 30)
                            errors.append(
                                f"{tag} items[{i_i}]（{nm}）：from 里写了旧料 {tok!r}，但本段或前序段未对 {tok!r} 写消耗动作；"
                                f"请在 items 中为 {tok!r} 补写 what=\"用完了\"（整件合入成品）或 \"用了\"（仅取一部分），避免同一件旧物重复占位"
                            )

            # 强制采纳插曲池校验：本轮给了可用插曲/双人候选时，严禁全靠纯自拟 from="self" 水段跳过池子
            # （本轮已安排睡觉，或主人情绪触发 exempt_forced_twist 允许对症反思补救时豁免）
            bot_has_sleep = any(st_code == "sleep" for _idx, _raw, _s, _e, st_code in bot_kept)
            bot_mood_exempt = False
            if comp_ref is not None:
                all_bots = list(self.raw_log.enabled_bots()) if self.raw_log is not None else list(tasks.keys())
                mv = comp_ref.safe("mood_view_for", bot, flat, bots=all_bots, default=None)
                if isinstance(mv, dict) and mv.get("exempt_forced_twist"):
                    bot_mood_exempt = True
            if isinstance(cand_map, dict) and bot_self_filler_cnt >= 1 and not bot_has_sleep and not bot_mood_exempt:
                avail_twist_codes = [
                    code for code, meta in cand_map.items()
                    if isinstance(meta, dict)
                    and meta.get("kind") in ("twist", "night_twist", "together", "accident", "foiled")
                    and meta.get("bot") in ("", bot)
                ]
                has_twist_pick = any(
                    o["kind"] in ("twist", "night_twist", "together", "accident", "foiled")
                    for o in bot_resolved_origins
                )
                has_pastime_pick = any(o["kind"] == "pastime" for o in bot_resolved_origins)
                if avail_twist_codes and not has_twist_pick and (not has_pastime_pick or bot_self_filler_cnt >= 2):
                    codes_str = " / ".join(avail_twist_codes[:6])
                    errors.append(
                        f"{who_lbl}本轮有空闲时段且写了 {bot_self_filler_cnt} 段纯自拟水段（from=\"self\"），"
                        f"却一条上方插曲/双人候选（{codes_str}）都没采纳；严禁跳过插曲池去编数柴/擦灰/擦玻璃类空转家务，"
                        f"请把其中至少 1 段改为采纳上方候选并填入对应短编号"
                    )

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

        # 物理唯一煤油灯防分裂校验（仅拦一灯同时出现在两处，绝不拦白天点灯）
        if seaside:
            lamp_spans: list[tuple[str, int, datetime.datetime, datetime.datetime, bool, list[str], str]] = []
            for l_bot, (b_kept, _, _) in kept_by_bot.items():
                for l_idx, l_raw, l_s, l_e, _ in b_kept:
                    if l_raw.get("lamp") is True:
                        lamp_spans.append((
                            l_bot, l_idx, l_s, l_e, bool(l_raw.get("at_home")),
                            [str(x) for x in (l_raw.get("with") or [])], str(l_raw.get("place") or ""),
                        ))
            # 仅本批确实有点灯段时，才纳入世界线里对方已有且与本批重叠的 lamp=True 段（含前一天跨午夜段与进行中开口段）
            if lamp_spans:
                span_dates = {s.date() for _, _, s, _, _, _, _ in lamp_spans} | {e.date() for _, _, _, e, _, _, _ in lamp_spans}
                span_dates.add(min(span_dates) - datetime.timedelta(days=1))
                all_days = sorted(d.isoformat() for d in span_dates)
                max_lamp_end = max(e for _, _, _, e, _, _, _ in lamp_spans)
                batch_bots_covered = {b: (bk[0][2], bk[-1][3]) for b, (bk, _, _) in kept_by_bot.items() if bk}
                all_bots = list(self.raw_log.enabled_bots()) if self.raw_log is not None else list(tasks.keys())
                for other_bot in all_bots:
                    cur_open = self.world.current(other_bot, flat)
                    cur_open_key = (
                        (str(cur_open.get("id") or ""), str(cur_open.get("start") or ""))
                        if isinstance(cur_open, dict) else ("", "")
                    )
                    cov = batch_bots_covered.get(other_bot)
                    for d_s in all_days:
                        for ex in self.world.entries(d_s, other_bot):
                            if is_event(ex) or real_stop(ex) or ex.get("lamp") is not True:
                                continue
                            es = parse_time(ex.get("start"))
                            if es is None:
                                continue
                            ee = parse_time(ex.get("end")) if ex.get("end") else None
                            if ee is None:
                                # 仅认该 bot 此刻真正在进行中的开口段（按 id+start 双键匹配，防跨天同名 e1 误撞昨日未封口脏段）
                                if not cur_open_key[0] or (str(ex.get("id") or ""), str(ex.get("start") or "")) != cur_open_key:
                                    continue
                                if cov is not None:
                                    # 若本批 open 模式已用 end_current 截到 cov[0]，按 [es, cov[0]] 纳入校验，不被 cov 跳过
                                    if cov[0] <= es:
                                        continue
                                    ee = cov[0]
                                else:
                                    if es >= max_lamp_end:
                                        continue
                                    ee = max(es + datetime.timedelta(minutes=30), flat, max_lamp_end)
                            else:
                                if cov is not None and not (ee <= cov[0] or es >= cov[1]):
                                    continue
                            ex_at_home = not view_mod._is_outside_place(ex.get("place"), ex.get("at_home"), self.home)
                            lamp_spans.append((
                                other_bot, 0, es, ee, ex_at_home,
                                [str(x) for x in (ex.get("with") or [])], str(ex.get("place") or ""),
                            ))
            reported_lamp_pairs: set[tuple] = set()
            for i_a in range(len(lamp_spans)):
                ba, ia, sa, ea, ha, wa, pa = lamp_spans[i_a]
                for i_b in range(i_a + 1, len(lamp_spans)):
                    bb, ib, sb, eb, hb, wb, pb = lamp_spans[i_b]
                    if (ia == 0 and ib == 0) or ba == bb or max(sa, sb) >= min(ea, eb):
                        continue
                    # 同在船屋里共用一盏灯合法；同行外出共提一盏灯合法
                    if ha and hb:
                        continue
                    if (not ha) and (not hb) and (bb in wa or ba in wb or (pa and pa == pb)):
                        continue
                    pair_key = tuple(sorted([(ba, ia, sa), (bb, ib, sb)]))
                    if pair_key in reported_lamp_pairs:
                        continue
                    reported_lamp_pairs.add(pair_key)
                    la, lb = self.label(ba) or ba, self.label(bb) or bb
                    ov_s, ov_e = max(sa, sb), min(ea, eb)
                    errors.append(
                        f"煤油灯分身冲突（{fmt_time(ov_s)[11:16]}–{fmt_time(ov_e)[11:16]}）：船屋里只有一盏煤油灯，"
                        f"{la}（{'在家' if ha else '外出'}）与 {lb}（{'在家' if hb else '外出'}）不在同一处，不能同时把 lamp 填为 true"
                    )

        money_book = getattr(self, "money", None)
        if money_book is not None:
            norm_segs = {}
            for m_bot, (bot_kept, _, _) in kept_by_bot.items():
                norm_segs[m_bot] = [
                    dict(raw, start=fmt_time(seg_start), intent=self.intent_id(raw.get("intent"), m_bot, [str(x) for x in (raw.get("with") or [])]))
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
                master_replied=master_replied,
            ))

        return errors

    def apply(
        self,
        data: dict,
        tasks: dict,
        now: datetime.datetime,
        *,
        mood_snapshot: dict | None = None,
        master_replied: bool = False,
        world_fp: dict | None = None,
    ):
        self._last_apply_aborted_by_mood = False
        self._last_apply_aborted_by_world = False
        if world_fp is not None:
            flat_now = now.replace(tzinfo=None)
            if self._tasks_world_fingerprint(tasks, flat_now) != world_fp:
                self._last_apply_aborted_by_world = True
                self._cancel_deferred_decision_on_abort()
                logger.info("[super_enhance/world] 补写落盘前目标角色世界线已被改写，放弃过期排程等待重排")
                return {}, []
        guard_before = mood_snapshot if mood_snapshot is not None else getattr(self, "_mood_guard_start", None)
        if guard_before is not None:
            guard_after = self._mood_guard_snapshot(self._guard_now(now))
            drifted, reason = self._mood_guard_drifted(guard_before, guard_after)
            if drifted:
                self._last_apply_aborted_by_mood = True
                self._cancel_deferred_decision_on_abort()
                logger.info(
                    "[super_enhance/world] 补写在途主人情绪已变化（%s），放弃过期排程等待重排",
                    reason,
                )
                return {}, []
        with fact_batch(self):
            return self._apply(data, tasks, now, master_replied=master_replied)

    def _apply(self, data: dict, tasks: dict, now: datetime.datetime, *, master_replied: bool = False):
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
        from . import view as view_mod
        try:
            m_where_apply = self.world.master_where(flat)
        except Exception:
            m_where_apply = None
        master_outside_apply = (
            view_mod._is_outside_place(m_where_apply.get("place"), m_where_apply.get("at_home"), self.home)
            if m_where_apply is not None else False
        )
        timed_master_intent_ids, timed_master_minutes = self._collect_timed_master_info(date_text)

        ask_raw = data.get("ask_master") if isinstance(data.get("ask_master"), dict) else {}
        ask_q = clean_text(ask_raw.get("question"), 160) if ask_raw else ""
        ask_who = str(ask_raw.get("who") or "").strip() if ask_q else ""
        if ask_q and ask_who not in bots:
            ask_who = next((b for b in tasks if b in bots), bots[0] if bots else "")
        ask_at_dt = (
            parse_time(norm_time(str(ask_raw.get("at") or "").strip(), date_text))
            if ask_q and str(ask_raw.get("at") or "").strip() else None
        )

        # 第一遍：先按每人 MAX_SEGMENTS / horizon / until 截断保留段，杜绝截断段提前预占 ID
        kept_plans: dict[str, tuple[list, datetime.datetime, datetime.datetime | None]] = {}
        for bot, info in tasks.items():
            start = info["start"]
            if info["mode"] == "open":
                cur = info["cur"]
                cur_start = parse_time(cur.get("start")) or flat
                raw_list_open = segments.get(bot) if isinstance(segments.get(bot), list) else []
                end, _ = self._resolve_open_end(
                    bot, info, end_current, raw_list_open, date_text, flat
                )
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

        # 第三遍：组装条目、对齐双人同行 master 对称性、规整主动消息与 step_no 夹紧、落库
        def _has_prior_master_out(before_dt: datetime.datetime) -> bool:
            for _ob, (o_plan, _oc, _ou) in kept_plans.items():
                for o_raw, o_start, _o_end, _o_st, _o_id, _o_sl, _o_t, _o_ps in o_plan:
                    if o_start < before_dt and o_raw.get("at_home") is False and "master" in [str(x) for x in (o_raw.get("with") or [])]:
                        return True
            return False

        staged_by_bot: dict[str, tuple[list[dict], list[dict], datetime.datetime, datetime.datetime | None]] = {}
        for bot, (bot_plan, final_cursor, until) in kept_plans.items():
            staged_entries = []
            staged_meta = []
            for raw, seg_start, seg_end, state, idle, sleep, _texts, parsed_steps in bot_plan:
                raw_with = [str(x) for x in (raw.get("with") or [])]
                with_list = [x for x in raw_with if x in bots and x != bot]
                raw_intent = str(raw.get("intent") or "").strip()
                intent = "" if idle else self.intent_id(raw.get("intent"), bot, raw_with)
                if intent in moves and not moves[intent]["done"]:
                    intent = ""
                training = self.training_id(raw.get("training"), bot)
                prev = staged_entries[-1] if staged_entries else self.world.latest_for(bot, before=fmt_time(seg_start))
                prev_with_master = bool(prev) and "master" in (prev.get("with") or []) and not prev.get("sleep")
                prev_out_with_master = bool(prev_with_master and prev.get("at_home") is False)
                origin_val = origin_of(
                    raw.get("from"), bot, seed, intent=intent, training=training,
                    pools_data=pools_data, cand_map=cand_map,
                )
                is_meal_with_master = bool(raw.get("meal") is True and raw.get("at_home") is True and not master_outside_apply)
                is_promise_with_master = origin_val.get("kind") == "promise"
                seg_min = seg_start.hour * 60 + seg_start.minute
                near_timed = any(abs(seg_min - tm) <= 20 for tm in timed_master_minutes)
                prior_master_out = _has_prior_master_out(seg_start)
                brought_home_earlier = prior_master_out
                allow_home_master = not (master_outside_apply and not master_replied and not brought_home_earlier)
                # 主人此刻在睡时：近期（<2h）或深夜/凌晨（<06:00 / >=22:00）的段视为仍在睡；
                # 跨到白天 06:00–22:00 且距今 >=2h 的预排段（如凌晨 04:00 预排 07:30 早饭或 08:00 约定）正常保留合法 master
                is_future_daytime = (
                    6 <= seg_start.hour < 22
                    and (seg_start - flat).total_seconds() >= 2 * 3600
                )
                seg_master_sleeping = bool(master_sleeping and not is_future_daytime)
                if not seg_master_sleeping and not idle and not sleep and "master" in raw_with:
                    if raw.get("at_home") is False:
                        keep_master = bool(
                            master_replied
                            or master_outside_apply
                            or (intent in timed_master_intent_ids)
                            or (raw_intent in timed_master_intent_ids)
                            or near_timed
                            or prior_master_out
                            or prev_out_with_master
                            or self._intent_with_master(intent)
                            or is_promise_with_master
                        )
                    else:
                        keep_master = bool(
                            allow_home_master and (
                                self._intent_with_master(intent)
                                or (intent in timed_master_intent_ids)
                                or (raw_intent in timed_master_intent_ids)
                                or near_timed
                                or prev_with_master
                                or is_meal_with_master
                                or is_promise_with_master
                                or master_replied
                                or brought_home_earlier
                            )
                        )
                    if keep_master:
                        with_list.append("master")
                origin_mood_states = None
                if isinstance(origin_val, dict) and origin_val.get("text"):
                    o_txt = str(origin_val["text"])
                    for p_kind in ("twists", "night_twists", "together", "pastimes", "idles", "night_idles"):
                        for p_row in (pools_data.get(p_kind) or []):
                            if isinstance(p_row, dict) and str(p_row.get("text") or "") == o_txt:
                                origin_mood_states = p_row.get("mood_states") or None
                                break
                        if origin_mood_states:
                            break
                msg_copy = {
                    "send_msg": raw.get("send_msg"),
                    "msg_at": raw.get("msg_at"),
                    "msg_purpose": raw.get("msg_purpose"),
                    "msg_kind": raw.get("msg_kind"),
                    "doing": raw.get("doing"),
                    "result": raw.get("result"),
                    "state": raw.get("state"),
                    "sleep": sleep,
                    "idle": idle,
                    "at_home": raw.get("at_home"),
                    "meal": raw.get("meal"),
                }
                _normalize_proactive_fields(msg_copy, prev_entry=prev, origin_mood_states=origin_mood_states)
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
                stove_val = (
                    raw_stove
                    if raw_stove in ("charcoal", "wood") and raw.get("at_home") is not False and self.home == "海边小镇"
                    else ""
                )
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
                    "msg_kind": msg_copy["msg_kind"],
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
                    "origin": origin_val,
                    "things": things_list,
                    **({"money": money_list} if money_list else {}),
                    "seen_by": [bot] + with_list,
                    "source": "step",
                    "created": fmt_time(flat),
                }
                staged_entries.append(entry)
                staged_meta.append({
                    "raw_with": raw_with,
                    "seg_master_sleeping": seg_master_sleeping,
                    "allow_home_master": allow_home_master,
                })
            staged_by_bot[bot] = (staged_entries, staged_meta, final_cursor, until)

        # 双人同行汇合时同步保留 master，避免一侧保留而另一侧被静默剥离破坏对称性
        for _ in range(max(1, len(staged_by_bot))):
            synced_any = False
            for bot, (staged_entries, staged_meta, _fc, _un) in staged_by_bot.items():
                for entry, meta in zip(staged_entries, staged_meta):
                    if (
                        "master" in entry["with"]
                        or "master" not in meta["raw_with"]
                        or meta["seg_master_sleeping"]
                        or entry.get("idle")
                        or entry.get("sleep")
                    ):
                        continue
                    if entry.get("at_home") is not False and not meta["allow_home_master"]:
                        continue
                    partner_has_master = False
                    for partner in entry["with"]:
                        if partner not in bots or partner == bot:
                            continue
                        if partner in staged_by_bot:
                            for p_entry in staged_by_bot[partner][0]:
                                if (
                                    p_entry["start"] == entry["start"]
                                    and p_entry["end"] == entry["end"]
                                    and bot in p_entry["with"]
                                    and "master" in p_entry["with"]
                                ):
                                    partner_has_master = True
                                    break
                        else:
                            seg_s = parse_time(entry["start"])
                            seg_e = parse_time(entry["end"])
                            if seg_s is not None and seg_e is not None:
                                for d_txt in sorted({seg_s.date().isoformat(), seg_e.date().isoformat()}):
                                    for item in self.world.entries(d_txt, partner):
                                        if is_event(item):
                                            continue
                                        p_w = [str(x) for x in (item.get("with") or [])]
                                        if bot not in p_w or "master" not in p_w:
                                            continue
                                        es = parse_time(item.get("start"))
                                        ee = parse_time(item.get("end")) if item.get("end") else None
                                        if es is not None and es <= seg_s and (ee is None or ee >= seg_e):
                                            partner_has_master = True
                                            break
                                    if partner_has_master:
                                        break
                        if partner_has_master:
                            break
                    if partner_has_master:
                        entry["with"].append("master")
                        if "master" not in entry["seen_by"]:
                            entry["seen_by"].append("master")
                        synced_any = True
            if not synced_any:
                break

        for bot, (staged_entries, _staged_meta, final_cursor, until) in staged_by_bot.items():
            bot_plan = kept_plans[bot][0]
            _sanitize_proactive_nodes(staged_entries)
            if ask_q and ask_who == bot and ask_at_dt is not None:
                for entry in staged_entries:
                    if not entry.get("send_msg"):
                        continue
                    trig = _trigger_time_of(entry)
                    if trig is not None and abs((trig - ask_at_dt).total_seconds()) <= 600:
                        entry["send_msg"] = False
                        entry["msg_at"] = ""
                        entry["msg_purpose"] = ""
                        entry["msg_kind"] = ""
            rows = []
            cursor = tasks[bot]["start"] if not bot_plan else final_cursor
            last_ok_end = tasks[bot]["start"]
            for entry in staged_entries:
                seg_start = parse_time(entry["start"])
                record = self.world.add_entry(seg_start.date().isoformat(), entry)
                if record is None:
                    break
                if entry.get("send_msg") and entry.get("msg_kind"):
                    self.world.patch_entry(
                        seg_start.date().isoformat(),
                        record.get("id"),
                        {"msg_kind": entry["msg_kind"]},
                    )
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
            lbl_map = (
                {b: (self.label(b) or b) for b in (self.raw_log.enabled_bots() or ())}
                if self.raw_log is not None else None
            )
            ctx = build_live_context(
                start=win_start,
                end=max(win_start, flat) + datetime.timedelta(hours=HORIZON_HOURS),
                home=self.home,
                weather_of=self._weather_text_of(),
                world=self.world,
                store=getattr(self, "things", None),
                shelf=shelf,
                money=getattr(self, "money", None),
                companion=getattr(self, "companionship", None),
                labels=lbl_map,
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
            "wood_kg": float(ctx.get("wood_kg") or 0.0) if ctx.get("has_inventory") else 40.0,
            "hardwood_kg": float(ctx.get("hardwood_kg") or 0.0) if ctx.get("has_inventory") else 20.0,
            "oil_ml": float(ctx.get("oil_ml") or 0.0) if ctx.get("has_inventory") else None,
            "ship_unpicked": bool(ctx.get("ship_unpicked")),
            "lamp_ok": lamp_ok,
            "ctx": ctx,
        }

    def _recent_wood_sources(self, win_start: datetime.datetime, days: int = 5) -> list[str]:
        """按结构化 items.weather.fuel_source 查最近几次新弄回炉柴的来源（用于轮换避重）。"""
        out: list[str] = []
        for back in range(days, -1, -1):
            d_str = (win_start.date() - datetime.timedelta(days=back)).isoformat()
            for e in self.world.entries(d_str):
                if real_stop(e):
                    continue
                en_t = parse_time(e.get("start")) or parse_time(e.get("end"))
                if en_t is None or en_t > win_start:
                    continue
                for eff in (e.get("things") or []) + (e.get("items") or []):
                    if not isinstance(eff, dict) or not (eff.get("new") or str(eff.get("what") or "") in ITEM_START_STEPS):
                        continue
                    kd = clean_text(eff.get("kind"), 10)
                    purp = str(eff.get("purpose") or "").strip()
                    if kd not in weathering.STOVE_WOOD_KINDS and purp != "fuel":
                        continue
                    src = weathering.clean_conditions(eff.get("weather")).get("fuel_source")
                    if src:
                        out.append(src)
        return out

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
        if hasattr(self.world, "manual_stoves"):
            for ms in self.world.manual_stoves(win_start.date() - datetime.timedelta(days=2), win_start.date()):
                if str(ms.get("stove") or "").strip().lower() != "wood":
                    continue
                st_t = parse_time(ms.get("start"))
                en_t = parse_time(ms.get("end"))
                if st_t is None or st_t > win_start:
                    continue
                ref_t = min(en_t, win_start) if en_t is not None else st_t
                if ref_t >= cutoff_30h:
                    wood_count_30h += 1
                    if ref_t >= cutoff_4h or (en_t is not None and en_t > win_start):
                        recent_wood = True

        if ctx.get("has_weather"):
            min_temp_val = ctx.get("min_temp")
            min_temp = float(min_temp_val) if min_temp_val is not None else 18.0
            max_rain = float(ctx.get("max_rain") or 0.0)
            evening_or_night = win_start.hour >= 17 or win_end.hour >= 18 or win_start.hour < 6
            freezing_cold = min_temp <= 6.0
            chilly = freezing_cold or (min_temp <= 13.5) or (evening_or_night and min_temp <= 15.5) or (max_rain >= 0.3 and min_temp <= 16.0)
            th_win = weather_mod.cabin_temp_info(win_start, self.world, self._weather_text_of(), self.home)
            cabin_cool = th_win["mode"] != "burning" and th_win["delta"] < 2.5
            recent_sources = self._recent_wood_sources(win_start)
            last_src = recent_sources[-1] if recent_sources else ""
            rot_hint = ""
            if last_src:
                last_lbl = weathering.FUEL_SOURCE_LABELS.get(last_src, last_src)
                rot_hint = f"（上一回刚用过「{last_lbl}」[{last_src}]，这次务必换条门路，严禁连续两次在同一处拾枯枝）"
            hardwood_hint = ""
            if inv["hardwood_kg"] <= 0:
                hardwood_hint = "（家里眼下没有能夹出闷炭的硬木粗柴 `fuel_size=\"split\"`，优先弄硬木劈柴/船坞实木边角回来）"
            wood_ways = (
                "别老只跑坡上薪柴地，按海边地图换着路子弄柴%s%s："
                "① 看码头布告板接镇民清倒树/风断枝短工或去坡上菜地帮菜农剪老果木主枝（得硬木劈柴 `fuel_source=\"pruning\", fuel_size=\"split\"`）；"
                "② 去船坞帮老船匠收场换未刷漆胶合的厚实硬木船料边角（`workshop`, `split`）或干刨花（`kindling`），也可拿海漂木换干柴；"
                "③ 去坡上柴料场与干燥棚买干硬柴、以物换柴或买便宜半湿硬木/原木段回来劈晾烘干（`supplier`, `split`/`log`）；"
                "④ 换去山丘灯塔/旧观景台/旧信号塔山路或坡上薪柴地拾枯落松枝硬枝（`land_deadwood`）；"
                "⑤ 退潮去漂流物小湾高潮线以上拾雨水冲刷晒透的干陆枝（`drift_unknown`）"
                % (rot_hint, hardwood_hint)
            )
            if freezing_cold:
                if cabin_cool and not recent_wood:
                    if inv["wood_kg"] > 0:
                        nudges.append(
                            "- 严寒生炉过冬：屋外已跌至约 %.0f℃，船舱此刻也降到了约 %.0f℃（%s）——早起舱冷、外出吹透海风回来或入夜前，顺着冷意抱干柴把铁桶柴炉生旺暖舱（生火/添柴段必填 \"stove\": \"wood\"），睡前拨小封火门压火留余温。"
                            % (min_temp, th_win["cabin"], th_win["stove_desc"])
                        )
                    else:
                        bed_word = "被褥" if ctx.get("has_thick_quilt") else "薄被和旧毛毯"
                        nudges.append(
                            "- 严寒断柴转折：屋外低至约 %.0f℃、船舱仅约 %.0f℃，冻得想点铁桶柴炉，去摸柴架却发现干柴烧空了——白天无大雨大雪就%s（items 写 new 燃柴入库，amount 写公斤）再生炉；夜深了就紧塞门缝裹紧%s，记着明早头一件事去弄柴。"
                            % (min_temp, th_win["cabin"], wood_ways, bed_word)
                        )
                if not ctx.get("has_winter_gear") and 8 <= win_start.hour <= 16 and max_rain < 1.0:
                    nudges.append(
                        "- 单衣过冬与请示当家人：天已骤冷，船屋里还没有厚冬衣和厚棉被——未经主人点头不许擅自动家底买大件！白天可先去半淹商店街旧衣铺或集市布摊打听好厚棉衣与旧棉被的价钱，并在 ask_master 里填 kind=\"decision\", topic=\"winter_gear\" 报给主人拍板先买哪样。"
                    )
            elif chilly and cabin_cool and not recent_wood:
                if inv["wood_kg"] > 0:
                    nudges.append(
                        "- 天凉生炉暖舱：这阵子船舱返潮发冷（最低约 %.0f℃），别写成全知提前预判，写成坐着觉得手脚发凉时起身抱干柴把铁桶柴炉生起来暖舱（该段必填 \"stove\": \"wood\"），睡前记得压火看炉。"
                        % min_temp
                    )
                else:
                    nudges.append(
                        "- 天凉缺柴转折：这阵子船舱发冷（约 %.0f℃），想生铁桶柴炉去摸干燥架却发现干柴烧空了——若在白天且无大雨就临时%s（items 写 new 燃柴入库，amount 写公斤），夜深了就多裹铺盖、记着明早先去弄柴。"
                        % (min_temp, wood_ways)
                    )
            # 提前留安全余粮（不锁死 6℃，也不被上面生炉分支吞掉）：干柴安全线 30 公斤、闷炭安全线 500 克
            if 7 <= win_start.hour <= 16 and max_rain < 2.5:
                if (chilly or min_temp <= 15.0) and 0 < inv["wood_kg"] < 30.0:
                    unsplit_tip = "屋里已堆着未劈大圆木/大木头，可以直接花大半天一口气劈成硬木劈柴码满棚下；" if ctx.get("has_unsplit_wood") else ""
                    nudges.append(
                        "- 提前囤满过冬大柴垛（安全线 30 公斤）：家里可用干柴现只剩 %.1f 公斤，别等彻底烧光才临时捡几公斤枯枝！%s趁白天没大雨，优先当成一件「大活」集中拉一批大圆木或粗硬木回来、花大半天锯劈码满棚下大柴垛（一次囤足 80~150 公斤够烧十天半月，%s；items 写 new 燃柴入库，amount 写公斤，优先硬木 `fuel_size=\"split\"`）。"
                        % (inv["wood_kg"], unsplit_tip, wood_ways)
                    )
                if 0 < inv["charcoal_g"] < 500.0:
                    nudges.append(
                        "- 提前备足闷炭余粮（安全线 500 克）：灶台闷炭现只剩 %.0f 克，别等做完下一顿彻底见底！趁白天可去煮盐棚帮大半天工换一大罐闷炭、去坡上柴料场问问买炭，或点柴炉烧硬木粗柴未化灰时拿火钳夹红炭闷存（真正到手才在 items 写 new 入库）。"
                        % inv["charcoal_g"]
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

        # 5. 白天公用铁盒余钱 <30 元且 48h 内无进账时，补一条精简提示
        if bool(hours_day & set(range(7, 19))):
            tin_yuan = ctx.get("tin_box_yuan")
            money_book = getattr(self, "money", None)
            snap_48h = None
            if money_book is not None:
                try:
                    snap_48h = money_book.snapshot(win_start, self.world)
                    bal = ((snap_48h.get("accounts") or {}).get("tin_box") or {}).get("balance")
                    if bal is not None:
                        tin_yuan = float(bal)
                except Exception:
                    snap_48h = None
            if tin_yuan is not None and float(tin_yuan) < 30.0:
                cutoff_48h = win_start - datetime.timedelta(hours=48)
                has_earn_48h = False
                for rec in (snap_48h or {}).get("records") or []:
                    if not isinstance(rec, dict) or rec.get("kind") != "earn":
                        continue
                    if str(rec.get("reason") or "").lower() not in ("work", "salvage", "sell"):
                        continue
                    r_t = parse_time(rec.get("at"))
                    if r_t is not None and cutoff_48h <= r_t <= win_start:
                        has_earn_48h = True
                        break
                if not has_earn_48h:
                    nudges.append(
                        "- 铁盒余钱偏紧（仅余约 %s 元且近两天暂无进账）：白天可顺手浅水打捞废铜、扛现有废金属去收购站过秤卖钱（≤%d元自主）或接零活。"
                        % (fmt_yuan(tin_yuan), int(AUTONOMOUS_SELL_LIMIT))
                    )

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
        merged["wx_hints"] = dict(win_seed.get("wx_hints") or {})
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
