# Codex subagent 通信拓扑 hook

使用 `PreToolUse` 限制直接调用 collaboration 工具：每个 agent 只允许与 **parent、siblings、直接 children** 通信。关系取自当前 session 的数据库父子边，不依赖 agent 名称或路径深度。主 agent 同样受限，只能联系直接 children。

## 安装与注册

需要 Bash、Python 3.9+，以及支持 `PreToolUse` 命令 hook 的 Codex。先进入本目录：

```bash
cd /path/to/subagent-hooks

# 项目级：目标项目目录必须已经存在；相对路径以执行命令时的目录为基准
./install.sh --local /path/to/project
./install.sh --local ../my-project

# 全局：写入已有 CODEX_HOME，未设置时写入 ~/.codex
./install.sh --global
```

安装器自动合并目标 `hooks.json` 的 `PreToolUse` 注册，命令使用当前 Python 解释器和脚本的绝对路径，并对空格、引号等 shell 字符转义。**相对 command 路径按 hook 执行工作目录解析，不按 hooks.json 所在目录解析**，所以安装器不用相对 command。

安装后在 Codex 中审核并信任该 hook，再开启新 session，检查 hook 已启用。安装器不会修改信任设置，也不会替你信任项目。注册命令发生变化时可能需要重新审核。已有 session 不保证自动重载。

安装位置：

| 模式 | 注册文件 | 运行代码和配置 | 作用范围 |
| --- | --- | --- | --- |
| `--local PROJECT` | `PROJECT/.codex/hooks.json` | `PROJECT/.codex/subagent-hooks/` | event 的 cwd 与 PROJECT 完全一致 |
| `--global` | `CODEX_HOME/hooks.json` 或 `~/.codex/hooks.json` | 同目录下 `subagent-hooks/` | 所有工作目录，按 session 隔离 |

项目范围采用目录精确匹配；切换到子目录或其他 worktree 不在该项目安装的范围内。如需覆盖这些目录，逐个安装或使用全局模式。全局和项目同时安装时，两者都可能执行，拒绝决定可能叠加；请选择需要的安装范围。

重复运行相同安装命令即可升级代码，不会重复添加本安装的注册，也不会覆盖已安装的 `agent_acl_policy.json`。首次项目安装会迁移旧版 `.codex/agent_acl_hook.py` 的已知注册及策略，旧文件不主动删除。原 `hooks.json` 有变动时，会先保存 `hooks.json.backup-*`。其他 hook 和其他事件的注册保留。安装更新应串行执行。

## 策略配置

编辑**安装目录内**的 `agent_acl_policy.json`，下次工具调用生效：

```json
{
  "version": 1,
  "identity": {"state_db": null},
  "communication": {
    "allowed_relations": ["parent", "siblings", "children"],
    "status_reads": "neighborhood"
  }
}
```

- `allowed_relations`：上述三种关系的任意无重复子集；不允许给自己发消息。删除 `children` 也会禁止创建子 agent。
- `status_reads`：`neighborhood` 仅允许潜在返回结果全部处于自身及允许关系范围内的状态读取；`deny` 禁止状态读取。深层树中的全局 `list_agents`、`wait_agent` 通常会被拒绝，带 `path_prefix` 的列表也会检查整个匹配子树。
- `identity.state_db`：默认 `null`，读取 `CODEX_HOME/state_5.sqlite` 或 `~/.codex/state_5.sqlite`。必要时可指定绝对路径；同目录还必须有 `thread_history_1.sqlite`。调试环境变量 `PROJECT_AGENT_ACL_DB` 优先级更高。

`installation.json` 由安装器生成，单独记录 global/local 范围。安装目录内的 `agent-acl-audit.jsonl` 记录调用身份、目标与允许/拒绝原因，不记录消息正文。审计日志不会自动轮转。

## 验证

在本目录运行：

```bash
python3 -B -W error::ResourceWarning -m unittest -v test_agent_acl test_install
bash -n install.sh
```

测试覆盖多层拓扑的 128 个有向组合、重命名、身份伪造与 session 隔离、策略配置、watchdog，以及安装合并、重复安装、自定义策略保留、特殊字符路径、全局/项目范围。全局安装测试通过模拟 home 写入临时目录，不改真实用户配置。

## 兼容性与边界

本实现依据本机 Codex CLI 0.165.0 实测的事件字段及 SQLite schema：`threads`、`thread_spawn_edges`、`thread_turns`。这些属于内部接口，升级 Codex 后需要重新验证；不是跨版本稳定 API。调用者通过 `turn_id` 对应的 thread 与 `agent_id` 交叉核验；根调用使用 session 身份。需要持久化索引，`--ephemeral` 不适用。

策略覆盖 `send_message`、`followup_task`、`interrupt_agent`、`spawn_agent`、`list_agents`、`wait_agent`；未知 collaboration 操作拒绝。worker 配置错误、数据库缺失、身份无法确认、超时或异常输出会由 watchdog 返回 JSON deny。

**这不是不可绕过的安全隔离边界。** 已实测 Codex 对 hook 进程自身非零退出可能继续执行工具；watchdog 只能处理其成功启动后的 worker 故障。解释器/入口脚本无法启动、hook 未启用或未信任，均不能保证拦截。共享文件、其他通信 API、通过允许邻居转发消息，也不在直接通信 ACL 的保障范围内。

## 禁用与卸载

在目标 `hooks.json` 中删除 command 指向该安装目录 `subagent-hooks/agent_acl_hook.py` 的注册项，保留其他注册。随后可删除该安装目录，并新开 session。备份文件供手动恢复参考；已有其他配置变更时不要直接整体覆盖。全局与项目安装需分别卸载。
