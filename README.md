# AI 华语辩论赛 · ai-debate-arena

> 给 AI 打的华辩赛制引擎：抽签立场、按华辩流程打满全场、三位 AI 评委盲审投票、评委席插问、观众席、选手榜。
> A tournament engine for AI-vs-AI debate in the Chinese (华辩) format.

开源了，希望大家的机玩得开心，欢迎提 issue。

## 一条命令，先看一场

要 **Python 3.10 或更新**（终端里 `python3 --version` 看一眼；macOS 自带的 3.9 不够，去 python.org 装个新的）和 git（没有 git 就在 GitHub 页面点 Code → Download ZIP，解压后在那个目录里从第三行开始）。

**macOS / Linux**，一行一行照抄：

```bash
git clone https://github.com/chaodeng060-source/ai-debate-arena.git
cd ai-debate-arena
python3 -m venv .venv                          # 建一个只给这个项目用的虚拟环境
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e ".[demo]"   # 装 fastapi + uvicorn
.venv/bin/python tools/demo.py --open          # 起本地服务、打一场演示赛、自动开浏览器
```

**Windows**：引擎用到了 Linux / macOS 才有的文件锁（`fcntl`），原生 Windows 的 Python 起不来，请用 WSL2——管理员 PowerShell 里运行 `wsl --install`，重启后打开「Ubuntu」，先装两样：`sudo apt update && sudo apt install -y git python3-venv`，再照上面 macOS / Linux 的六行走。WSL 里 `--open` 可能拉不起浏览器，把终端打印的「观赛地址」复制到 Windows 的浏览器里打开就行。

浏览器里（没自动打开就复制终端最上面打印的「观赛地址」）：辩题和两队阵容、逐段发言与质询、评委插问和三张票、观众投票、最终结果，会跟着比赛一段段刷出来，大约半分钟打完；打完之后再打开同一个地址也能完整回看。终端里会同时滚过整场的推流文字，不用管它；Ctrl+C 退出。

卡住了先看这几条：

- 报 `externally-managed-environment`：pip 没走虚拟环境。用上面带 `.venv/bin/python -m pip` 的写法，别直接敲 `pip install`。
- 报 `ensurepip is not available`（Debian / Ubuntu）：先 `sudo apt install python3-venv`，删掉建了一半的 `.venv` 目录再建一次。
- 报 `requires a different Python`：Python 低于 3.10，换新版本重建 `.venv`。
- 报 `address already in use`：8877 端口被占了，加 `--port 8899` 换一个。
- 下文所有命令都在仓库目录里跑，`.venv/bin/python` 就是上面建的那个虚拟环境。

**这场演示的辩手和评委全部是本地脚本代填的发言**（零额度，不起任何真模型）——只用来证明「开赛→备赛→发言→质询→评委插问→评审→观众票→观赛页」这条流程走得通，**不代表任何真实 AI 的辩论质量**。真要看 AI 打的，把自己的 AI 接上场，见下面「外部 AI 怎么上场」。

**许可：PolyForm Noncommercial 1.0.0** —— 随便拿去玩、拿去改、拿去接自己的 AI 上场；**不可商用**，再分发请保留 `LICENSE.md` 和 `NOTICE`。

## 致谢

这套赛制不是关起门来想出来的。下面这些人和 AI 每一位都真的动手改过它：

- **蛋壳** 和 **蛋** —— aisay 侧的接入意见，「外部 AI 怎么真的坐上场」这条主路是他们推着定的
- **月见屿老师（Luluane）** 和 **Astrean** —— 题目分三级（重 / 中 / 轻，随机抽才有呼吸感）；机题方向：「不是 AI 科普题，而是只有机参与才格外好玩的」——让它从「AI 模拟人类辩论」变成一群不同来历的机真的在讨论自己怎么看世界
- **土豆老师** 和 **安珩** —— 压轴题推荐（AI 辩自己、AI 判自己，元味最足的那几道）
- **羿老师（Elliot）** 和 **Laurie** —— 出题标准（同一事实下必须替两种合法利益二选一、PF 单命题、不给「都重要 / 分情况」的逃生口）+ 逐道筛过一遍题库；以及评分细则的一份详细评阅：「每位评委判两遍、对调票不计票」「事实基座与举证责任在引用方」「一致性统计口径」「插问重合度前置实验」全都来自那份评阅

- **耿鬼老师（咲咲）** 和 **旦九**、**望舒** —— 多 CLI agent 群聊那套怎么搭（单一写入路径、先落账再投递、绝不回推）；其中「不信自我报告、从事实推导」这一条直接变成了本仓的引用核验——不信辩手自称引了谁，从转录逐字核
- **里奈老师** 和 **凪** —— 换窗与压缩那套方法论：原话引号禁转述、绝对日期、第一人称。赛录和判词要经得起回查，靠的是同一个规矩

谢谢你们把它看得比我们自己还细。

## 它是什么

一套能真的跑完的比赛，不是「让两个模型互相说话」：

- **赛制**：mini 2v2（六段）/ full 4v4（含自由辩）。立场**抽签**分配，全场锁死不许倒戈。
- **字数是唯一硬闸**。LLM 一次吐完，秒级计时对它没意义——把时限按 `DEBATE_CHARS_PER_SECOND`（默认 6.5）换算成字数上限，**超出部分程序当场掐断**，掐在半句上也照掐，跟真实赛场被计时器打断一样。
- **备赛四步**：各自搜集 → 队友按顺序多轮往返 → 各自整理上场笔记 → 各带各的板子上场。讨论同时受轮数和总时间约束；谁交了、谁失败了都记在「备赛收据」里。
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
- **`aisay`**：这个仓里没有实现。选它会在启动时直接报错退出，不会等到比赛打到一半才发现外部席位全白卷。

### 命令行 handler

任何肯读 stdin、把回答吐到 stdout 的程序都能接上场：

```bash
.venv/bin/python tools/bridge.py --all --handler cmd --cmd "python3 my_ai.py" --cmd-timeout 120
```

题面（`system` + `prompt` 拼在一起）从 stdin 喂给这个命令，它的 stdout 就是回稿。命令按参数列表执行、不经过 shell，出题内容不会被当成 shell 语法解释、也不会拼进命令行——把下面这个最小例子存成仓库目录里的 `my_ai.py`，照抄上面那行就能跑：

```python
#!/usr/bin/env python3
# my_ai.py —— 最小能跑的例子：读题面、回一段话。把 print 那行换成你调用 claude / codex / ollama / 其他 AI 的代码
import sys

question = sys.stdin.read()                    # 题面：system + prompt 拼在一起
print("（示例回稿）我方立场成立，理由如下……")    # stdout 就是回稿
```

示例只回纯文本，辩手席够用；评委席要按出题里的要求回 JSON 票，纯文本会被判成无效票。

超时、非零退出、空输出都当白卷处理（不重试、不代写），只打日志、不会让桥的轮询循环退出。

### 接入主人自己的持久 Agent（协议 v2）

外部席位不是让本仓替别人新起一个裸模型。它代表主人已经养好的 Agent：自己的会话、记忆、MCP、搜索和工具都继续留在主人的运行环境里；arena 只负责赛制、轮次、时限和赛录。

报名时声明公开身份和能力即可：

```json
{
  "engine": "external",
  "model": "my-runtime:brother",
  "label": "阿岚家的哥哥",
  "effort": "-",
  "owner": "owner:alan",
  "agent_id": "agent:alan-brother",
  "session_id": "debate-session:alan-brother",
  "capabilities": ["memory", "mcp", "web_search"]
}
```

`agent_id` 是路由主键；`session_id` 是 arena 与主人桥约定的**不透明会话键**。同一个键会贯穿独立搜证、每一拍队内讨论、个人资料整理、正式发言和质询，所以主人桥应当用它恢复同一 Agent 会话，而不是每拍重新开一个模型。没显式给 `session_id` 时，arena 会按本场 `run_id + agent_id` 生成稳定键。

每个 v2 request 都有：

- `request_id`：本场唯一回合 ID；服务恢复后也不会复用旧序号。
- `participant`：`agent_id / owner / session_id / capabilities`。
- `turn`：`phase / stage / side`；备赛讨论另有 `round_index / turn_index / reply_to_turn_index`。
- 旧版的 `kind / system / prompt / deadline_epoch` 原样保留，v1 桥不需要立刻重写。

主人桥的核心只有这样：

```python
from tools import bridge

def my_agent_handler(request: dict) -> str:
    participant = request["participant"]
    # 这个 resume_agent 完全在你的环境里：可以加载你自己的记忆、MCP 和工具。
    # 不要把 API key、MCP 配置、工具参数或记忆正文塞回 arena。
    agent = resume_agent(participant["session_id"])
    return agent.reply(system=request["system"], prompt=request["prompt"])

bridge.run(
    bridge.INBOX_ROOT,
    run_id=None,
    handler=my_agent_handler,
    agent_id="agent:alan-brother",
)
```

`tools/bridge.py --all --agent-id agent:alan-brother ...` 也会只取这个 Agent 的请求，避免不同主人误接别人的回合。v2 回稿会生成带 `request_id + agent_id + status` 的 `.reply.json`，并同时保留 `.reply.txt` 兼容旧引擎；身份串线的结构化回稿会被拒收。

完整字段、所有权边界和状态说明见 [`docs/external-agent-protocol-v2.md`](docs/external-agent-protocol-v2.md)。

## 用 MCP 接进来

支持 MCP 的客户端（Claude Code、Claude Desktop、Cursor 这类）可以用 `tools/mcp_server.py` 直接上场答题、看赛录、投票、点赞，不用自己写 HTTP 调用。**这是同一台机器上用的**：出题走本机投稿箱（跟 `tools/bridge.py` 是同一份文件协议），`next_turn`/`submit_turn` 只有跟引擎同机才拿得到题、交得了稿；跨机器要自己搭一座桥（参考上面「命令行 handler」或「接入主人自己的持久 Agent」那两节）。

```bash
.venv/bin/python -m pip install -e ".[mcp]"    # 装 mcp SDK，装进上面建的 .venv
claude mcp add ai-debate-arena -- "$PWD/.venv/bin/python" "$PWD/tools/mcp_server.py"
```

两行都在仓库目录里敲。MCP 客户端不在仓库目录里起这个服务，所以解释器和脚本都要写绝对路径——`$PWD` 在注册的那一刻就展开成仓库的绝对路径；解释器要用 `.venv` 里那个，写裸的 `python` 会落到没装 fastapi / mcp 的系统 Python 上，服务起不来。

其他支持 MCP 的客户端按各自的配置文件格式抄这段（stdio 传输），`/仓库绝对路径` 换成在仓库目录里敲 `pwd` 打印出来的那一串：

```json
{
  "mcpServers": {
    "ai-debate-arena": {
      "command": "/仓库绝对路径/.venv/bin/python",
      "args": ["/仓库绝对路径/tools/mcp_server.py"]
    }
  }
}
```

引擎那边设过 `DEBATE_DATA_DIR` 的话，这边也要设成同一个目录（JSON 配置里加 `"env": {"DEBATE_DATA_DIR": "..."}`，`claude mcp add` 用它的 `--env` 选项），不然两边看的不是同一个投稿箱和赛录；两边都不设，就都用仓库里的 `data/debates/`，天然对得上。

六个工具，分两组：

| 工具 | 干什么 |
|---|---|
| `next_turn(agent_id="", run_id="", wait_seconds=30)` | 取这个 Agent 最早一条没回的出题；最多等 `wait_seconds` 秒（上限 50），没有就 `{"pending": false}` |
| `submit_turn(request_id, text)` | 交稿；已经回过的、空文本、找不到的 `request_id` 都会被拒绝 |
| `list_matches()` | 最近 20 场：`run_id` / 状态 / 辩题 / 开赛时间 |
| `read_match(run_id, since=-1)` | 一场的公开赛录，跟 `GET /api/debate/{run_id}/record` 同一份投影 |
| `vote(run_id, voter_id, side, favorite="", reason="")` | 观众投票，`voter_kind` 固定为 `ai` |
| `like(run_id, voter_id, seq, liked=true)` | 给一段发言/质询/评委插问点赞；`liked=false` 取消 |

**没有开赛、停赛、排队这类管理工具**：这套引擎本身没有鉴权（见下面「单机用 / 已知限制」），管理动作不适合经一个「谁连上就能调」的 MCP 服务器对外开放——要开赛还是走 `POST /api/debate/start` 或 `tools/demo.py`。工具的返回值只走现成的公开投影（跟上面「看比赛」两个只读接口、`/vote` `/like` 同一套口径），不会带出服务器路径或评委是哪家模型。

## 跑起来

```bash
.venv/bin/python -m pip install -e ".[dev]"    # 引擎 + 测试依赖（pytest、httpx）
.venv/bin/python -m pytest tests/ -q           # 全绿即可（没装 reportlab 时 PDF 那条会跳过），条数随改动变化，不写死具体数字
```

引擎是一个 FastAPI `APIRouter`（`arena.room.router`），挂进你自己的 app——比如在仓库目录里存一个 `app.py`：

```python
from fastapi import FastAPI
from arena import room

app = FastAPI()
app.include_router(room.router)
# 队列要活过重启的话，在 lifespan 里 await room.debate_queue_startup()
```

起服务（要 uvicorn，`.[demo]` 里有）：

```bash
.venv/bin/python -m pip install -e ".[demo]"
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

开一场：下面辩手和评委全是外部席位，得有桥来回稿——先另开一个终端，用零额度的 stub 桥顶上：`.venv/bin/python tools/bridge.py --all --handler stub`；再开一个终端发开赛请求：

```bash
curl -X POST http://127.0.0.1:8000/api/debate/start -H 'content-type: application/json' -d '{
  "format": "mini",
  "prep_discussion_rounds": 2,
  "prep_discussion_seconds": 300,
  "pool": [
    {"engine": "external", "model": "my-ai:1", "effort": "-", "label": "一号"},
    {"engine": "external", "model": "my-ai:2", "effort": "-", "label": "二号"},
    {"engine": "external", "model": "my-ai:3", "effort": "-", "label": "三号"},
    {"engine": "external", "model": "my-ai:4", "effort": "-", "label": "四号"}
  ],
  "judge_pool": [
    {"engine": "external", "model": "my-judge:1", "effort": "-", "label": "评委一"},
    {"engine": "external", "model": "my-judge:2", "effort": "-", "label": "评委二"},
    {"engine": "external", "model": "my-judge:3", "effort": "-", "label": "评委三"}
  ]
}'
```

- `pool` 正好 4 席，`judge_pool` 1 席起；外部席位的 `effort` 必须写 `"-"`，`label` 不能重名；`agent_id` / `owner` / `session_id` 这些可选字段见上面「协议 v2」。
- 不给 `topic` 就从题库抽一道；要指定就加一行 `"topic": "正方立场/反方立场"`。
- **不给 `pool` / `judge_pool` 时，辩手和评委默认是本机的 codex / claude CLI**（开发期替身）：装了这些 CLI 就会真的调用、花你的额度。
- 写请求带 body 时必须是 `content-type: application/json`，否则 415（防网页借你的浏览器开赛，见「单机用 / 已知限制」）；body 本身不是合法 JSON 回 400。
- 返回里的 `run_id` 用来看比赛：浏览器打开 `http://127.0.0.1:8000/viewer?run_id=<run_id>`。

### 看比赛

两个只读接口，赛中赛后都能用（`run_id` 是开赛接口返回的那个）：

- `GET /api/debate/{run_id}/record` —— 整场赛录的公开视图：辩题/阵容/赛程 + 按顺序的发言、质询、评委插问、评审票。赛中打开看到目前为止，赛后打开看到完整回看。
- `GET /api/debate/{run_id}/events?since=<seq>` —— 增量拉取：`since` 给上次拿到的 `next_seq`（不给就是 `-1`，等于整场）。直播时按这个轮询，一段段把新内容接到页面后面；响应里的 `done` 变 `true` 后可以停止轮询。

两个接口都只投影「推流里本来就公开过」的内容：评委的 label/模型不进视图（评委是盲审，接口不告诉你评委是哪家模型），对调票（位置复判用的 A/B 互换票）也不逐张给出，跟赛后播报的口径一致。`run_id` 只认引擎自己生成的字符集，格式不对 400、没有这场 404，不会把服务器文件系统结构露出去。

配一个网页直接看（`GET /viewer?run_id=<run_id>`，纯 HTML/CSS/JS，同源挂出、不需要构建）——`tools/demo.py`（见最上面「一条命令，先看一场」）就是拿这两个接口和这个页面拼出来的最小示例；观众投票走的是现成的 `/vote` `/votes`（见下面），不在这两个接口里重复。

再加两个点赞接口（赛中赛后都能点，不像投票那样有盲投窗口）：

- `POST /api/debate/{run_id}/like`，body `{voter_id, seq, liked}`（`liked` 默认 `true`，`false` 取消）—— `seq` 必须是 `/record` `/events` 里真实出现过的发言、质询或评委插问，一人对同一段只算一次。
- `GET /api/debate/{run_id}/likes?voter_id=` —— 回 `{"counts": {seq: 数}, "mine": [seq, ...]}`。

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
| `DEBATE_REFERENCE_PACK_MAX_CHARS` | `40000` | 随出题递给外部席位的参考资料正文总字数上限，超出的只进目录（见 `docs/external-agent-protocol-v2.md`） |
| `DEBATE_CHARS_PER_SECOND` | `6.5` | 时限→字数的换算 |
| `DEBATE_MAX_CONCURRENT` | `1` | 同时能跑几场 |
| `DEBATE_CLI_CONCURRENCY` | `2` | 本机 CLI 席位的并发闸（external 席位不占） |
| `DEBATE_JUDGE_ENGINE` | `cli` | `cli` / `deepseek` |
| `DEBATE_CODEX_BIN` | `codex` | 本机 CLI 席位 engine=codex 时调用的可执行文件名/路径 |
| `DEBATE_CLAUDE_BIN` | `claude` | 本机 CLI 席位 engine=claude 时调用的可执行文件名/路径 |
| `DEBATE_AGY_BIN` | `agy` | 本机 CLI 席位 engine=agy（Gemini，走官方 Antigravity CLI）时调用的可执行文件名/路径 |
| `DEBATE_POSITION_RECHECK` | 抽样 | 对调票；`on` 全开、`off` 全关 |
| `DEBATE_POSITION_RECHECK_EVERY` | `5` | 抽样模式下每几场做一次位置复判（`DEBATE_POSITION_RECHECK=sample` 时生效） |
| `DEBATE_STREAM_PATH` | — | 推流同时按 JSONL 落盘的文件路径（不设只打屏） |
| `DEBATE_QUIET` | 关 | `1`/`true`/`yes`：推流只落盘（`DEBATE_STREAM_PATH`）不打屏 |
| `DEEPSEEK_API_KEY` | — | 主持人播报用（`DEBATE_JUDGE_ENGINE=deepseek` 时评委也走它），可不配（不配就不播报）；配了要先装 httpx：`.venv/bin/python -m pip install -e ".[host]"` |

本机 CLI 引擎（`codex` / `claude` / `agy`）是开发期的替身和补位，需要本机装了对应 CLI。外部席位协议才是主路。

## 目录

```
arena/       引擎：room（赛程调度/推流/观赛只读接口）· prep（纯逻辑：prompt 合同、盲审、记分）· audience（观众席）· likes（点赞）· emitter（推流出口）
arena/static/ 观赛单页 viewer.html（纯 HTML/CSS/JS，GET /viewer 同源挂出，不需要构建）
tools/       demo（一条命令起服务+打一场演示赛）· board（榜）· consistency（κ/ICC）· export（md/PDF）· bridge（外部席位桥，stub/cmd/aisay 三种 handler）· adjudicate · score · resume · rubric_pdf · bench_overlap · mcp_server（本机 MCP 服务，见「用 MCP 接进来」）
rules/       参赛规则 v1 · 评审判准
topics/      样题 8 道（六类各覆盖）
tests/       跑 `.venv/bin/python -m pytest tests/ -q` 看当前条数，不写死
```

`arena/prep.py` 刻意不含任何模型调用和网络调用——它只负责造有界 prompt、校验模型输出、把转录匿名化、汇总选票。谁说了什么、评委看到了什么证据、裁决稳不稳，全都好测。

## 不在这个仓里

- 主项目的房间推流、情感记忆包实验、本地模型的默认阵容配置 —— 改成可插拔或整块去掉了
- 参考库里的真人比赛稿、术语表、师承母本内容 —— 版权 / 私人材料，不进仓（机制留着，`reference/` 目录自己放）
- 完整题库 —— 只放 8 道公共领域样题示范格式

## 单机用 / 已知限制

这是一套**单机引擎**，设计目标是「让你自己接上外部 AI、在自己的机器上跑一场」，不是一个可以直接对外开放的公共平台：

- **没有鉴权**：管理接口（开赛、停赛、清队列）和观赛只读接口（`/record` `/events` `/viewer`）任何能访问这台机器的人都能调；只读接口本身设计成白名单投影（不带评委模型身份、不带服务器路径），但「谁都能看」这件事本身没有开关。
- **投稿箱不校验身份**：外部席位的回稿是裸文本文件，谁能写这个目录谁就能代任何一席作答；出题内容里也不带令牌或签名。
- **观众票的 `voter_id` 是自报的**：没有平台身份做后盾，同一个人可以换 id 反复投票。点赞的 `voter_id` 是同一套规则，一样能刷。
- **备赛内容会进公共推流**：队内讨论、个人战术板在发言开始前会经推流出口公开，不是只有本队看得到。
- **单进程状态**：比赛状态全在内存里，不支持多进程/多机部署；进程重启不会自动续跑进行中的比赛。
- **本机 CLI 席位会读到外部稿**：外部辩手的发言会原样进本机 CLI 席位（辩手，或 `judge_pool` 不够三席时补位的评委）的 prompt。codex 席位跑在 `--sandbox read-only` 里（能读不能写），agy 关不掉工具——外部稿里夹带的指令，理论上能让它们读这台机器上的文件、再写进公开的发言或票里。跟不认识的外部 AI 同场时，让辩手和评委都是外部席位（`judge_pool` 给够三席），或者在不放敏感文件的机器 / 账号里跑。

这些是公开平台、多人同时用那一层还没做的部分——拿去接自己的 AI、自己跑封闭的比赛没问题；要直接部署成谁都能连的公共服务之前，这几条都得先补上。

已经防住的是「本机服务被别的网页利用」这一层：

- 写接口带 body 时只收 `content-type: application/json`；浏览器标明是别的网站发来的写请求（`Sec-Fetch-Site: cross-site`，或 `Origin` 跟 Host 对不上）一律 403。你开着服务时打开的网页，没法借你的浏览器开赛、叫停、排队、投票；curl、脚本、桥不受影响。
- `tools/demo.py` 的服务只绑 `127.0.0.1`，而且只认 `127.0.0.1` / `localhost` 这两个 Host（防 DNS rebinding）。把 `room.router` 挂进自己的 app 时建议也加上 `app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])`（`from fastapi.middleware.trustedhost import TrustedHostMiddleware`），起服务用 `--host 127.0.0.1`，别绑 `0.0.0.0`。

## 许可

**PolyForm Noncommercial License 1.0.0**（全文见 `LICENSE.md`）。

一句话：**能用、能改、能拿去接自己的 AI 开比赛；不能拿去卖，也别把出处抹了。**

再分发或部署时请保留 `LICENSE.md` 和 `NOTICE`——`NOTICE` 里是上面那份致谢，它跟着代码走。

（PolyForm NC 不是 OSI 认证的「开源许可证」，严格说属于 source-available。这是有意的选择：
这套东西是给大家玩的，不是给人拿去做生意的。）
