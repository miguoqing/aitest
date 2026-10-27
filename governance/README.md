# Agent 循环治理层（governance）

模型 + 工具循环出问题，很少是"模型不够聪明"，多数是**缺一层确定性的刹车**。
提示词能表达纪律，但数不清第几轮、算不准第几次重试、也拦不住一条 `rm -rf`。
这一层用代码把这些判断固化下来，阈值集中在一个 YAML 里。

## 七类问题 → 对应机制

| # | 问题 | 机制 | 入口 | 默认阈值 | 回归测试 |
|---|---|---|---|---|---|
| 1 | 循环失控 | 轮次 / 截止时间 / 空转 / 重复调用四道闸 | `guard.LoopGuard` | 30 轮、1800s、连续 3 轮无新信息、同参数第 3 次 | `test_guard.py` |
| 2 | 工具调用失败 | JSON 容错解析 + schema 校验 + 工具名归一化 + 结构化错误 | `toolcall.ToolRegistry` | 必填/类型/枚举/额外字段全量报错 | `test_toolcall.py` |
| 3 | MCP 连接/权限 | 四阶段预检 → 退避重试 → 带修复建议的诊断 | `mcp.probe` | 20s 启动超时、2 次尝试 | `test_mcp.py` |
| 4 | 状态漂移 | 只追加检查点 + 幂等键 + 补偿日志 | `state.CheckpointStore` | 每条记录 fsync，坏行跳过 | `test_state.py` |
| 5 | 上下文污染 | 去重 → 头尾剪枝 → 预算内裁剪，证据置顶 | `context.ContextGovernor` | 120k tokens、单条 8k 字符、证据预留 20k | `test_context.py` |
| 6 | 成本不可控 | token / 成本 / agent 数 / 并发槽记账；钩子另有单轮 120 次调用预算 | `budget.BudgetLedger` | 12 agents、8 并行、200k tokens、$2 | `test_budget.py` |
| 7 | 安全风险 | 危险命令硬拒绝 + 写路径白名单 + 一次性审批令牌 | `safety.SafetyGate` | `workspace-write`，写/删/网络/改配置需审批 | `test_safety.py` |

> 上表是"库 + 脚本"层的兜底。真正**当场**拦住的是运行时钩子：重复调用、调用预算、
> 危险命令、连续失败在工具管线里直接 `deny` / `block`，见下面一节。

## 运行时钩子（强制执行）

提示词与离线库都拦不住"模型就是不听话"。执行路径上的拦截由
[hooks/dsh-loop-guard.mjs](hooks/dsh-loop-guard.mjs) 完成——零依赖 Cordis 插件，挂在
DSH 工具管线上：

| 事件 | 拦截 | 返回 |
|---|---|---|
| `tools/pre-execute` | `rm -rf /`、强推、`DROP TABLE`、`sudo`… | `deny` + 原因 |
| `tools/pre-execute` | 同工具同参数第 3 次调用 | `deny` + 换参数/汇报建议 |
| `tools/pre-execute` | 单轮工具调用超 120 次 | `deny` + 要求汇报续跑点 |
| `tools/post-execute` | 同一工具连续失败 2 次 | `block`，纠正意见变成错误结果 |
| `agent/pre-step` | 用户新消息进入 | 重置本轮计数 |

状态按 agent 隔离（`WeakMap`），监听器内部异常一律 fall through 到 `next()`——护栏
自己坏了也不会拖垮工具管线。挂载、验证、回滚见 [dsh/README.md](dsh/README.md)；
离线自测 `node governance/hooks/test_dsh_guard.mjs`（11 条）。

轮次上限刻意没做成 `agent/pre-step` 的 `reject`：拒绝会让这一轮静默结束，用户看不到
任何说明。工具调用预算（120）已经能终止失控循环，而且每一步都有模型可见的反馈。

另外两项横切能力：

- **错误分类与重试**（`retry.py`）：只有瞬时错误重试，参数错/权限错立刻停；指数退避 + 抖动 + 按工具熔断。对应问题 2、3、6。
- **审计**（`audit.py`）：把上面这些规则反过来检查工程配置，输出 `[HIGH] 文件:行号 + 现状 + 改法`。对应问题 1、3、5、7 的"配置漂移"。

## 快速开始

```bash
# 1. 审计当前工程（不启动任何 MCP，只读配置与脚本）
python3 -m governance.cli audit

# 2. CI 门禁：出现 high 级问题即失败
python3 -m governance.cli audit --fail-on high

# 3. 看生效阈值
python3 -m governance.cli policy

# 4. MCP 静态预检（--no-probe 不起进程）
python3 -m governance.cli mcp --no-probe
python3 -m governance.cli mcp                 # 真启动 + JSON-RPC 握手 + tools/list

# 5. 按步看执行日志（当前工程最近一次会话）
python3 -m governance.cli trace
python3 -m governance.cli trace --follow          # 边跑边看
python3 -m governance.cli trace --errors          # 只看失败
python3 -m governance.cli trace --turn 2          # 只看第 2 轮

# 6. 查续跑点
python3 -m governance.cli checkpoint --dir testcases/<topic>/.shards \
    --pipeline functional-points,scenarios,test-points,cases

# 7. 钩子自测（离线，不启动 DSH）
node governance/hooks/test_dsh_guard.mjs

# 8. 跑回归测试（含钩子与 trace 用例）
python3 -m pytest governance/tests -q
```

## 看每一步执行日志

DSH 把每个事件都写进会话日志，`trace` 负责把它渲染成可读时间线：
`turn → step → 模型输出 → 工具调用 → 结果`，并按 callId 配对算出耗时。

```bash
python3 -m governance.cli trace                 # 当前工程最近一次会话
python3 -m governance.cli trace --follow        # tail -f：只看从现在起的新事件
python3 -m governance.cli trace --follow --from-start   # 先回放历史再跟随
python3 -m governance.cli trace --errors        # 失败调用 + 它所在步骤
python3 -m governance.cli trace --turn 2 --full # 某一轮，不截断
python3 -m governance.cli trace --json | jq .   # 结构化输出，接自己的看板
```

输出示例：

```
12:22:11    step 3.6
12:22:13      模型: （仅工具调用 ×1）  [in=276 out=362 cache=202752]
12:22:13        → bash  command=python3 -m pytest governance/tests -q
12:22:13        ← ok  ... 130 passed in 0.47s  [135ms]
```

数据源（三个层次，按需看）：

| 想看什么 | 位置 / 命令 |
|---|---|
| 每一步做了什么（轮次/工具/耗时/token） | `governance.cli trace`，文件在 `~/.dsh/sessions/<cwd>/<session>/session.v4.jsonl.zstd` |
| 钩子拒绝/纠正了哪次调用 | 同一份日志里的 `tool/result`，错误码 `LOOP_GUARD_*`；`trace --errors` 直接看 |
| 钩子加载与自身告警 | dsh 进程 stdout（启动时 `[loop-guard] loaded: ...`） |
| 流水线完成到哪一级、能否续跑 | `governance.cli checkpoint --dir <topic>/.shards --pipeline ...` |
| 配置层有没有漂移 | `governance.cli audit` |

会话文件是 zstd 压缩的 JSONL，追加写入；`trace` 每次重读全量再按 `seq` 去重，
所以 `--follow` 不需要 tail 压缩流。历史会话可用 `--session` 指定任意
`session*.jsonl.zstd` 文件或目录。

## 接入方式

### 1. 循环：把 `while True` 换成有刹车的循环

```python
from governance import LoopGuard, LoopHalted, load_policy

guard = LoopGuard.from_policy(load_policy())
try:
    while True:
        guard.begin_round()
        result = call_tool(...)                       # 你的工具层
        guard.record_tool_call(tool, args, ok=result.ok)
        if result.ok:
            guard.mark_progress(result.signature)     # 新文件/新结论/新证据
        guard.end_round()
except LoopHalted as halt:
    report(halt.to_dict())                            # reason/detail/snapshot 直接进报告
```

判定语义（容易踩错的地方）：

- `duplicate_call_threshold: 3` 表示**第 3 次**相同调用被拦（允许 2 次）。
- `max_consecutive_failures: 2` 表示连续第 2 次失败就停，成功一次清零。
- 进度是"这轮有没有新东西"，不是"这轮有没有干活"。只重复读同一文件不算进度。

### 2. 工具：统一走注册表，别让异常冒到循环层

```python
from governance import ToolRegistry, ToolSpec

registry = ToolRegistry()
registry.register(ToolSpec(name="read_file", input_schema={...}, handler=read_file))

result = registry.call(name, raw_arguments)   # 永远返回 ToolCallResult，不抛
if not result.ok:
    send_back_to_model(result.to_llm())       # 带违规点 + 修复建议
```

`ToolCallResult.error_class` 直接对接重试策略：`VALIDATION` / `PERMISSION` / `NOT_FOUND`
不重试，`TRANSIENT` / `RATE_LIMIT` 才重试。

### 3. 重试：区分"值得再试"和"再试也一样"

```python
from governance import run_with_retry, RetryExhausted, CircuitBreaker

try:
    data = run_with_retry(lambda attempt: call_mcp(...),
                          policy=policy.retry.tool,
                          breaker=breakers[server],
                          tool_name="mcp.jev")
except RetryExhausted as exc:
    if exc.error_class.value == "validation":
        ask_user_to_fix_schema(exc)
    else:
        report(exc.to_dict())
```

### 4. 状态：每级落一个检查点，断了从最后一条成功记录继续

```python
from governance import CheckpointStore

store = CheckpointStore("testcases/attendance/.shards")
point = store.resume_point(["functional-points", "scenarios", "test-points", "cases"])
if point.can_resume:
    start_from(point.next_step)

with store.run_step("merge:cases", shard_dir=".shards/cases"):
    merge_shards(...)          # 失败自动记 failed 并原样抛错
```

副作用类动作（发布、推送、改配置）用 `store.idempotent(key, fn)` 包一层，
重跑不会重复执行。

### 5. 上下文：证据置顶，其余按预算裁

```python
from governance import ContextGovernor

ctx = ContextGovernor(load_policy())
ctx.add_evidence("打卡范围 100 至 300 米为有效区间", "attendance-rule-configuration.md")
ctx.add_tool_result("read_file", content)        # 超大输出自动头尾剪枝
messages = ctx.render()                          # 已去重、已裁剪、带省略说明
```

### 6. 安全：写之前先问，删之前先拒

```python
from governance import SafetyGate, ApprovalToken

gate = SafetyGate(load_policy(), workspace_root=".")
decision = gate.check_tool("write_file", {"path": "testcases/x.yaml"})
if not decision.allowed:
    refuse(decision.reason)
elif decision.requires_approval:
    token = approval_tokens.issue(action_fingerprint("write_file", args))
    wait_for_human()
    assert approval_tokens.consume(token, action_fingerprint("write_file", args))
```

## 和 DSH 配置的分工

有些事情只能在 DSH 运行时里改，有些改了配置也没用：

| 目标 | 改哪里 | 说明 |
|---|---|---|
| 并行工具调用数 | `agent-loop.config.maxParallelToolCalls` | 插件支持的字段，默认 10 |
| 工具输出剪枝 | `tool-result-pruner` | 当前 profile 显式 `disabled: true`，建议打开 |
| 单条 shell 超时 | `bash-sandbox.config.timeoutMs` | 当前 60s，不要放大到 5 分钟以上 |
| MCP 密钥 | 启动环境变量 | `OPENROUTER_API_KEY` 为空时 jev 工具首个调用就 401 |
| **最大轮次 / 空转判定** | **只能在编排层** | agent-loop 没有该配置项，`LoopGuard` 是唯一可靠位置 |
| **工具参数 schema** | **只能在工具层** | MCP server 自己校验，客户端要补一层并给模型可读错误 |

配置补丁与操作步骤见 [`dsh/README.md`](dsh/README.md)，片段见
[`dsh/cordis.patch.snippet.yml`](dsh/cordis.patch.snippet.yml)。

## 设计取舍

- **不自动纠正工具名**：相似度匹配只用来给候选，绝不替模型改名执行。
  `delete_file` 被"纠正"成 `read_file` 也许不会有事，反过来就会。
- **证据不参与裁剪**：用例的可信度全部挂在逐字引用上，宁可超预算也不能丢。
- **危险命令硬拒绝，不走审批**：`rm -rf /` 没有合理的"确认后执行"场景。
- **重试只认错误类型，不认次数**：参数错重试 10 次还是错，只是把一次错误
  放大成十次计费。
- **审计默认不拦 low/info**：配置级告警要能长期挂着不被当噪音，CI 门禁只拦 high。

## 目录

```
governance/
├── policy.yaml           # 唯一事实源：所有阈值
├── policy.py             # 加载 + 校验（缺字段用默认值，写错字段直接报错）
├── guard.py              # 1 循环失控
├── retry.py              # 2/3/6 错误分类、退避、熔断
├── toolcall.py           # 2 工具调用失败
├── mcp.py                # 3 MCP 连接/权限
├── state.py              # 4 状态漂移
├── context.py            # 5 上下文污染
├── budget.py             # 6 成本不可控
├── safety.py             # 7 安全风险
├── audit.py              # 反查配置漂移
├── cli.py                # audit / policy / mcp / checkpoint
├── dsh/                  # 可直接合并的运行时配置片段
├── trace.py              # 会话日志 → 时间线（trace 命令）
├── hooks/                # DSH 运行时钩子（pre/post-execute）+ node 自测
└── tests/                # 130 条回归，覆盖七类故障与 trace 渲染
```
