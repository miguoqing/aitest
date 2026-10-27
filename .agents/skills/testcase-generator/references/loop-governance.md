# 流水线的循环治理

这份文档只回答一个问题：**跑用例生成流水线时，什么情况下必须停、必须补、必须落盘。**
阈值与 `governance/policy.yaml` 保持一致；两者冲突时以 policy 为准。

## 预算

| 项 | 上限 | 超了怎么办 |
|---|---|---|
| 轮次（编排者自己的回合） | 30 | 落盘已有产物，报告缺口，停 |
| 工具调用总数 | 120 | 同上；通常意味着在重复读同一批文件 |
| 总 agent 数 | 12 | 调整批次大小，而不是把流水线拉长 |
| 每轮并行 agent | 8 | 超过后固定开销吃掉并行收益 |
| Token | 200k | 先落盘再继续；不要靠"再读一遍"补上下文 |
| 成本 | $2 | 砍掉第 C 轮审查，保 `testcases.yaml` 与报告 |

预算吃紧时的取舍顺序：**能交付的用例集 > 覆盖度报告 > 第三轮语义审查**。
半套带审查的流程，不如一整套能用的用例集。

workflow 脚本没有文件系统，只有内存计数。最小写法：

```js
const MAX_AGENTS = 12;
let agents = 0;

async function budgeted(task, label) {
  if (agents >= MAX_AGENTS) return null;   // 返回 null 让下游走"缺失分片"路径，而不是抛错丢结果
  agents += 1;
  return agent(task, { label });
}
```

`null` 一定要在上层被看见（见下一节补跑），否则预算耗尽会表现成"安静地少了一块知识"。

## 失败与补跑

`agent()` 失败返回 `null`，不会被异常打断——所以必须显式补：

```js
const results = await parallel(tasks);          // 失败项为 null
const missing = results.map((r, i) => [r, i]).filter(([r]) => !r).map(([, i]) => i);
const retried = await parallel(missing.map(i => () => tasks[i]()));   // 只补失败分片，重跑一次
```

规则：

- **只补一次**。同一个分片第二次仍失败，说明是知识或 schema 问题，不是偶发；记为待确认，
  写进 `coverage-report.md` 的缺口清单，交付时明确说出来。
- **补跑不能整批重来**。整批重跑会把已成功的分片再计一次费，还可能产生重复 ID。
- **缺失分片必须可见**。合并脚本要对比"应有文件数 vs 实际文件数"，少一个就报错，
  不要让后续阶段在缺一块知识的情况下"看起来正常"。

## 断点续跑

每轮落盘后，磁盘就是唯一可信状态。开工顺序：

```bash
ls testcases/<topic>/            # 01/02/03/testcases.yaml 各到哪一级
python3 -m governance.cli checkpoint --dir testcases/<topic>/.shards \
    --pipeline functional-points,scenarios,test-points,cases
```

从 `next_step` 继续，不要从头重跑。已经存在的分片要**沿用**：重跑会重新编号，
下游引用全部失效，比慢更糟。

长轮次（第 B 轮用例分片）可以分两半发，跑完一半先 `merge_shards.py` 落盘，
再发另一半。中断时磁盘上至少有一份能交付的用例集。

## 每轮的"新信息"判定

一轮结束前自问：这一轮产出了什么上一轮没有的东西？

- 新文件 / 新分片 / 新校验结果 → 有进度
- 只是把同一份文件又读了一遍、把同一段话又总结了一遍 → 无进度
- 只是换了措辞解释为什么失败 → 无进度

连续 3 轮无进度，结束循环并汇报，不要继续消耗预算。

## 校验门禁

```bash
python3 <skill>/scripts/merge_shards.py ...     # 合并 + 重复 ID 检测
python3 <skill>/scripts/validate_cases.py ...   # 硬失败非零退出
python3 <skill>/scripts/render_report.py ...    # 覆盖度报告
```

只有脚本报**重复 ID 或语义重复**时才值得开 agent 做归一。脚本能确定性完成的事，
不要交给模型重做一遍——那是把 1 次工具调用换成 1 个 agent。

## 和自己写的脚本的关系

流水线里的 Python 脚本（合并、校验、渲染）属于"工具层"，同样受治理：

- 输入路径不存在 → 直接报错退出，不要静默生成空结果
- 覆盖已有产物前先写 `.bak` 或写新文件，不原地覆盖
- 输出摘要（数量、ID 范围、告警数），让编排者一眼判断是否需要补跑
- 任何"删除"都不要在脚本里做；改为归档到 `.trash/` 并打印路径
