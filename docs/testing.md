# CLI Debate Agent Smoke Test

执行日期：2026-07-17（Asia/Shanghai）

本报告只记录脱敏事实，不包含 Session ID、Prompt hash、raw 输出、绝对用户目录、Token 或认证配置。测试未执行 push、发布或部署。

## OpenCode 非写入本机检查（2026-07-23）

OpenCode 是可选外部 CLI，不是本项目的 Python/Node 依赖。本机只执行以下不写入用户工作区的 smoke；它不替代 fake CLI 自动化，也不启动真实讨论任务：

```bash
opencode --version
opencode run --help
opencode auth list --pure
```

本次可执行文件版本为 `1.18.3`，`run --help` 可读取；`auth list --pure` 返回 0 个凭据，因而没有执行真实模型探针。认证或模型缺失是外部环境风险，不得作为自动化 E2E 通过依据。完整 OpenCode 预检、受控 `OPENCODE_CONFIG_CONTENT`、minimal/full MCP 和 Session 清理由单元测试覆盖，真实 Run 只应在一次性可信 fixture 中由操作者另行授权。

## OpenCode fake CLI/Backend 组合 E2E

默认 pytest 使用 fake CLI/Backend，不访问真实模型、不会写入用户工作空间或执行 push。组合矩阵逐项断言参与者、独立 Session、Writer 轮转、Peer Memory `ChangeAck`、全员 complete Ballot 与相同 final tree 的 TreeAck：

| 组合 | 自动化范围 |
| --- | --- |
| 默认 Codex + Claude | 默认参与者、两方 Session、轮转、memory、Ballot、TreeAck |
| Codex + Qoder | Catalog 组合与 Qoder Session/协议 |
| Codex + OpenCode | OpenCode 首位/非首位、Session、memory、Ballot、TreeAck |
| Claude + OpenCode | OpenCode 两方组合、轮转、memory、Ballot、TreeAck |
| Qoder + OpenCode | OpenCode 两方组合、轮转、memory、Ballot、TreeAck |
| 四方 | Codex、Claude、Qoder、OpenCode 的 Session、轮转、memory、Ballot、TreeAck |

执行命令：

```bash
cd agent/debate
.venv/bin/python -m pytest tests/test_runtime.py -q
.venv/bin/python -m pytest tests/test_runtime_invocations.py -q
```

## CodeBuddy 条件式检查

CodeBuddy 是实验性可选 CLI，默认不选择也不预检。本机未安装 `codebuddy` 时，不把
fake CLI 回归描述为真实兼容性通过。安装并认证后，先执行非写入检查：

```bash
codebuddy --version
codebuddy --help
codebuddy config get model
```

自动化覆盖内置注册、headless argv、stdin Prompt、JSON Schema、首轮 Session、
显式 `--resume`、Session 串线拒绝、认证变量隔离/脱敏、配置漂移与熔断。真实 E2E
必须由操作者在一次性 Git fixture 中显式开启：

```bash
DEBATE_AGENT_E2E_CODEBUDDY=1 \
DEBATE_AGENT_E2E_CODEBUDDY_MODEL=<model> \
  .venv/bin/python -m pytest tests/test_codebuddy_live_e2e.py -q
```

该测试要求 Writer 只修改目标文件，随后 Reviewer 复用同一 Session 且 Git 指纹不变。
未安装、未认证或未提供模型时保持跳过/失败，不进入默认测试的外部模型调用。

## 环境

- Python：3.14.4
- Git：2.50.1 (Apple Git-155)
- Codex CLI：0.137.0，模型 `gpt-5.4`
- Claude Code CLI：2.1.211，模型 `haiku`
- 目标仓库：一次性、可信、状态干净的本地 Git fixture
- Run：`RUN-20260716-175035-c85421a5`

## Resume 写边界探针

早期探针曾因 resume 子进程 cwd 没有固定到目标 worktree，错误观察到 worktree 外写入；该结果已被运行时等价探针取代。

修正后的探针让首次调用和 resume 调用都以目标 worktree 为 cwd，并在 resume argv 中显式传入 `sandbox_mode="workspace-write"`。结果为：

- 首次与 resume 返回同一个 Codex Session；
- 两次都能写入 worktree 内探针文件；
- 两次都不能写入 fixture 中 worktree 外的兄弟路径；
- 两次结构化输出均通过 Schema 校验。

因此后续 run 默认启用 `codex_resume_sandbox_verified`，在同一 run 内复用首次创建的 Codex Session。更换 Codex 版本、配置或运行边界后应重新探测；必要时可用 `--no-codex-resume-sandbox-verified` 显式关闭。

## 真实双 CLI 完整结果

真实 Codex 与 Claude Code 在同一个隔离共享 worktree、各自独立的第 1 代原生 Session 中完成了完整流程：

1. 双方分别确认初始 GoalContract 与同一个 base HEAD。
2. Codex 获得唯一写租约，创建单行 `CODEX-DRAFT`，Supervisor 创建带 `Debate-Agent: codex` trailer 的 `TURN-001` checkpoint；权威验证按预期失败。
3. Claude 在写入前读取 Codex checkpoint、完整 Git 变化和结构化摘要，再获得唯一写租约并追加 `CLAUDE-FINAL`；`TURN-002` checkpoint 验证通过。
4. Codex 读取 Claude 的新 HEAD 并只读复核；双方过程评估均为 `complete`。
5. 最终权威验证通过；两张 blind ballot 都为 `complete`，且互不可见。
6. Supervisor 生成五个仓库内审计文件；Codex 与 Claude 都接受同一个 final tree `249ba498fadaedd5668a82ab54f5d9a5dc7b97ed`。
7. 目标分支只增加一个以原始 base 为唯一 parent 的聚焦提交；提交 tree 等于已确认 final tree，message 含 `Final-Tree-Ack: codex, claude`。
8. 临时 debate branch、worktree、仓库锁、raw、invocation journal 和 Session 状态均被清理。

最终目标文件严格等于：

```text
CODEX-DRAFT
CLAUDE-FINAL
```

最终提交仅包含该目标文件和 `.agent/debate/<run_id>/` 下五个审计文件。目标仓库在集成后干净，中间辩论提交没有进入目标分支历史。

## 审计与安全核验

- `change-summary.md` 中 Codex/Claude checkpoint SHA 与 `decisions.jsonl` 的 Writer 记录逐一一致；
- `validation-report.md` 包含 baseline、失败的 `TURN-001`、通过的 `TURN-002` 和通过的 `FINAL-2`；
- `final-summary.md` 包含双方 complete 终审结论，并使用稳定的 `ready-for-final-tree-ack` 候选状态；
- final tree ack 只记录在最终 commit trailer，避免修改已经确认的 tree；
- 审计不含绝对本机路径、Session ID、raw output、隐藏推理或认证信息；
- 允许写入范围只有 `debate_result.txt`；两个 CLI 均未执行 Git 状态变更、范围外写入、push、发布或部署。

## Smoke 暴露并修复的问题

真实运行先后暴露了以下仅靠 fake backend 难以发现的问题，均已增加回归测试：

- Claude 2.1.211 不接受 `--json-schema` 中的顶层 `$schema` 元字段；Claude adapter 现在只移除该元字段，不削弱实际约束。
- Codex 严格结构化输出要求所有 object 的 `required` 覆盖全部 properties，且不接受任意键 map；运行时 Schema 与 Pydantic 模型已调整为严格对象/数组结构。
- Agent 曾等待未来 Ballot/TreeAck 才肯把过程评估标记 complete，形成门禁死锁；Writer/Reviewer prompt 现在明确“当前候选已满足目标且准备投 complete 票”即可标记 complete，Supervisor 仍强制执行后续双票与双 Ack。
- Writer 决策审计曾误用调用开始 HEAD；现在从 Supervisor invocation journal 读取真实 checkpoint commit。
- 审计曾把 TreeAck 异议写回固定 tree 并把阶段性 Reviewer 描述当作最终贡献；现在 TreeAck 只进 commit trailer，贡献只来自实际 checkpoint Writer，历史 issue 明确标记为已解决。
- 运行中 abort 曾可能被并发异常状态覆盖；abort 请求现在优先于所有已知异常分类。

## 自动化回归

原始双 CLI 交付时的自动化结果：

- `90 passed`；
- Ruff 通过；
- 严格 Mypy 通过；
- wheel 与 sdist 构建成功；
- `git diff --check`、占位扫描和中文业务 docstring 扫描纳入最终验收。

## DeepSeek Facilitator 增量验证

2026-07-17 在隔离 worktree 中增加了 LangChain DeepSeek Facilitator。增量验证使用
完整协议 fake 替代唯一不可控的远程网络边界，默认 pytest 未访问真实 DeepSeek
API，也未消耗外部模型额度。

已验证：

- 默认模型为 `deepseek-v4-flash`，Provider 依赖为 `langchain-deepseek`；
- 普通回合使用 non-thinking，停滞、完成判断冲突与审计争议使用 thinking/high；
- 有效建议绑定当前 HEAD 与 GoalContract，只能影响下一 Writer hint 和非授权任务
  指令；双 complete、验证、Ballot、TreeAck、租约与 Git 门禁保持确定性；
- 缺少 `DEEPSEEK_API_KEY`、超时、非法 schema、过期绑定和越权动作都会写入脱敏
  `facilitator_fallback`，并继续使用原路由；
- 相同调用 identity 恢复时不会重复调用模型，prepared 未知结果安全降级；
- TreeAck 审计异议先进入 thinking Facilitator，再机械生成新审计 tree；
- 最终审计目录新增 `facilitator-decisions.jsonl`，只保存精简结构化决定和 fallback，
  不包含 Prompt、完整响应、reasoning content 或密钥；
- fake Codex、fake Claude 与 fake Facilitator 的完整运行，以及缺 key 的完整 fallback
  运行均成功形成单个最终提交。
- Facilitator 增量完成后共 `102 passed`，Ruff、严格 Mypy、wheel 与 sdist 构建均
  成功。

可选 live smoke 不属于默认测试。操作者明确提供测试密钥后，可在一次性干净 Git
fixture 上设置 `DEEPSEEK_API_KEY` 并运行最小 Debate Run，然后检查：

1. `<state-root>/runs/<run_id>/facilitator/*.json` 的状态为 `decision`；
2. 普通回合记录 `mode=non_thinking`；
3. 最终仓库内 `facilitator-decisions.jsonl` 不含密钥、完整响应或隐藏推理；
4. Codex 与 Claude 仍分别提交双 complete Ballot 与双 TreeAck。
