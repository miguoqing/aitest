# DSH 运行时钩子：loop-guard

`dsh-loop-guard.mjs` 是零依赖的 Cordis 插件，挂在 DSH 工具管线的三个事件上。
和 `governance/` 里的 Python 库互补：

| 层 | 位置 | 是否能强制 |
|---|---|---|
| 提示词 | `AGENTS.md`、技能文档 | 只能"建议"，模型可以不听 |
| **钩子** | **本插件，`tools/pre-execute` / `post-execute`** | **能，在执行路径上返回 deny/block** |
| 库 + 门禁 | `governance/*.py`、`audit`、pytest | 事后/脚本侧，能拦住配置漂移与脚本调用 |

## 挂载的事件

| 事件 | 拦截内容 | 返回 |
|---|---|---|
| `tools/pre-execute` | 危险命令（`rm -rf /`、强推、`DROP TABLE`、`sudo`…） | `deny` + 原因，模型看到错误 |
| `tools/pre-execute` | 同工具 + 同参数第 N 次调用（默认 3） | `deny` + 换参数/汇报建议 |
| `tools/pre-execute` | 单轮工具调用超预算（默认 120） | `deny` + 要求汇报续跑点 |
| `tools/post-execute` | 同一工具连续失败达阈值（默认 2） | `block`，把"停止重试"作为错误反馈交回模型 |
| `agent/pre-step` | 用户发新消息时重置本轮计数 | 仅重置 |

状态按 agent 隔离（`WeakMap`），不跨会话泄漏；任何监听器内部异常都 fall through 到
`next()`，护栏自己出错时不会把整条工具管线弄挂。

## 挂载方式

在 `~/.dsh/profiles/web/cordis.patch.yml` 末尾追加：

```yaml
- insert:
    - id: loop-guard
      name: <PROJECT_ROOT>/governance/hooks/dsh-loop-guard.mjs
      config:
        maxToolCalls: 120
        maxConsecutiveFailures: 2
        duplicateCallThreshold: 3
```

`name` 用绝对路径，DSH 的动态 `import()` 可直接加载。如果加载失败（日志里看不到
`[loop-guard] loaded`），退一步：把 `dsh-loop-guard.mjs` 复制到 profile 目录，改用相对名：

```bash
cp governance/hooks/dsh-loop-guard.mjs ~/.dsh/profiles/web/dsh-loop-guard.mjs
# patch 里写 name: ./dsh-loop-guard.mjs
```

## 验证

1. 重启 `dsh web`，日志里应出现：
   ```
   [loop-guard] loaded: maxToolCalls=120 maxConsecutiveFailures=2 duplicateThreshold=3
   ```
2. 让 agent 连续三次读同一个文件：第 3 次应收到 `LOOP_GUARD_DUPLICATE` 错误。
3. 让 agent 执行 `sudo whoami`：应被拒绝并提示"安全策略拒绝执行"。

离线自测（不启动 DSH）：

```bash
node governance/hooks/test_dsh_guard.mjs   # 11/11 passed
```

## 回滚

删掉 patch 里的 `- insert: loop-guard` 块，重启即可。插件不带持久化副作用，
卸载不影响会话历史。

## 为什么轮次上限不放在这里

`agent/pre-step` 能 `reject` 一步，但拒绝会让这一轮直接结束、用户什么都看不到；
把"到 30 轮了"变成静默停止，比跑超时更难排查。所以轮次上限放在编排层
（`LoopGuard` + `budgeted()` 计数），工具层的硬预算是 120 次调用——它同样能
终止失控循环，且每一步都有模型可见的反馈。
