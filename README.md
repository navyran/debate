# CLI Debate Agent

独立开源仓库：<https://github.com/navyran/debate>，采用 [MIT License](LICENSE)。
独立安装可先执行 `git clone https://github.com/navyran/debate.git`，进入 `debate`
目录后执行本文安装步骤；文中 `cd agent/debate` 表示嵌入父项目时的路径。
发布文件安全边界见 [SECURITY.md](SECURITY.md)，不要上传本地 `.runtime` 目录。

`debate-agent` 使用 LangGraph 编排 Codex CLI、Claude Code CLI、Qoder CLI、OpenCode
CLI、OpenClaw、Hermes 与 CodeBuddy。每次运行任选至少两个拥有独立原生 Session 的 Agent，在
同一个 Git 工作空间中交替修改、审查和验证代码；默认仍是 Codex + Claude，新增
Agent 默认不选，也不会被预检。Hermes 与 CodeBuddy 标记为实验性。默认直接使用目标目录的
临时 `debate/<run-id>` 分支，也可以显式启用隔离 worktree。可选的 DeepSeek
Facilitator 通过 LangChain 读取脱敏结构化事实，为下一轮提供语义建议；确定性的
Supervisor 仍管理唯一写租约、检查点提交、恢复、审计和最终集成，不替参与 Agent
做业务裁决。

正常结束必须同时满足：权威最终验证通过、所有选中的 Agent 都独立判断完整 GoalContract 已完成，并确认同一个包含代码和审计材料的 final tree。选择集成时，目标分支最终只增加一个聚焦提交；选择保留时，目标分支不变，聚焦提交保留在结果分支。两种方式都不把中间辩论提交带入目标分支。

## 使用文档

- [辩论 Agent 使用与初始化手册](USER_GUIDE.md)：从全新环境初始化到首个 Run、恢复、审计和故障排查；
- [全部 CLI Agent 默认执行参数](CLI_AGENT_EXECUTION_DEFAULTS.md)：模型、推理强度、Session 与权限边界；
- [执行稳定性与 CLI 实时输出优化方案](RELIABILITY_AND_LIVE_OUTPUT_PLAN.md)：活动感知超时、实时事件、故障分类、自动恢复和实施验收方案。
- [OpenClaw 与 Hermes Agent 接入实施方案](OPENCLAW_HERMES_INTEGRATION_PLAN.md)：可行性结论、Adapter、Session、权限、预检和分阶段验收方案；
- [OpenClaw 与 Hermes 最终实施验收报告](OPENCLAW_HERMES_IMPLEMENTATION_REPORT.md)：模块提交、16 项验收证据、最终门禁与保留边界；
- [CodeBuddy 接入说明](CODEBUDDY_INTEGRATION.md)：headless 参数、Session、密钥边界、健康检查和真实 E2E 门禁；
- [一次性对抗审查方案](ADVERSARIAL_REVIEW_PLAN.md)：精简后的渐进实施方案与阶段决策门槛；
- [对抗审查最小协议](ADVERSARIAL_REVIEW_PROTOCOL.md)：EvidenceRef、ReviewResult 与可选 Issue Ledger；
- [对抗审查验证计划](ADVERSARIAL_REVIEW_VALIDATION.md)：对照实验、故障注入和 Go/No-Go 标准。

## 从 discuss-agent 迁移

项目、Python 包和主命令现已统一为 `debate-agent`、`debate_agent` 与
`agent/debate`。新运行把审计写入 `.agent/debate/<run-id>`，默认从
`~/.config/debate-agent/secrets.env` 加载 DeepSeek Key。

为避免已有环境突然失效，本版本保留以下读取兼容：

- `agent/discuss` 是指向 `agent/debate` 的兼容符号链接；
- 已安装环境重新执行 editable install 后，旧 `discuss-agent` 命令仍指向新入口；
- 新密钥文件不存在时，自动回退读取
  `~/.config/discuss-agent/secrets.env`；
- 恢复旧 Run 时，如果目标仓库已有 `.agent/discuss/<run-id>`，继续使用该目录；
- 新代码、文档和新 Run 不再生成旧名称。

## 安装与前置条件

- Python 3.10 或更高版本；
- Git；
- 至少安装并认证 `codex`、`claude`、`qodercli`、`opencode`、`openclaw`、
  `hermes`、`codebuddy` 中计划选择的两个；仅选择的 CLI 才会预检；
- OpenClaw 需要可连接的 Gateway；debate-agent 不使用 `--local` 或 `--deliver`；
- Hermes 需要显式 Provider/Model，当前推荐 `deepseek/deepseek-v4-flash`，并可从
  默认 secrets 文件读取 DeepSeek Key；
- CodeBuddy 使用 `codebuddy -p -y` headless 模式；首次调用创建 Session，后续显式
  `--resume`。可使用本机登录状态，或仅向 CodeBuddy 子进程传递其官方认证环境变量；
- 可选的 `DEEPSEEK_API_KEY`，用于默认模型 `deepseek-v4-flash`；
- 一个可信且可读写的目标目录；已有 Git 仓库必须状态干净并位于目标分支；
- 已有 Git 仓库需配置可用的 Git author；未初始化目录会自动 `git init`、配置本地 author，并将非保护文件提交为初始基线。

```bash
cd agent/debate
python -m venv .venv
.venv/bin/pip install -e '.[dev,ui]'
.venv/bin/python -m debate_agent --help
```

可信模式默认完全授权命令，Agent 可能执行联网工具，因此只应在可信工作区使用；严格模式可恢复命令白名单和联网限制。系统自身不 push、不发布、不部署，也不会在中途请求用户裁决。Agent 只能在 GoalContract 允许范围内形成最终变更，Git 检查点、合并和清理由 Supervisor 负责。

## 启动运行

如需启用语义编排，把密钥保存到默认文件：

```bash
mkdir -p ~/.config/debate-agent
chmod 700 ~/.config/debate-agent
# 编辑 ~/.config/debate-agent/secrets.env：
export DEEPSEEK_API_KEY='...'
chmod 600 ~/.config/debate-agent/secrets.env
```

每次启动 `debate-agent` 都会自动、安全地读取该文件，不需要手工 `source`；解析器
只读取 `DEEPSEEK_API_KEY` 赋值，不执行文件中的 shell 命令。显式环境变量优先于
文件值。该值不会进入冻结 run 配置，也不会传给参与的 CLI 子进程。未设置密钥
时，Facilitator 自动记录脱敏 `facilitator_fallback` 并继续，不阻断 Debate Run。

## Streamlit 控制中心

不想手写完整 CLI 参数时，可以启动只监听本机的控制中心：

```bash
cd agent/debate
.venv/bin/debate-agent ui
```

默认地址为 `http://127.0.0.1:8502`，状态目录为当前工作目录下的
`agent/debate/.runtime`。从仓库根目录启动时也可以显式指定：

```bash
agent/debate/.venv/bin/debate-agent ui \
  --port 8502 \
  --state-root /absolute/path/to/agent/debate/.runtime
```

页面提供：

- 简化启动表单，目标分支自动读取，默认不创建 worktree，可在高级设置中开启；
- 启动前可选择完成后合并目标分支，或保留独立结果分支供后续继续修改；
- 未初始化 Git 的目录会显示预告，启动时自动建立初始基线；
- 用户配置的命令文本转换为 JSON argv；可信模式默认允许 Agent 提议并执行任意命令；
- Git 干净度、分支、已选 CLI 和 DeepSeek Key 的启动预检；
- 当前阶段、写入回合、Writer、HEAD 和验证状态的两秒自动刷新；
- 所有参与者的提交接力轨，以及按选择动态出现的“Codex 实时”“Claude 实时”等
  各 Agent 实时页签；
- 各 CLI 的运行/空闲秒数、事件数、变更文件数、最近活动和终止类别；
- 默认折叠的“原始输出”脱敏尾部、结构化变更、验证、所有已选 Agent 的完成判断和 Facilitator 决定；
- 高级设置可冻结 CLI 路径、模型、startup/idle/hard 超时、MCP 和代理/证书变量名；
  OpenCode 提供 Variant，OpenClaw 提供原生 Agent/Profile 与 Thinking，Hermes 提供
  Provider/Model，CodeBuddy 提供原生 Agent/Profile 与 Model；后两者显示实验性提示；
- 历史运行、恢复、中止与清理入口。

页面通过独立 Worker 执行 LangGraph，不在 Streamlit 渲染线程里运行 CLI。
运行期的节点事件写入 `events.jsonl`/`progress.json`，CLI 内部事件写入
`invocation-events.jsonl`/`invocation-progress.json`。页面按 `seq` 增量读取，默认
最多保留最近 1000 条；raw 只按配置读取脱敏尾部，不加载完整文件。Prompt、隐藏
推理、API Key、Token、Cookie、代理值和完整 Session ID 不进入公开事件。
当前 UI 固定监听 `127.0.0.1`，不用于远程共享。

所有开发命令都以 JSON argv 传入，不经过 shell 拼接：

```bash
.venv/bin/python -m debate_agent run \
  --workspace /absolute/path/to/trusted-repo \
  --topic "实现目标并补充测试" \
  --participant codex \
  --participant openclaw \
  --first-agent openclaw \
  --openclaw-native-profile main \
  --openclaw-model provider/model \
  --openclaw-variant off \
  --allow-path src \
  --allow-path tests \
  --validation-command-json '["python","-m","pytest","-q"]'
```

也可以使用 `--topic-file requirements.md`。常用配置包括：

- `--participant codex|claude|qoder|opencode|openclaw|hermes|codebuddy`：可重复指定，至少
  两个；省略时默认 Codex + Claude；
- `--first-agent`：只决定首位 Writer，且必须属于参与者；
- `--worktree`：显式创建隔离 worktree；默认直接使用原目录的临时 debate 分支；
- `--target-branch`：覆盖自动识别的当前分支；新仓库留空时使用 Git 的默认初始分支；
- `--*-model` 与 `--*-command`：健康预检后冻结路径、版本和模型；OpenCode 必须
  使用单个 `provider/model`；
- `--opencode-variant VALUE`：冻结 OpenCode Variant；
- `--openclaw-native-profile VALUE`、`--openclaw-variant off|low|...`：分别映射
  OpenClaw `--agent` 与 `--thinking`；
- `--hermes-provider VALUE`、`--hermes-model VALUE`：Hermes 必须显式配置；
- `--codebuddy-native-profile VALUE`、`--codebuddy-model VALUE`：分别冻结
  CodeBuddy 原生 Agent 和模型；模型留空时复用当前账号的 CLI 默认模型；
- `--*-startup-timeout-seconds 300`、`--*-idle-timeout-seconds 300`、`--*-hard-timeout-seconds 3600`：分层调用时限；
- `--*-heartbeat-interval-seconds 5`：页面心跳，不会伪造有效活动；
- `--max-turns 8`：最多写入回合数；
- `--total-timeout-seconds 7200`：整个 run 的时限；
- `--max-process-failures 2`、`--stall-turns 2`：失败和停滞预算；
- `--mcp-mode minimal|full`：默认 minimal，不加载用户本机可选 MCP；
- `--proxy-env NAME`：仅允许显式选择的代理/证书变量名，配置不保存变量值；
- `--realtime-output` / `--no-realtime-output`：开启或关闭调用级公开事件；
- `--raw-output-tail-lines 500`：页面可读取的脱敏 raw 尾部行数；
- `--setup-command-json '["..."]'`：准备共享工作空间的基线命令；
- `--validation-command-json '["..."]'`：按需配置权威验证；页面默认留空，代码任务建议填写测试命令；
- `--allow-path`、`--protect-path`、`--high-impact-path`：写入范围；
- `--env NAME`：显式传给子进程的环境变量名；未指定时使用安全默认白名单；
- `--unrestricted-commands`：可信工作区默认开启，不限制 `sh`、`rg`、`python`、`git` 或联网命令；使用 `--no-unrestricted-commands` 可恢复严格白名单；
- `--allow-network`：仅在严格白名单模式下允许已批准命令联网；
- `--no-facilitator`：显式关闭 DeepSeek Facilitator；
- `--facilitator-model deepseek-v4-flash`：冻结语义模型名；
- `--facilitator-base-url https://api.deepseek.com`：冻结 DeepSeek API 地址；
- `--facilitator-timeout-seconds 120`：单次语义调用时限；
- `--state-root /path`：运行状态目录，默认 `agent/debate/.runtime`。

新 Run 在任何仓库副作用之前执行 CLI 健康预检：解析绝对路径和版本、发现或核验
显式模型、检查必需结构化流参数与登录状态，并运行最长 30 秒的只读模型探针。
相同 CLI/版本/模型/错误指纹重复出现基础设施故障时会触发持久化熔断，冷却期内
后续 Run 快速失败。MCP full 模式中的可选 MCP 失败只记录 warning；模型主连接失败
仍会阻止启动。

超时采用 startup/idle/hard 三层判定。工具、消息、文件变化和工作区指纹变化会刷新
idle；单纯存活、心跳、`thread.started` 与重复 cache/MCP 错误不会。只有工作区干净
且可安全重放的 `startup_timeout`、`idle_timeout`、`network_error` 或进程错误才使用
有限基础设施重试；`hard_timeout`、协议、验证、策略和工作区竞态不会盲目重放 Prompt。

普通回合使用 non-thinking 模式。出现无有效变化、所有已选 Agent 的关键判断冲突或审计
TreeAck 争议时，确定性代码切换到 thinking 模式并使用
`reasoning_effort="high"`。只保存最终 `FacilitatorDecision`；不保存完整 Prompt、
完整响应或 reasoning content。Facilitator 没有 shell、Git、文件或其他工具，不能
修改 GoalContract、验证计划、租约、Ballot、TreeAck 或完成状态。合法建议只能
聚焦下一 Writer 和下一轮任务，且 Writer 仍需读取当前 HEAD 与真实 Git 变化。

`--codex-resume-sandbox-verified` 默认开启，使同一 run 持续复用首次创建的 Codex Session。首次和 resume 子进程都以目标工作空间为 cwd，resume 调用显式保留 `sandbox_mode="workspace-write"`。更换 Codex CLI 版本、配置或运行边界后应重新验证；可用 `--no-codex-resume-sandbox-verified` 临时关闭并改为受控新 Session。

Qoder 默认以 `qodercli --yolo -p` 启动，首次调用创建固定 Session ID，后续使用
`--resume`。OpenCode 使用受控 JSONL 调用和 `--session`。OpenClaw 通过 Gateway、
显式 Session Key、0600 临时消息文件和 JSON 外层调用；连接状态不明时先查询 Session，
不会盲目重放，且永不生成 `--local`、`--deliver` 或 Channel 参数。Hermes 使用
`chat --quiet --yolo --no-restore-cwd`，通过 `--resume` 复用 Session；由于 v0.19.0
只支持 argv Query，Prompt 在同机进程列表中可能短暂可见，但审计 argv 始终脱敏，
超过 65,536 UTF-8 字节会在启动前拒绝，且不会启用 Hermes `--worktree`。
CodeBuddy 使用 stdin 传递 Prompt、`--output-format stream-json` 与 `--json-schema`
接收终局协议，首次响应的 `session_id` 会被冻结，后续只用显式 `--resume` 续接；
Vendor Adapter 只提取终止事件，结构化内容由 ProtocolGateway 校验或转码；返回不同
Session 或缺少终止事件时拒绝结果。默认仅允许
`Bash,Read,Edit,Write,Glob,Grep`，并关闭自动记忆、热更新、市场与遥测。

完全授权不会移除单 Writer 租约、Git 检查点、允许修改路径和敏感文件检查。Codex
仍使用 `workspace-write` sandbox；其他 CLI 与 Supervisor 的 shell 不具备同等级
操作系统隔离，因此只应对可信工作区启用。需要强制阻止工作区外副作用时，应额外
使用容器或 OS sandbox。

## 结构化协议恢复

CLI 的最终响应按固定边界处理：

```text
Vendor Adapter（进程、Session、终止事件）
  → ProtocolGateway（严格 JSON → 确定性语法修复 → DeepSeek 只读语义转码）
  → InvariantBinder（Agent、Turn、HEAD、GoalContract）
  → Pydantic Schema
  → Supervisor / LangGraph
```

严格 JSON 和有限的裸引号/说明文字修复都失败后，才会调用 DeepSeek。转码器复用
`facilitator_model`、`facilitator_base_url`、超时和 `DEEPSEEK_API_KEY`，但与
Facilitator 的业务路由职责相互独立；`protocol_normalizer_enabled` 默认开启。
DeepSeek 不能改写或补造 `decision`、`completion_assessment`、`completion`、
`accepted`，这些结论必须能在 CLI 原始终止内容中明确识别；Supervisor 掌握的身份、
回合、HEAD 和 GoalContract 版本由 Binder 注入并校验。成功的 `AgentResult` 会记录
`protocol_method=strict|deterministic|deepseek`，便于判断本次协议经过哪一级恢复。
发送给 DeepSeek 的终止文本会先经过与页面 raw 输出相同的凭据、Session、绝对路径
脱敏；本机严格解析和业务结论提取仍以未改写的原始内容为准。

## 状态、恢复与清理

```bash
.venv/bin/python -m debate_agent status  --run-id RUN-... --state-root /path
.venv/bin/python -m debate_agent resume  --run-id RUN-... --state-root /path
.venv/bin/python -m debate_agent abort   --run-id RUN-... --state-root /path
.venv/bin/python -m debate_agent cleanup --run-id RUN-... --state-root /path
```

- `resume` 只从该 run 的最新 SQLite checkpoint 继续；
- 若 Writer 已写入但调用中断，Supervisor 只在 active 租约、写前 journal 和当前 HEAD 能唯一归属时重新执行策略与验证、创建 recovered checkpoint，然后继续；不确定 Prompt 不会被盲目重放；
- `abort` 写入中止标记，运行中的进程组会被终止，并保留临时分支、可选 worktree 和恢复材料；
- `cleanup` 必须明确指定非运行中的 run ID，只清理该 run；
- 选择集成时，成功后自动移除临时 branch 和可选 worktree；选择保留时，目标分支不变，`debate/<run_id>` 及其可选 worktree 会保留；两种方式都会释放锁并清理 raw/session 等敏感运行产物。

非 `completed` 状态均不代表交付成功。常见状态包括 `preflight_blocked`、`policy_violation`、`workspace_race`、`recovery_blocked`、`max_turns`、`stalled` 和 `integration_blocked`，`status.json` 的 `stop_reason` 给出确定性原因。失败 run 默认保留现场供 `resume` 或排查。

## Git 与审计结果

每个合法写回合由 Supervisor 创建如下中间提交，message 包含真实 Agent、turn、GoalContract 与验证状态：

```text
[debate][codex][TURN-001][passed] ...
[debate][claude][TURN-002][failed-validation] ...
```

每次提交后，其他参与 Agent 都会收到各自的结构化变更摘要；它们在下次行动前必须自主读取最新 Git/workspace 并返回绑定当前 HEAD 的 `ChangeAck`。普通验证失败可以作为检查点交给后续参与者修复；安全、范围或来源不明的变更不会提交。

完成时，代码与精简审计一同进入：

```text
.agent/debate/<run_id>/
├── goal-contract.md
├── final-summary.md
├── change-summary.md
├── decisions.jsonl
├── facilitator-decisions.jsonl
└── validation-report.md
```

`facilitator-decisions.jsonl` 只包含绑定 HEAD、GoalContract 版本、模式、有限动作、
未解决条件、证据引用、fallback 类别和 Supervisor 采用的确定性路由。审计不包含
隐藏推理、完整模型请求响应、原始终端输出、Session ID、绝对路径或认证信息。
选择合并时，最终目标分支只接收一个以原始 base 为唯一 parent 的提交；若目标
分支漂移、源工作区变脏或 tree 不一致，运行进入 `integration_blocked`，不会强行
改写历史。选择不合并时，同一个聚焦提交会固定到 `debate/<run_id>`，目标分支保持
不变；直接工作区模式停留在该结果分支，独立 worktree 模式则保留结果目录。

审计摘要在进入 TreeAck 时标记为 `ready-for-final-tree-ack`，表示业务候选、最终验证和所有已选 Agent 的 complete Ballot 已就绪；所有已选 Agent 对固定 tree 的 Ack 只写入最终提交的 `Final-Tree-Ack` trailer，避免为记录 Ack 反向修改正在确认的 tree。只有所有已选 Agent 的 Ack，且最终提交已按配置完成集成验证或结果分支保留验证后，运行状态才是 `completed`。默认 Codex + Claude 只是默认参与者，不是完成门槛；选择 Qoder、OpenCode、OpenClaw、Hermes 或 CodeBuddy 时同样必须完成其 Ballot 与 TreeAck。

## 故障排查与验证

先查看页面的终止类别、`status` 和 `stop_reason`，再检查
`<state-root>/runs/<run_id>/` 中的脱敏 invocation 事件、validation、facilitator、
invocation journal 与 checkpoint。`facilitator/` 中只有
幂等结构化决定或 fallback；`missing_api_key`、`timeout`、`schema_error`、
`stale_binding`、`policy_rejection` 和 `provider_error` 都会安全降级。不要把
`raw/`、Session 状态或环境凭据提交到仓库。

常见调用终止类别：`startup_timeout` 表示从未出现有效活动，`idle_timeout` 表示工作
后静默，`hard_timeout` 表示达到单次绝对上限，`network_error` 表示模型主连接或 TLS
故障，`cache_incompatible` 表示 CLI 能力/缓存与所需协议不兼容。页面“原始输出”只
适合本机排障；即使已脱敏，也不应直接复制整个 `.runtime` 给第三方。

开发验证：

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
.venv/bin/mypy src
.venv/bin/python -m build
```

真实 CLI E2E 默认不收集、不调用模型。需要在隔离临时仓库中显式验证时运行：

```bash
DEBATE_AGENT_E2E_OPENCLAW=1 .venv/bin/python -m pytest \
  tests/test_openclaw_hermes_live_e2e.py -q
DEBATE_AGENT_E2E_HERMES=1 .venv/bin/python -m pytest \
  tests/test_openclaw_hermes_live_e2e.py -q
DEBATE_AGENT_E2E_CODEBUDDY=1 \
DEBATE_AGENT_E2E_CODEBUDDY_MODEL=<model> \
  .venv/bin/python -m pytest tests/test_codebuddy_live_e2e.py -q
```

OpenClaw 可用 `DEBATE_AGENT_E2E_OPENCLAW_PROFILE/MODEL` 覆盖测试 Profile/Model；
Hermes 可用 `DEBATE_AGENT_E2E_HERMES_PROVIDER/MODEL` 覆盖。Hermes 测试结束会精确
删除自身 Session；OpenClaw CLI 当前没有单 Session 删除命令，测试使用唯一 Key 并
执行官方 `sessions cleanup --enforce`，不会直接改写厂商内部 Store。
CodeBuddy E2E 必须显式给出模型，验证同一 Session 双轮续接、Writer 文件修改和
Reviewer 只读 Git 指纹；本机没有 `codebuddy` 时默认跳过。

本工具不会自动 push。最终提交仍由操作者自行检查和推送。
