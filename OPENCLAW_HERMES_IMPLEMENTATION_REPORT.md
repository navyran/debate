# OpenClaw 与 Hermes Agent 最终实施验收报告

> 验收日期：2026-07-26
>
> 基线：`master` / `2fb9ceb4`
>
> 实施分支：`codex/openclaw-hermes-integration`
>
> 结论：通过
>
> Push：未执行

## 1. 实施结论

OpenClaw 已作为正式可选内置 Agent 接入，Hermes 已作为实验性可选内置 Agent
接入。默认参与者仍为 Codex + Claude；未选择的新 Agent 不执行预检，也不要求其
认证或 Gateway。实现没有新增 LangGraph 业务节点，没有改变 Supervisor、唯一写租约、
Git 检查点、Peer Memory、全体 Ballot 或 TreeAck 的职责边界。

方案、实施清单、兼容性探针、用户文档、代码、测试 Fixture 和最终产物均位于
`agent/debate`，没有把审计材料外置。

## 2. 模块与提交

| 模块 | Commit | 验收内容 |
| --- | --- | --- |
| M0 | `275a524d` | 完整实施清单、基线门禁和缺失的既有最终基准 Fixture |
| M1 | `6913b84a` | OpenClaw 真实 CLI、Gateway、Session、Writer/Reviewer 和中断探针 |
| M2 | `456e02ce` | Catalog 能力、Profile 字段、Prompt Transport、临时文件和审计 argv |
| M3 | `3e9e6894` | OpenClaw Adapter、Session 边界、输出白名单和模糊传输恢复 |
| M4 | `489db390` | OpenClaw Health、UI、审计、恢复和 Peer Memory |
| M1-H | `b467280e` | Hermes 真实认证、Session、cwd、Writer/Reviewer 和长度硬门禁 |
| M5 | `53ff091d` | Hermes 注册、Adapter、Provider/Model、Session、Runtime 和注册所需 Health Strategy |
| M6 | `0428452a` | Hermes Health 验收、CLI/UI 配置、审计与恢复元数据 |
| M7 | `83fc706d` | 组合矩阵、真实 E2E、Hermes 双流修复和用户文档 |

M5 与 M6 在最终审计时从原有合并提交拆开；拆分前后 Git tree 经
`git diff --exit-code <backup-tree> HEAD^{tree}` 验证完全一致。本地备份分支为
`codex/backup-openclaw-hermes-combined`。

## 3. 方案 16 项验收证据

| # | 验收项 | 直接证据 | 结果 |
| --- | --- | --- | --- |
| 1 | 页面可选且默认不选 | `agent_catalog.py`、`ui/app.py`、`test_ui_app.py`、`test_agent_catalog.py` | 通过 |
| 2 | 未选择时不预检/认证 | `cli_health.py`、`test_agent_registries.py`、`test_openclaw_health.py`、`test_hermes_health.py` | 通过 |
| 3 | OpenClaw Gateway Session 稳定复用 | `OpenClawAdapter`、真实 OpenClaw E2E、兼容性报告 | 通过 |
| 4 | Hermes Session 恢复且 cwd 正确 | `HermesAdapter` 强制 `--no-restore-cwd`、真实 Hermes E2E、兼容性报告 | 通过 |
| 5 | Prompt 不进入不安全审计边界 | `PromptTransport.TEMP_FILE/ARGV`、`audit_argv`、`test_adapters.py`、`test_process.py` | 通过 |
| 6 | Hermes 超长 Prompt 不截断 | 65,536 UTF-8 字节启动前门禁及边界单测 | 通过 |
| 7 | 两个 Agent 可在租约内写入 | 真实 Writer 探针、Runtime 组合矩阵、唯一租约测试 | 通过 |
| 8 | Reviewer 不留下变化 | 真实 Reviewer 探针、工作区指纹门禁和 Runtime Reviewer 写入拒绝测试 | 通过 |
| 9 | 不自行管理 Git/worktree | Adapter 禁止参数、统一 Prompt 规则、Supervisor Git 门禁测试 | 通过 |
| 10 | 检查点带 Agent 标识 | Runtime checkpoint message 与 OpenClaw/Hermes 审计测试 | 通过 |
| 11 | 其他 Agent 收到 Git 全文和摘要 | Peer Memory 广播、ChangeAck 与六参与者 Runtime 测试 | 通过 |
| 12 | 页面显示心跳/Session/Git/结果 | 动态 UI 卡片、实时轨道和 UI 测试 | 通过 |
| 13 | raw/审计不泄露敏感内容 | OpenClaw stdout 白名单、Hermes argv 脱敏、Redactor 与审计测试 | 通过 |
| 14 | OpenClaw 中断不盲目重放 | `openclaw_recovery.py` 完成/运行/未接收/未知四态测试 | 通过 |
| 15 | 全体接受且完成才结束 | 双参与者、三参与者和六参与者 Runtime 矩阵 | 通过 |
| 16 | 既有四 Agent 无回归 | 最终全量 551 项 pytest | 通过 |

## 4. 最终验证

在拆分后的最终代码 tree 上执行：

```text
python -m pytest -q
551 passed in 316.19s

ruff check src tests
All checks passed!

mypy src
Success: no issues found in 39 source files

python -m build
Successfully built debate_agent-0.1.0.tar.gz
and debate_agent-0.1.0-py3-none-any.whl

git diff --check
exit 0
```

真实 CLI E2E 也在当前安装版本上重新执行：

```text
DEBATE_AGENT_E2E_OPENCLAW=1 ... test_openclaw_hermes_live_e2e.py -q
2 passed in 8.98s

DEBATE_AGENT_E2E_HERMES=1 ... test_openclaw_hermes_live_e2e.py -q
2 passed in 8.46s
```

默认不设置环境变量时，真实模型测试函数不会被收集，日常测试不调用模型。Hermes
E2E 精确删除自身 Session；OpenClaw 当前 CLI 没有单 Session 删除命令，E2E 使用
唯一 Key 并执行官方 session maintenance，不直接修改厂商内部 Store。

## 5. 差异、秘密与占位审计

- `master...HEAD` 只修改 `agent/debate` 下的实现、Schema、测试、Fixture 和文档；
- M0 增加的 `final_benchmark.json` 是既有测试明确引用、但基线遗漏提交的固定
  Fixture，用于让独立 worktree 可复现基线，不是新业务范围；
- 新增代码没有 TODO、FIXME、占位实现、skip、xfail 或调试断点；
- Fixture 和文档只保存 `<redacted>`、占位 Key 与不可复用 Session 摘要；
- 没有发现 API Key、Token、完整业务 Prompt 或可复用 Session 凭据；
- 无关重构：无；
- 未完成项：无；
- Git push：未执行。

## 6. 保留的产品边界

以下是已接受并已文档化的边界，不属于未完成项：

- Hermes v0.19.0 仍标记为实验性；方案 P3 的十次真实业务 Run 是未来取消实验性
  标记的观察条件，不是本次接入完成条件；
- Hermes Prompt 受厂商 CLI 限制位于 argv，同机进程查看者可能短暂可见；审计脱敏
  和 65,536 字节门禁不能替代操作系统级进程隔离；
- OpenClaw 依赖外部 Gateway，传输状态未知时会安全停止或恢复，不承诺无证据重试；
- OpenClaw 与 Hermes 的实时事件只展示厂商有直接证据的生命周期信息，不虚构
  Codex JSONL 同等级的工具粒度。
