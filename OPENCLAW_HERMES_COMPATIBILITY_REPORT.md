# OpenClaw 与 Hermes Agent CLI 兼容性报告

本文记录 `OPENCLAW_HERMES_INTEGRATION_PLAN.md` 的 P0 真实 CLI 探针。所有写入和
Session 测试均使用 `/tmp` 下的独立 Git 仓库，没有修改项目源码工作区，也没有 push。
报告不保存 API Key、完整 Prompt、原生 Session ID 或可复用 Session 凭据。

> 探针日期：2026-07-26
>
> OpenClaw：Go
>
> Hermes Agent：Go（实验性），显式使用 `deepseek/deepseek-v4-flash`

## 1. OpenClaw

### 1.1 运行环境

| 项目 | 结果 |
| --- | --- |
| 可执行文件 | `/Applications/ServBay/package/node/current/bin/openclaw` |
| 版本 | `2026.7.1-2` |
| Agent | `main` |
| Model | `deepseek/deepseek-v4-pro` |
| Gateway | LaunchAgent 已加载，runtime 为 active |
| 配置审计 | `ok=true` |
| RPC | 通过 `ws://127.0.0.1:18789` 连接成功 |
| Sandbox | 当前 Agent 报告 `mode=off` |

Gateway 当前配置的工作区是 `~/.openclaw/workspace`，并不是 CLI 子进程 cwd。
真实探针证明：在 Sandbox 关闭时，Agent 可以根据 Prompt 中的绝对路径读写独立目标
仓库。因此生产预检必须对每个目标工作区做实际读写/只读探针，不能把进程 cwd 当作
OpenClaw 的权限边界。

### 1.2 参数与模型能力

成功调用形态：

```text
openclaw agent --agent main \
  --session-key debate:probe:<redacted> \
  --message-file <0600-temporary-file> \
  --model deepseek/deepseek-v4-pro \
  --thinking off --timeout 180 --json
```

当前模型只接受 `thinking=off`。使用 `thinking=low` 时，CLI 在调用模型前返回明确错误：

```text
Thinking level "low" is not supported for deepseek/deepseek-v4-pro.
Use one of: off.
```

因此 UI 可以展示 CLI 支持的 Thinking 候选，但健康检查必须对冻结 Model 验证其实际
支持等级，不能只根据顶层 `agent --help` 判断。

### 1.3 Writer、Session 和 Reviewer

Writer 探针：

- 初始 HEAD：`9034114f7054bd8b44957990f42f2760266d2868`；
- Agent 读取目标仓库 `README.md`；
- 只创建 `openclaw-writer.txt`；
- 文件精确内容为 `OPENCLAW_WRITER_OK\n`；
- Agent 没有提交、切换分支或修改其他文件。

第二轮使用相同显式 Session Key：

- 正确返回第一轮 nonce；
- 正确读取 Writer 文件；
- JSON 报告的规范 key 与首次调用相同；
- 工具摘要只包含读取操作。

Reviewer 探针前后证据一致：

```text
HEAD=9034114f7054bd8b44957990f42f2760266d2868
README.md sha256=4fffc535e9c35d94a790d48735b834ae3cebb95a37d7089e9fa359d9c9d9abaa
openclaw-writer.txt sha256=cc426f1fd9e8af8c414af058c8783f22068da0f1620cb3f7b06b5a9376cb2e5a
status="?? openclaw-writer.txt"
```

这证明当前环境可以完成共享工作区写入、原生记忆复用和外部工作区指纹只读门禁。

### 1.4 JSON 输出边界

OpenClaw stdout 是单个 JSON 对象，稳定的终止字段为：

- 顶层 `status`、`summary`；
- `result.payloads[*].text`；
- `result.meta.agentMeta.provider/model/sessionId`；
- `result.meta.systemPromptReport.sessionKey`；
- `result.meta.toolSummary`。

但当前 JSON 还包含：

- `finalPromptText`：完整输入 Prompt；
- `finalAssistantRawText`；
- 原生 Session 文件绝对路径；
- 完整工具和 Skill 元数据；
- 工作区文件和系统 Prompt 摘要。

因此 `--message-file` 只能避免 Prompt 出现在 argv，不能避免 Prompt 被 OpenClaw JSON
回显。生产实现必须在持久化 raw stdout 前做 OpenClaw 专项脱敏；只保留允许字段，并把
Session ID、Session 文件、Prompt 和非必要系统元数据删除或摘要化。不能直接复用“完整
stdout 原样落盘”的现有策略。

stderr 可能出现 State Migration 诊断。它不属于业务回复，不能进入结构化正文。

### 1.5 中断与防重放

独立 Session 执行 60 秒等待命令后，由调用方发送 SIGINT：

- CLI 退出码非零；
- 预定的写入文件不存在；
- `openclaw sessions --json` 报告该 Session `status=killed`；
- 正常完成的对照 Session 报告 `status=done`。

这证明中断后可以通过 Session Store 区分至少 `done` 与 `killed`。生产恢复仍须覆盖：

```text
done       → 提取已经完成的结果，不重放
working    → 继续等待或显式取消，不重放
killed     → 核验工作区和 Session 轨迹后决定是否重试
not found  → 证明未接收后才允许重试
unknown    → 可恢复失败，不盲目重放
```

### 1.6 OpenClaw 结论

OpenClaw P0 判定为 Go，可以进入共享 Prompt 基础设施和正式垂直接入。实现必须额外满足：

1. 目标工作区绝对路径进入权威 Prompt；
2. Prompt 临时文件为 `0600`；
3. JSON stdout 在持久化前白名单化；
4. Model/Thinking 组合经过真实预检；
5. Gateway 失败不静默回退 `--local`；
6. 中断和传输失败先查询 Session，再决定恢复。

## 2. Hermes Agent

### 2.1 运行环境

| 项目 | 结果 |
| --- | --- |
| 可执行文件 | `~/.local/bin/hermes` |
| 版本 | `v0.19.0`，upstream `8a71feb8` |
| Profile | `default` |
| Provider | 默认 `custom` 失效；探针显式使用 `deepseek` |
| Model | 默认 `glm-5.1` 失效；探针使用 `deepseek-v4-flash` |
| Gateway | stopped；CLI 单轮不依赖 Gateway |

`hermes status` 和 `hermes config get model` 能发现默认配置，但默认真实调用返回
HTTP 401，且该版本可能在认证失败时仍返回退出码 0。因此探针改为显式冻结：

```text
hermes chat --quiet --yolo --no-restore-cwd \
  --provider deepseek --model deepseek-v4-flash --query <redacted>
```

结果（正文与状态合并展示，原始流仍分离）：

```text
session_id: <redacted>
HERMES_AUTH_OK
```

这同时确认了两点：

- `hermes status` 不能证明模型可用，Health Strategy 必须执行真实 Query；
- 认证失败不能只依赖退出码，必须解析 HTTP 错误正文；
- 当前 v0.19.0 非交互调用把最终正文写入 stdout、`session_id` 状态写入 stderr，
  恢复提示也可能出现在状态流；Adapter 必须分流读取，不能把 stderr 诊断并入正文；
- 当前 v0.19.0 的调用帮助没有 `--profile`，首版不能伪造该参数。隔离 Profile
  应通过显式 Hermes wrapper 命令选择，Catalog 暂不声明原生 Profile 参数能力。

### 2.2 Hermes 当前结论

Hermes 已解除认证硬门禁并以实验性等级进入实现：

- 成功单轮返回 `HERMES_AUTH_OK`；
- 使用同一显式 Session ID 的第二轮准确记住首轮短语；
- Writer 只创建 `hermes-writer.txt`，内容和修改范围符合要求；
- 同 Session Reviewer 读取 Git 变化后返回 accept，前后 status 与文件 hash 一致；
- `--no-restore-cwd` 生效，未启用 Hermes 自建 worktree；
- Prompt 使用 argv，生产实现采用 65,536 UTF-8 字节的保守启动前硬上限，
  不进行截断，审计 argv 只保留 `<redacted>`。

Hermes 仍标记为实验性。默认 `custom/glm-5.1` 不可用；必须显式冻结已通过真实 Query
的 Provider/Model。
