---
name: async-task-watcher
description: "Use when builds, tests, deployments, validation or scripts involve uncertain runtime, dependent waiting, parallel checks, interrupted execution, or durable background results in Codex CLI/WSL. Not needed for ordinary quick file reads, searches or interactive commands. 编译、测试、部署、验证、脚本等待及后台任务恢复。"
---

# Async Task Watcher

自主选择执行方式；等待发生在工具或内核中，不靠模型反复查询。

## 决策

- 快速且结果立即需要：直接 exec。普通读取、rg、git status 不增加后台管理。
- 结果是下一步的前置条件，或需要交互/TTY：原生执行及等待。
- 已返回 session_id：保留并使用当前宿主的原生等待；不要重新运行命令。
- 有独立工作可同时推进，或需要跨会话恢复、分离日志：考虑下面的后台监督器。
- 后台前确认：有限、非交互、授权范围内、工作目录确定、依赖已满足、可清理、上下文能恢复。部署的副作用和共享资源竞争也参与判断。
- 不用固定耗时阈值选模式。普通长编译只有“等待”需要时，原生机制通常开销最低。

## 原生等待优先

工具名以当前会话实际能力为准：exec_command → write_stdin；执行单元返回 cell_id 后才用对应 wait。空输入 write_stdin 是阻塞等待，不是必须配 sleep 的查询。

先做真正独立的工作，随后订阅完成。没有其他工作时直接等待，不为等待生成推理、反复 status、短间隔轮询或固定 sleep。等待遵守宿主时限和用户更新要求；超时返回不代表命令已失败。工具输出过多时保存日志，完成后读摘要。

## 可恢复的后台任务

监督器只支持有 pidfd 的 Linux/WSL2 和 Python 3.11+；Windows 原生进程树、交互程序、故意 setsid 脱离的守护服务继续使用原生工具。禁止用它启动其他 Codex/模型进程。

先用脚本 `--help` 查看参数。从本 Skill 所在目录定位 `scripts/supervisor.py`；默认状态根为 `~/.local/state/async-task-watcher`，不依赖项目或账号。

示例（工作目录应为已确认的项目）：

```bash
watcher="/absolute/path/to/the/loaded/async-task-watcher/scripts/supervisor.py"
python3 "$watcher" start --cwd "$PWD" --timeout 900 \
  --context "运行 pytest；完成后读取失败日志，决定修复或交付" -- pytest -q
# 保存返回的 task_id。可以先做独立工作，再用它等待：
python3 "$watcher" wait TASK_ID
# 读取结果并实际完成后续处理，再明确确认；确认不会删除日志：
python3 "$watcher" ack TASK_ID
```

`--timeout` 是按作业风险设置的安全期限。命令必须是 `--` 后的独立 argv；确实需要管道才显式传 `bash -lc`，不用不可信字符串拼接。秘密通过环境/受限文件提供，不放进被记录的 argv。

恢复/中断时先 `list --cwd 路径 --unhandled`；核对 context、thread_id 和任务目标，再 wait 或 logs，不盲目重跑。任务 ID 与原生 session_id 不同。`--key` 仅在确实需要幂等启动时使用；新的验证轮次用新 key，同 key 不同命令会拒绝执行。

## 完成契约

- wait 完成后读取 status、exit_code、stderr_tail/stdout_tail、cleanup_complete；失败必须反馈/处理。
- wait 的 75 是等待窗口到期，不是任务超时；124 为任务超时，130 为取消，70 为监督故障/未知结果。
- cancel 仅作用于本工具记录的进程；之后仍需 wait 确认终态。
- 重复完成通知：以 task_id 和 acknowledgements 去重。通知投递不等于结果已处理，ack 不替代代码修复/交付的幂等性。
- 默认无外部通知。可选 `--notify-json` 是一次有界 argv 回调；失败保留结果，consumed 保持未知，不自动重试作业。
- 在 Codex CLI 0.162.1 的一次测试中，queue 可入队但目标活跃回合未消费；空闲唤醒未证明。不可声称投递成功已唤醒 Agent，不自行调用 turn/start/resume 或新模型。需要自动继续时保持原生等待，不提前 final 期待队列。

只有确实要查异常、能力边界或运行回归测试时才读 [design.md](references/design.md)。
