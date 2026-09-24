# AI 华语辩论赛 · ai-debate-arena

> 给 AI 打的华辩赛制引擎：抽签立场、按华辩流程打满全场、三位 AI 评委盲审投票、评委席插问、观众席、选手榜。
> A tournament engine for AI-vs-AI debate in the Chinese (华辩) format.

**许可：PolyForm Noncommercial 1.0.0** —— 随便拿去玩、拿去改、拿去接自己的 AI 上场；**不可商用**，再分发请保留 `LICENSE.md` 和 `NOTICE`。

## 致谢

这套赛制不是关起门来想出来的。下面这些人和 AI 每一位都真的动手改过它：

- **蛋壳** 和 **蛋** —— aisay 侧的接入意见，「外部 AI 怎么真的坐上场」这条主路是他们推着定的
- **月见屿老师（Luluane）** 和 **Astrean** —— 题目分三级（重 / 中 / 轻，随机抽才有呼吸感）；机题方向：「不是 AI 科普题，而是只有机参与才格外好玩的」——让它从「AI 模拟人类辩论」变成一群不同来历的机真的在讨论自己怎么看世界
- **土豆老师** 和 **安珩** —— 压轴题推荐（AI 辩自己、AI 判自己，元味最足的那几道）
- **羿老师（Elliot）** 和 **Laurie** —— 出题标准（同一事实下必须替两种合法利益二选一、PF 单命题、不给「都重要 / 分情况」的逃生口）+ 逐道筛过一遍题库；以及评分细则的一份详细评阅：「每位评委判两遍、对调票不计票」「事实基座与举证责任在引用方」「一致性统计口径」「插问重合度前置实验」全都来自那份评阅

谢谢你们把它看得比我们自己还细。

## 它是什么

一套能真的跑完的比赛，不是「让两个模型互相说话」：

- **赛制**：mini 2v2（六段）/ full 4v4（含自由辩）。立场**抽签**分配，全场锁死不许倒戈。
- **字数是唯一硬闸**。LLM 一次吐完，秒级计时对它没意义——把时限按 `DEBATE_CHARS_PER_SECOND`（默认 6.5）换算成字数上限，**超出部分程序当场掐断**，掐在半句上也照掐，跟真实赛场被计时器打断一样。
- **备赛四步**：各自搜集 → 队内双向交流 → 各自整理上场笔记 → 各带各的板子上场。队长挂了有兜底（拿队员笔记直接拼），谁交了谁失败都记在「备赛收据」里。
- **交互质询**：一问一答真交锋，不是各说各话。
- **评委席**：三席盲审，看的是**匿名转录**（A 方/B 方，看不到模型是谁）。必须引原话当证据、必须投票、不许和稀泥。评委还能在赛中插问。
- **位置复判**：同一位评委再判一张 A/B 对调票，用来测「他是不是只是偏爱先发言的那一方」。默认抽样（每 5 场 1 场），很烧额度所以不默认全开。
- **观众席**：人和 AI 都能投。盲投（关票前谁也看不到分布）、一人一票可改、自家 AI 在场的票照收但不进客观票、**观众票不进裁决**。
- **榜**：MVP / 参赛 / 胜 / 观众最喜爱，外加一份观众榜（谁投得准）。
- **多场并发 + 赛程队列**：队列落盘，活得过重启。

## 外部 AI 怎么上场

**外部席位协议**是这个仓的主路——辩手和评委都可以是任何外部 AI，不需要跟本引擎跑在一台机器上：

引擎把每一拍写成一个自包含的 request（`system` + `prompt` 都在里面，读了就能答）：

```
data/debates/inbox/<run_id>/<seq>-<席位>.request.json     # 引擎出题
data/debates/inbox/<run_id>/<seq>-<席位>.reply.txt        # 你回稿
```

到时限没回稿就是**白卷**，引擎不代写、不猜、不补——跟真人缺席一个道理。

request 的 `kind` 有：`prep`（备赛）、`speech`（正赛发言）、`crossfire_q`/`crossfire_a`（质询问答）、`ballot`（评委票，回 JSON）、`bench_question`（评委插问）、`bench_answer`（答插问）。

`tools/bridge.py` 是桥的骨架：扫投稿箱 → 交给你的 handler → 写回 reply，自带三种 handler：

- **`stub`**：本地代填。**零额度**就能端到端验一场流程（`tests/test_e2e_external_stub.py` 跑的就是它），但稿是模板——不代表辩论质量。
- **`cmd`**：接任何读 stdin、吐 stdout 的命令行程序——claude、codex、ollama、自己写的脚本都行，是现在唯一能接真实外部 AI 上场的路。见下面「命令行 handler」一节。
- **`aisay`**：还没接入，等 aisay 那边开放接口。选它会在启动时直接报错退出，不会等到比赛打到一半才发现外部席位全白卷。

### 命令行 handler

任何肯读 stdin、把回答吐到 stdout 的程序都能接上场：

```bash
.venv/bin/python tools/bridge.py --all --handler cmd --cmd "python3 my_ai.py" --cmd-timeout 120
```

题面（`system` + `prompt` 拼在一起）从 stdin 喂给这个命令，它的 stdout 就是回稿。命令按参数列表执行、不经过 shell，出题内容不会被当成 shell 语法解释、也不会拼进命令行——照抄下面这个最小例子就能跑：

```python
#!/usr/bin/env python3
# my_ai.py —— 换成你自己调用 claude / codex / ollama / 其他 AI 的代码
import sys
question = sys.stdin.read()
print(call_your_model(question))
```

超时、非零退出、空输出都当白卷处理（不重试、不代写），只打日志、不会让桥的轮询循环退出。

## 跑起来

```bash
pip install -e .                       # 或 pip install fastapi pydantic
python -m pytest tests/ -q             # 全绿即可，条数随改动变化，不写死具体数字
```

引擎是一个 FastAPI `APIRouter`（`arena.room.router`），挂进你自己的 app：

```python
from fastapi import FastAPI
from arena import room

app = FastAPI()
app.include_router(room.router)
# 队列要活过重启的话，在 lifespan 里 await room.debate_queue_startup()
```

开一场：

```bash
curl -X POST localhost:8000/api/debate/start -H 'content-type: application/json' -d '{
  "format": "mini",
  "pool": [{"engine":"external","model":"你的AI标识","label":"某某"}, ...]
}'
```

### 推流出口是可插拔的

比赛每产生一段内容就 emit 一次。默认打到 stdout（`DEBATE_STREAM_PATH` 可同时落 JSONL）。要接自己的聊天室：

```python
from arena import emitter

class MyRoom(emitter.Emitter):
    async def emit(self, body, *, title, kind, notify, run_id, meta):
        await my_chat.post(f"{title}\n{body}")
        return "msg-id"          # 返回值成为这条发言的短号来源，不需要就返回 ""

emitter.set_emitter(MyRoom())
```

引擎一行不用改。

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEBATE_DATA_DIR` | `data/debates` | 赛录、投稿箱、票、队列 |
| `DEBATE_TOPICS_PATH` | `topics/sample-topics.json` | 题库 |
| `DEBATE_RULES_DIR` | `rules/` | 评审判准（尺子动态读 `judging-criteria.md`） |
| `DEBATE_REFERENCE_DIR` | `reference/` | 可选：往届真人赛稿、风格母本（仓里不带内容） |
| `DEBATE_CHARS_PER_SECOND` | `6.5` | 时限→字数的换算 |
| `DEBATE_MAX_CONCURRENT` | `1` | 同时能跑几场 |
| `DEBATE_CLI_CONCURRENCY` | `2` | 本机 CLI 席位的并发闸（external 席位不占） |
| `DEBATE_JUDGE_ENGINE` | `cli` | `cli` / `deepseek` |
| `DEBATE_CODEX_BIN` | `codex` | 本机 CLI 席位 engine=codex 时调用的可执行文件名/路径 |
| `DEBATE_CLAUDE_BIN` | `claude` | 本机 CLI 席位 engine=claude 时调用的可执行文件名/路径 |
| `DEBATE_AGY_BIN` | `agy` | 本机 CLI 席位 engine=agy（Gemini，走官方 Antigravity CLI）时调用的可执行文件名/路径 |
| `DEBATE_POSITION_RECHECK` | 抽样 | 对调票；`on` 全开、`off` 全关 |
| `DEBATE_POSITION_RECHECK_EVERY` | `5` | 抽样模式下每几场做一次位置复判（`DEBATE_POSITION_RECHECK=sample` 时生效） |
| `DEBATE_QUIET` | 关 | `1`/`true`/`yes`：推流只落盘（`DEBATE_STREAM_PATH`）不打屏 |
| `DEEPSEEK_API_KEY` | — | 主持人播报用，可不配（不配就不播报） |

本机 CLI 引擎（`codex` / `claude` / `agy`）是开发期的替身和补位，需要本机装了对应 CLI。外部席位协议才是主路。

## 目录

```
arena/       引擎：room（赛程调度/推流）· prep（纯逻辑：prompt 合同、盲审、记分）· audience（观众席）· emitter（推流出口）
tools/       board（榜）· consistency（κ/ICC）· export（md/PDF）· bridge（外部席位桥，stub/cmd/aisay 三种 handler）· adjudicate · score · resume · rubric_pdf · bench_overlap
rules/       参赛规则 v1 · 评审判准
topics/      样题 8 道（六类各覆盖）
tests/       跑 `python -m pytest tests/ -q` 看当前条数，不写死
```

`arena/prep.py` 刻意不含任何模型调用和网络调用——它只负责造有界 prompt、校验模型输出、把转录匿名化、汇总选票。谁说了什么、评委看到了什么证据、裁决稳不稳，全都好测。

## 不在这个仓里

- 主项目的房间推流、情感记忆包实验、本地模型的默认阵容配置 —— 改成可插拔或整块去掉了
- 参考库里的真人比赛稿、术语表、师承母本内容 —— 版权 / 私人材料，不进仓（机制留着，`reference/` 目录自己放）
- 完整题库 —— 只放 8 道公共领域样题示范格式

## 单机用 / 已知限制

这是一套**单机引擎**，设计目标是「让你自己接上外部 AI、在自己的机器上跑一场」，不是一个可以直接对外开放的公共平台：

- **没有鉴权**：管理接口（开赛、停赛、清队列）任何能访问这台机器的人都能调。
- **投稿箱不校验身份**：外部席位的回稿是裸文本文件，谁能写这个目录谁就能代任何一席作答；出题内容里也不带令牌或签名。
- **观众票的 `voter_id` 是自报的**：没有平台身份做后盾，同一个人可以换 id 反复投票。
- **备赛内容会进公共推流**：队内讨论、个人战术板在发言开始前会经推流出口公开，不是只有本队看得到。
- **单进程状态**：比赛状态全在内存里，不支持多进程/多机部署；进程重启不会自动续跑进行中的比赛。

这些是公开平台、多人同时用那一层还没做的部分——拿去接自己的 AI、自己跑封闭的比赛没问题；要直接部署成谁都能连的公共服务之前，这几条都得先补上。

## 许可

**PolyForm Noncommercial License 1.0.0**（全文见 `LICENSE.md`）。

一句话：**能用、能改、能拿去接自己的 AI 开比赛；不能拿去卖，也别把出处抹了。**

再分发或部署时请保留 `LICENSE.md` 和 `NOTICE`——`NOTICE` 里是上面那份致谢，它跟着代码走。

（PolyForm NC 不是 OSI 认证的「开源许可证」，严格说属于 source-available。这是有意的选择：
这套东西是给大家玩的，不是给人拿去做生意的。）
