# DSH 运行时配置：改哪几处、怎么验证、怎么回滚

运行时能做三件事：**挂执行路径上的钩子**、**限流与剪枝**、**补齐 MCP 鉴权**。
轮次上限不在这里（`agent-loop` 没有该配置项），由编排层的 `LoopGuard` / 预算计数负责。

> ⚠️ 不要改工程根目录的 `dsh-web-config.yml`：它是 profile 的生成产物，下次重建
> 会被覆盖；手工改还会让"当前会话能跑、下次不能跑"。只改 patch 层
> `~/.dsh/profiles/web/cordis.patch.yml`。
>
> `governance.cli audit` 现在会把 patch 层合并进来再判断，所以以 patch 为准，
> 不会被过期的生成配置误导。

## 一、挂运行时钩子（最关键的一步）

没有这一步，护栏只活在提示词和脚本里——模型不听话就失效。钩子插件挂在
`tools/pre-execute` / `tools/post-execute`，在工具真正执行前后强制：

| 场景 | 结果 |
|---|---|
| `rm -rf /`、`git push --force`、`DROP TABLE`、`sudo`… | `deny`，模型收到拒绝原因 |
| 同工具 + 同参数第 3 次调用 | `deny`，要求换参数或汇报卡点 |
| 单轮工具调用超过 120 次 | `deny`，要求先汇报续跑点 |
| 同一工具连续失败 2 次 | `block`，把"停止重试"作为错误反馈交回模型 |

在 `cordis.patch.yml` **末尾追加**（不是覆盖）：

```yaml
- insert:
    - id: loop-guard
      name: <PROJECT_ROOT>/governance/hooks/dsh-loop-guard.mjs
      config:
        maxToolCalls: 120
        maxConsecutiveFailures: 2
        duplicateCallThreshold: 3
```

`name` 用绝对路径，DSH 动态 `import()` 可以直接加载。离线先验一遍逻辑：

```bash
node governance/hooks/test_dsh_guard.mjs     # 11/11 passed
```

加载失败的退路：把 `dsh-loop-guard.mjs` 复制到 profile 目录，改用相对名
`name: ./dsh-loop-guard.mjs`。

## 二、限流与工具输出剪枝

```yaml
- id: agent-loop
  name: '@deepseek-ai/dsh-agent-loop'
  config:
    maxParallelToolCalls: 4      # 默认 10；一轮扇出十几个工具是最常见的瞬时成本源

- id: tool-result-pruner
  name: '@deepseek-ai/dsh-compaction-tool-result-pruner'
  config:
    thresholdChars: 8192
    headChars: 4096
    tailChars: 1024
  disabled: false                # 长循环里单条工具输出会持续占上下文
```

> 你当前的 patch 里 `tool-result-pruner` 已经改成 `disabled: false`；如果某次重建后
> 生成配置又变回 `true`，以 patch 为准再确认一次。

字段均已对照本机安装的插件 README 核验：`maxParallelToolCalls` 受支持（默认 10）；
pruner 的三个字符阈值受支持。

校验 YAML 没坏：

```bash
python3 -c "import yaml; yaml.safe_load(open('$HOME/.dsh/profiles/web/cordis.patch.yml')); print('yaml ok')"
```

## 三、一个环境变量（MCP 鉴权）

`jev` MCP 的 `OPENROUTER_API_KEY` 在配置里是 `process.env.OPENROUTER_API_KEY ?? ''`，
当前为空。server 能起来，但首个需要鉴权的调用会 401——表现成"工具调用失败"，
很容易被误判成 schema 问题。

```bash
export OPENROUTER_API_KEY=sk-or-...
# 写进 ~/.zshrc 或 launchd 配置，避免换终端就丢
```

## 四、一个"不要改"（权限模式）

保持默认 `DSH_PERMISSION_MODE` 未设置（等价 `workspace-write` + `approval: ask`）。
不要为了省几次点击把它设成 `danger-full-access`：那会把审批策略改成 `never`，
`rm -rf` 类动作就只剩钩子与 `SafetyGate` 一道闸。完全访问只应在一次性容器里用。

## 五、验证清单

```bash
# 1. 钩子自测（离线）
node governance/hooks/test_dsh_guard.mjs

# 2. 配置级问题：high 应为 0
python3 -m governance.cli audit --fail-on high

# 3. 看每一步：钩子拒绝会以 LOOP_GUARD_* 错误出现在工具结果里
python3 -m governance.cli trace --follow
python3 -m governance.cli trace --errors

# 4. MCP 双 server 握手
python3 -m governance.cli mcp

# 5. 护栏自身回归
python3 -m pytest governance/tests -q
```

重启 `dsh web` 后，日志里应出现 `[loop-guard] loaded: maxToolCalls=120 ...`。
再让 agent 连续三次读同一文件，第 3 次应收到 `LOOP_GUARD_DUPLICATE`。

## 回滚

```bash
cp ~/.dsh/profiles/web/cordis.patch.yml.bak ~/.dsh/profiles/web/cordis.patch.yml
# 重启 dsh web
```

钩子不写会话历史、不做持久化副作用，卸载不影响已有会话。

## 为什么不多加几项

- `agent-loop` 没有 `maxSteps`/`maxRounds` 字段（已核对插件 README），写进去会被忽略，
  甚至导致配置校验失败——轮次控制只能放在编排层。
- `llm-retry` 自身无配置；`retryPolicy` 在 provider 配置里，默认已是
  "5 次、500ms→10s、10% 抖动"，再调收益很小。
- `tool-call-timeout-policy` 无配置，超时按各工具自己的 `timeoutMs` 走；
  真正要盯的是 `bash-sandbox.timeoutMs`（当前 60s，合理，不要放大）。
- 轮次上限没做成 `agent/pre-step` 的 `reject`：拒绝会让这一轮直接结束、用户看不到
  任何说明，比跑超时更难排查。工具调用预算（120）已经能终止失控循环，且每步都有
  模型可见反馈。
