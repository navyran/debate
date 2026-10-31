# CodeBuddy 接入说明

> 状态：实验性、默认不选。单元与 fake CLI 回归已接入；真实 CodeBuddy CLI 双轮
> E2E 需要操作者在已安装和认证的环境中显式开启。

## 接入边界

CodeBuddy 作为内置 Agent 注册，和其他参与者共用 Supervisor 的唯一 Writer、Git
检查点、Peer Memory、全员 complete Ballot 与 TreeAck 门禁。它不会替代 Facilitator
或 Supervisor，也不会因启用 `-y` 绕过允许路径、敏感文件、工作区指纹和最终验证。

调用使用官方 headless 能力：

```bash
codebuddy -p -y \
  --output-format stream-json \
  --json-schema <schema> \
  --tools Bash,Read,Edit,Write,Glob,Grep \
  --setting-sources project \
  [--model <explicit-model>] \
  --agent <optional-native-agent> \
  --resume <only-after-first-session>
```

Prompt 通过 stdin 传入。默认 `mcp_mode=minimal` 时还会加入
`--strict-mcp-config --mcp-config '{"mcpServers":{}}'`。首轮 JSON 必须同时提供
`session_id` 与 `structured_output`；后续显式 `--resume`，若返回其他 Session ID
则拒绝结果。

## 安装、认证与配置

按 CodeBuddy 官方文档安装并完成一次交互登录。npm 安装方式为：

```bash
npm install -g @tencent-ai/codebuddy-code
codebuddy --version
codebuddy
```

模型可在页面或 `--codebuddy-model` 显式填写，也可从 `CODEBUDDY_MODEL` 或
`~/.codebuddy/settings.json` 的 `model` 发现；均未配置时不传 `--model`，复用当前
登录账号的 CodeBuddy CLI 默认模型。原生 Agent 可通过页面或
`--codebuddy-native-profile` 冻结。

系统只把下列已存在的官方变量传给 CodeBuddy 子进程，不传给其他参与 Agent：

```text
CODEBUDDY_API_KEY
CODEBUDDY_AUTH_TOKEN
CODEBUDDY_BASE_URL
CODEBUDDY_CUSTOM_HEADERS
CODEBUDDY_INTERNET_ENVIRONMENT
```

其中 Key、Token 和自定义 Header 会加入脱敏器，不写入公开事件、健康报告和审计。
默认还关闭 CodeBuddy 自动记忆、热更新、Prompt 建议、内置市场、自动升级、错误上报
和遥测，避免本次 Run 之外的隐式状态改变。

## 启动与恢复门禁

选择 CodeBuddy 后，健康检查会在临时 Git 仓库中验证版本、必需参数、模型、配置、
认证和一个无工具结构化 Query，再用首轮返回的 Session ID 做第二轮显式 resume。
只有两轮 Session 相同且返回预期 Schema 才允许进入目标仓库。恢复历史 Run 时会重新
比较可执行文件、版本、模型和运行配置哈希；漂移必须重新通过预检。

## 验证

默认测试不会访问真实模型：

```bash
.venv/bin/python -m pytest tests/test_codebuddy_health.py \
  tests/test_adapters.py tests/test_adapter_streams.py -q
```

真实 E2E 只在一次性隔离仓库中显式执行：

```bash
DEBATE_AGENT_E2E_CODEBUDDY=1 \
DEBATE_AGENT_E2E_CODEBUDDY_MODEL=<model> \
  .venv/bin/python -m pytest tests/test_codebuddy_live_e2e.py -q
```

该 E2E 验证同一 Session 双轮续接、Writer 只修改指定文件，以及 Reviewer 不改变 Git
指纹。本机未安装、未认证或未配置模型时应保持跳过/失败，不能把 fake CLI 结果视为
真实兼容性通过。

## 官方参考

- [CLI Reference](https://www.codebuddy.ai/docs/cli/cli-reference)
- [Headless Mode](https://www.codebuddy.ai/docs/cli/headless)
- [Environment Variables](https://www.codebuddy.ai/docs/cli/env-vars)
- [Configuration Directory](https://www.codebuddy.ai/docs/cli/codebuddy-dir)
