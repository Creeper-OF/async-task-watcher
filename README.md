# async-task-watcher

让 Codex 自主选择同步执行、原生进程等待或可恢复的后台任务，减少无意义轮询。

A Codex Agent Skill for native-first waiting and durable finite background jobs.
The bundled supervisor supports Linux/WSL2 and uses only Python's standard library.
It does **not** claim that an external notification can wake an idle Codex session.

## 它解决什么问题

- 普通短命令直接执行，不强制后台化。
- 编译、测试或验证需要结果时，优先使用 Codex 当前宿主提供的原生等待。
- 有独立工作可同时推进、或需要跨会话恢复时，才使用持久化监督器。
- 通过进程退出事件取得结果，不靠固定 sleep 或高频 status 查询。
- 保存命令、工作目录、恢复上下文、退出码及分离的 stdout/stderr 日志。
- 超时、取消、监督进程异常退出和通知失败都有状态记录。

选择依据是依赖、交互性、风险、运行环境和恢复需要，而不是一个固定耗时阈值。

## 环境要求与边界

| 能力 | 条件 |
| --- | --- |
| Skill 指导同步/原生等待 | 支持 Agent Skills 的 Codex；以实际可用工具为准 |
| 持久化后台监督器 | Linux/WSL2、Python 3.11+、内核支持 pidfd 与 Linux 信号机制 |
| Windows 原生 `.exe` 进程树 | 不由本监督器管理，使用 Windows/宿主原生工具 |
| macOS 后台监督器 | 不支持；使用原生等待 |
| 外部事件自动唤醒空闲会话 | 未验证，不依赖也不承诺 |

不是常驻服务，也不启动额外 Agent/模型。每个监督作业临时使用两个 Python 管理进程；
普通任务优先原生等待，以避免这部分开销。

## 安装

在 Linux 或 WSL 中获取仓库：

```bash
git clone https://github.com/Creeper-OF/async-task-watcher.git
cd async-task-watcher
python3 scripts/install.py
```

私有仓库需要有访问权限，也可以用 `gh repo clone Creeper-OF/async-task-watcher`。

默认安装到 `${CODEX_HOME}/skills/async-task-watcher`；未设置 `CODEX_HOME` 时为
`~/.codex/skills/async-task-watcher`。这一路径在 Codex CLI 0.162.1 已验证可发现。
也可以明确安装到当前官方用户目录：

```bash
python3 scripts/install.py --target "$HOME/.agents/skills/async-task-watcher"
```

不要同时在多个发现目录安装同名副本。安装器只复制四个 Skill 文件，不改
`config.toml`、认证、数据库或全局 `AGENTS.md`。目标已存在时会拒绝覆盖。
已有安装请先自行备份，并选择明确的新目标；安装中发生 I/O 错误时会说明可能残留的目录，不静默删除。

Codex 支持显式和按描述隐式选择 Skill；若新安装未显示，可刷新 Skills 或重启该会话。
参见 [官方 Skill 文档](https://learn.chatgpt.com/docs/build-skills)。

### 提高自主选择可靠性

将 [docs/AGENTS.example.md](docs/AGENTS.example.md) 中的简短策略**合并**到自己的
全局 `AGENTS.md`，不要覆盖原有指令。`agents/openai.yaml` 已允许隐式调用。

正常开发无需每次点名；是否触发仍取决于当前模型、任务、可见技能目录和其他指令。
显式调用可写 `$async-task-watcher`。

## 使用监督器

从已安装 Skill 的实际目录定位 `scripts/supervisor.py`，不要依赖当前项目目录。
下面以默认安装位置为例：

```bash
watcher="${CODEX_HOME:-$HOME/.codex}/skills/async-task-watcher/scripts/supervisor.py"
python3 "$watcher" --help

# 安全期限由具体作业决定；900 秒只是示例。
python3 "$watcher" start --cwd "$PWD" --timeout 900 \
  --context "完成 pytest 后读取失败日志，再决定修复或交付" -- pytest -q
```

保存返回的 `task_id`。可以先推进独立工作，再等待该任务：

```bash
python3 "$watcher" wait TASK_ID
python3 "$watcher" logs TASK_ID
python3 "$watcher" ack TASK_ID
```

`ack` 应在读取并实际处理结果后使用；它不会删除记录或日志。

| 命令 | 作用 |
| --- | --- |
| `start` | 启动可恢复的有限后台任务 |
| `run` | 启动监督任务并等待结果；普通短命令不必经此包装 |
| `wait` | 等待退出事件；可选等待预算不会取消任务 |
| `status` / `logs` | 读取状态和最多 16 KiB 的每个输出流尾部 |
| `list --cwd PATH --unhandled` | 按项目查找尚未确认的任务 |
| `cancel` | 请求取消本工具记录的进程；随后 wait 确认终态 |
| `ack` | 幂等记录消费者已处理结果，不删除文件 |
| `notify` | 执行配置的一次有界完成回调；不自动重试作业 |

`--root PATH` 是全局参数，放在子命令之前。默认任务状态位于
`${XDG_STATE_HOME:-$HOME/.local/state}/async-task-watcher`，不写进项目仓库。

退出码：成功 `0`、命令非零退出原样映射、监督故障/未知结果 `70`、等待窗口到期 `75`、
任务超时 `124`、取消 `130`。`start` 成功只代表启动/握手，任务结果以 `wait` 的 JSON 为准。

命令以 `--` 后的 argv 执行，默认不经过 shell；确需管道时才显式使用 `bash -lc`，
不要拼接不可信输入。命令和输出会被保存，秘密不要放在 argv 或日志里。

## 通知与恢复

默认不发送外部通知。`--notify-json` 可传一个授权的 argv 回调，回调通过
`ASYNC_TASK_ID` 和 `ASYNC_TASK_DIR` 获取结果位置，最长等待 10 秒。
`notification.status=delivered` 只表示回调退出成功，`consumed` 保持未知。

一次 Codex CLI 0.162.1 测试中，`codex queue` 返回成功，但消息仍留在目标活跃会话队列。
**入队不等于会话消费，更不等于唤醒。** 要可靠自动继续，就保持原生工具等待，
不要提前结束回合并期待队列启动模型。此观察不是对所有版本或空闲会话行为的断言。

中断后先 `list`，结合 `context` 和 `thread_id` 核对目标，再读取结果；不要盲目重跑部署。
可选 `--key` 为相同会话内的启动提供幂等，相同 key 的不同任务规格会被拒绝。

不承诺处理故意逃离进程组的守护进程、远程进程树、不可中断内核 I/O，或同时强杀两个管理进程。
操作系统重启会结束正在运行的进程；恢复只能标记未知失败，不能替它们“继续运行”。

## 开发与验证

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/install.py src/async-task-watcher/scripts/supervisor.py
```

测试使用真实本地进程和临时目录，不启动模型，不要求 Codex 登录，也不调用网络服务。
[验证记录](docs/verification.md) 区分实际测过的能力与尚未验证的能力。
GitHub Actions 在 Ubuntu/Python 3.11 上执行同一套测试。

## 仓库结构

```text
src/async-task-watcher/   可安装的 Skill、监督器及技术参考
scripts/install.py       不覆盖现有文件的安装器
tests/                   真实进程和安装回归测试
docs/                    全局策略示例与脱敏验证记录
.github/workflows/       最小权限测试工作流
```

仓库不包含个人配置、认证、会话记录、运行日志或私有验收快照。
未替所有者选择额外许可证，当前不附 LICENSE；后续可由所有者决定。
