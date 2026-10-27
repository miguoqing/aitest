# 多智能体编排手册

## 目录

- [为什么这样扇出](#为什么这样扇出)
- [五级分析，三轮扇出](#五级分析三轮扇出)
- [谁来做合并与校验](#谁来做合并与校验)
- [预算：agent 数是有上限的](#预算agent-数是有上限的)
- [脚本骨架](#脚本骨架)
- [各轮 agent 指令模板](#各轮-agent-指令模板)
- [失败与续跑](#失败与续跑)
- [没有 workflow 时的串行降级](#没有-workflow-时的串行降级)
- [实操检查点](#实操检查点)

## 为什么这样扇出

扇出的目标是**让每个 agent 的上下文尽量小而完整**，不是"开更多 agent"。一个 agent 读一篇文档时，能记住这篇文档里所有"只能/不得/上限/默认"这类限定词；读到第五篇时，前面那些已经被挤出注意力了。而限定词正是测试点的主要来源。

代价是各 agent 不知道彼此在做什么。所以每轮扇出之后都必须把分片收回来合并，且 ID 由规则约束而不是靠 agent 自觉。**合并不是收尾动作，是流水线的一半。**

更要紧的一条经验：**编排本身有成本**。每一次 `agent()` 调用都是一次完整的子任务，编排者要串行等它回来、再发下一个。一个二十多轮的流水线，很容易在中途耗尽编排者的预算——表现是产出到第三级戛然而止，前面几级的文件都还在，但再也走不下去。所以下面这套编排刻意把"扇出"压到三轮，把能确定性完成的事交回编排者自己用 bash 做。

## 五级分析，三轮扇出

分析仍然是五级（功能点 → 场景 → 测试点 → 用例 → 合并审查），但**扇出只有三轮**：

| 轮次 | 扇出单位 | 产出 | 典型 agent 数 |
| --- | --- | --- | --- |
| A | 知识文件 | 该文件的功能点 + 场景 + 测试点，三个分片 | 文件数（3–10） |
| B | 功能点批次 | 测试用例分片 | 4–8 |
| C | 用例分片 | 冗余/遗漏审查意见 | 0–3（用例多才开） |

为什么把前三级压在同一个 agent 里：这三级的推导共享同一份文档上下文。拆成三次扇出，第二轮的 agent 要重读 01、第三轮又要重读 02，每次都在重建同样的理解，既慢又容易漂。同一个 agent 连续推导"这个功能点有哪些场景、每个场景拆成哪些测试点"，判断是连贯的。

第 4 级必须单独扇出：用例数量是前三级的好几倍，一个 agent 写全部用例会超长。

## 谁来做合并与校验

**编排者自己用 bash 做，不要为每级开一个合并 agent。** 这是把流水线从二十多轮压到十轮左右的关键：

- 合并分片：`python3 <skill>/scripts/merge_shards.py ...`，一次 bash 调用。
- 结构校验：`python3 <skill>/scripts/validate_cases.py ...`，一次 bash 调用。
- 出报告：`python3 <skill>/scripts/render_report.py ...`，一次 bash 调用。

只有当脚本报出**重复 ID 或语义重复**时，才值得开一个 agent 来做语义归一。脚本能确定性完成的活，交给脚本。

## 预算：agent 数是有上限的

- **总 agent 数控制在 12 个以内。** 超过通常意味着该调整批次大小，而不是流水线更长。
- **每轮并行 3–8 个。** 扇出太细，每个 agent 都要重读 schema 和上游文件，固定开销吃掉并行收益，总耗时反而更长。
- **优先保证产出完整**：如果预算吃紧，砍掉第 C 轮审查，也要先写出 `testcases.yaml` 和报告。半套带审查的流程不如一整套能用的用例集。
- 用例超过 80 条时，才开第 C 轮；否则编排者自己逐条扫一遍更快。

同一篇知识文档不要按章节拆给多个 agent。章节之间有前提依赖——"自由上下班只统计旷工"的前提在上文，"按班次"的例外在另一节——拆开就会各自误读。

## 脚本骨架

路径一律用绝对路径。`workflow` 脚本没有文件系统访问能力，只能靠 `args` 和 agent 返回值传递路径；合并、校验这些文件操作要么由 agent 用 bash 做，要么由编排者在 workflow 之外做。

```js
// args 约定：
// {
//   topic: "attendance-management",
//   title: "打卡管理",
//   abbr: "ATT",
//   skillDir: "/abs/.agents/skills/testcase-generator",
//   projectRoot: "/abs/project",
//   sources: [{ path: "wiki/attendance-management/a.md", title: "打卡规则配置" }, ...]
// }

const A = args;
const DIR = `${A.projectRoot}/testcases/${A.topic}`;
const SH = `${DIR}/.shards`;
const SK = A.skillDir;

const SHARD = {
  type: "object",
  properties: {
    files: { type: "array", items: { type: "string" } },
    counts: { type: "object" },
    summary: { type: "string" },
  },
  required: ["files"],
  additionalProperties: true,
};

const chunk = (arr, n) => {
  const out = [];
  for (let i = 0; i < arr.length; i += n) out.push(arr.slice(i, i + n));
  return out;
};

// ---- 轮次 A：按文件扇出，一个 agent 连推三级 ----
phase("A 功能点-场景-测试点");
const aResults = await parallel(
  A.sources.map((src, i) => () =>
    agent(
      `你是测试用例流水线的「分析」agent，负责一篇知识文档的三级推导。

知识文件：${A.projectRoot}/${src.path}（标题：${src.title}）
只读这一个文件。不要读其他文件，不要补外部常识。

按顺序做三步，每步都把结果写成 YAML 分片（字段与粒度规则见 ${SK}/references/yaml-schema.md）：
1. 功能点分析 → ${SH}/functional-points/fp-${i}.yaml
   按"业务对象/规则簇"切，不按页面、不按章节顺次切。一篇文档切出 3–8 个功能点比较正常。
   source_ref.section 必须是文件里真实存在的标题，逐字抄。
2. 场景分析 → ${SH}/scenarios/sc-${i}.yaml
   每个功能点按关注面过一遍：正常主链路、分支/可选配置、边界与阈值、异常与非法输入、
   权限与越权、状态流转、数据一致性、并发、安全/防作弊。用不上的跳过，但主链路和至少一条负向不能跳。
   用刚切出的功能点编号，ID 形如 SC-FP-01-01。
3. 测试点设计 → ${SH}/test-points/tp-${i}.yaml
   把每个场景拆成可判定真伪的测试点，带逐字 evidence.quote。
   methods 必须对十种核心方法（边界值法、等价类划分、错误推测法、穷举法、逆向场景法、
   因果图法、场景法、异常法、并发法、安全性法）逐一表态：适用进 applicable，不适用进 skipped 并写原因。
   原因要可反驳（"无共享可变状态"），不要写"不适用"。

写每个文件后用 python3 -c 确认可解析。最后返回产出文件路径与各级数量。`,
      { label: `analyze-${i + 1}`, phase: "A 功能点-场景-测试点", schema: SHARD }
    )
  )
);
log(`分析分片：${aResults.filter(Boolean).length}/${A.sources.length} 成功`);
```

轮次 A 结束后，编排者在 workflow 之外（或用一个 agent）跑三次合并：

```bash
python3 $SK/scripts/merge_shards.py --stage functional-points \
    --shard-dir $SH/functional-points --out $DIR/01-functional-points.yaml --topic $A --title $B
python3 $SK/scripts/merge_shards.py --stage scenarios \
    --shard-dir $SH/scenarios --out $DIR/02-scenarios.yaml --topic $A --title $B
python3 $SK/scripts/merge_shards.py --stage test-points \
    --shard-dir $SH/test-points --out $DIR/03-test-points.yaml --topic $A --title $B
```

合并脚本会报出重复 ID。有冲突时开一个归一 agent 处理；没有冲突就直接进轮次 B。

```js
// ---- 轮次 B：按功能点批次扇出用例 ----
phase("B 测试用例");
const FPS = await agent(
  `读 ${DIR}/01-functional-points.yaml，只返回功能点清单（id/name/module）。`,
  {
    label: "read-fp",
    phase: "B 测试用例",
    schema: {
      type: "object",
      properties: {
        fps: {
          type: "array",
          items: {
            type: "object",
            properties: {
              id: { type: "string" },
              name: { type: "string" },
              module: { type: "string" },
            },
            required: ["id", "name", "module"],
          },
        },
      },
      required: ["fps"],
    },
  }
);

const fpChunks = chunk(FPS.fps, Math.max(2, Math.ceil(FPS.fps.length / 4)));

const casePrompt = (batch, idx) => `你是「测试用例设计」agent。

读 ${DIR}/03-test-points.yaml，只处理这几个功能点下的测试点：
${batch.map((f) => `${f.id}（${f.name}）`).join("、")}

对每个测试点：
1. 看 methods.applicable，用每种适用方法各生成用例；一条用例只用一个主方法、只判一件事。
2. 用例字段见 ${SK}/references/yaml-schema.md 的「四级」。
   ID 规则：TC-${A.abbr}-<FF><SS>，FF 取所属功能点两位序号，SS 在本批号段内递增，避免与他人撞号。
3. source_context.quote 从测试点的 evidence.quote 抄，一个字都不要改。
4. 知识没写清的地方不许编预期：status 写 pending_confirmation，
   expected 写"待确认：<具体问题>"，并在文件末尾 open_items 登记。
5. 同一动作不同输入的（数据驱动）合成一条用例的多个数据行，不拆成多条。

写入 ${SH}/cases/case-${idx}.yaml。返回文件路径与该批用例数。`;

// 分两半发，跑完一半就先合并落盘，任何时刻中断磁盘上都有可用用例集
const mid = Math.ceil(fpChunks.length / 2);
const halves = [fpChunks.slice(0, mid), fpChunks.slice(mid)];
for (let h = 0; h < halves.length; h++) {
  const half = halves[h];
  const offset = h === 0 ? 0 : mid;
  const shards = await parallel(
    half.map((batch, i) => () =>
      agent(casePrompt(batch, offset + i), {
        label: `cases-${offset + i + 1}`,
        phase: "B 测试用例",
        schema: SHARD,
      })
    )
  );
  log(`本轮完成 ${shards.filter(Boolean).length}/${half.length} 个用例分片，先合并落盘`);
  // 编排者在 workflow 之外跑 merge_shards.py --stage cases 覆盖写 testcases.yaml
}


读 ${DIR}/03-test-points.yaml，只处理这几个功能点下的测试点：
${batch.map((f) => `${f.id}（${f.name}）`).join("、")}

对每个测试点：
1. 看 methods.applicable，用每种适用方法各生成用例；一条用例只用一个主方法、只判一件事。
2. 用例字段见 ${SK}/references/yaml-schema.md 的「四级」。
   ID 规则：TC-${A.abbr}-<FF><SS>，FF 取所属功能点两位序号，SS 在本批号段内递增，避免与他人撞号。
3. source_context.quote 从测试点的 evidence.quote 抄，一个字都不要改。
4. 知识没写清的地方不许编预期：status 写 pending_confirmation，
   expected 写"待确认：<具体问题>"，并在文件末尾 open_items 登记。
5. 同一动作不同输入的（数据驱动）合成一条用例的多个数据行，不拆成多条。

写入 ${SH}/cases/case-${i}.yaml。返回文件路径与该批用例数。`,
      { label: `cases-${i + 1}`, phase: "B 测试用例", schema: SHARD }
    )
  )
);
```

轮次 B 是整条流水线最长的一段，所以上面把批次分两半发：跑完一半就先合并落盘一次。这样任何时刻中断，磁盘上都已有一个可用的 `testcases.yaml`。合并用覆盖写而不是追加，重复合并是幂等的。

轮次 B 之后的标准收尾：

```bash
python3 $SK/scripts/merge_shards.py --stage cases \
    --shard-dir $SH/cases --out $DIR/testcases.yaml --topic $A --title $B
python3 $SK/scripts/validate_cases.py --topic-dir $DIR --corpus <知识目录> --abbr $A
python3 $SK/scripts/render_report.py --topic-dir $DIR --out $DIR/coverage-report.md
```

注意 `$SH/cases` 里可能混着 agent 自己放的辅助文件（批次输入、临时脚本），合并脚本只读 `.yaml`/`.yml`，其余会被忽略——但如果某个临时 YAML 也带 `cases` 键就会被误并，所以临时文件用 `.json` 或换个目录。

硬失败非零就先修再出报告。用例超过 80 条时再开轮次 C。

```js
// ---- 轮次 C（可选）：审查 ----
if (caseShards.filter(Boolean).length > 0) {  // 仅在用例多时执行
  phase("C 审查");
  await parallel(
    ["S1-S3 核心链路", "S4-S5 边界与展示"].map((scope, i) => () =>
      agent(
        `你是「用例审查」agent，负责 ${scope} 范围内的用例。

只做四件事，每件都必须给具体用例 ID 和具体改法：
1. 冗余：前置+动作+判定对象实质相同的用例组，指出保留哪条、并入什么。
2. 遗漏：对照知识原文的限定词（必须/只能/不得/超过/默认），指出缺的测试点。
3. 定级：只报你认为定级错误的，写明失败后果和应改等级。
4. 可执行性：前置/步骤/预期不可执行的，指出具体哪一项。

不要改写用例正文，不要输出整份文件。每项一行：问题类型 | 用例ID | 问题 | 建议改法。
没有问题就明确说"无"。`,
        { label: `audit-${i + 1}`, phase: "C 审查" }
      )
    )
  );
}
```

## 各轮 agent 指令模板

模板必须包含五件事，缺一件产出就会漂：**只读哪份文件**、**产出写到哪个绝对路径**、**字段去读哪个 reference**、**ID 规则**、**一条最容易被违反的约束**。

| 轮次 | 必须强调的约束 |
| --- | --- |
| A 功能点 | 按业务对象切，不按章节顺次切；section 逐字抄 |
| A 场景 | 主链路 + 至少一条负向；描述不含动作与预期 |
| A 测试点 | 证据逐字；十种核心方法逐一表态 |
| B 用例 | 一条一个目标；`quote` 逐字；无据的写 `pending_confirmation` |
| C 审查 | 只报问题和改法，不重写整份文件 |

## 失败与续跑

`agent()` 失败返回 `null`，不抛异常。三条规则：

```js
const results = await parallel(tasks);
const missing = results.map((r, i) => (r ? null : i)).filter((i) => i !== null);
const retried = await parallel(
  missing.map((i) => () => agent(taskPrompt(i), { label: `retry-${i}`, schema: SHARD }))
);
```

- **补跑一次**。网络抖动补跑就好；上下文溢出会稳定失败——补跑还失败就把那批拆小。
- **不要静默吞掉**。少一个分片意味着漏掉一块知识，而后续阶段看起来都正常。最终报告里写明"X 个分片失败，已补跑/未覆盖"。
- **分片失败不阻断后续**。先跑完再把缺口补上，比卡住好。

**续跑比补跑更重要。** 每轮落盘后，`01/02/03/testcases.yaml` 就是续跑点。如果上下文将满或流水线中途断掉，不要从头再来：

1. `ls` 出已有产物，确认最高完成的级别。
2. 从下一级继续，用已落盘的文件作为上游。
3. 分片目录 `$SH` 保留不动，缺哪片补哪片。

**中断在轮次 B 中途是最常见的情况**，处理起来也最省：`.shards/cases/` 里已完成的分片全都有效。先数一下分片数是否等于批次总数，缺的那批补跑，然后合并 + 校验 + 出报告即可——不必重新设计任何用例。这也是为什么轮次 B 要分两半发：断掉的时候，磁盘上往往已经有一个能交付的 `testcases.yaml`。

编排者要在开始时就检查 `$DIR` 是否已有产物——已有就直接续，别覆盖重做。看到 `03-test-points.yaml` 已经存在却还在跑轮次 A，说明它在做无用功。

## 没有 workflow 时的串行降级

`workflow` 不可用时，用同样的三轮结构串行完成，落盘同样的文件：

1. 用 `bash` 列目录、`read` 读知识文件（一次一篇，读完立刻连推三级并写分片，不要一次读完全部）。
2. 每篇写完跑一次 `merge_shards.py` 增量合并。
3. 用例阶段按功能点逐批写，每批写完立刻跑 `validate_cases.py`，别攒到最后。

降级只影响速度，不影响质量要求。**不要因为不能并行就跳过中间产物**——跳过就等于放弃追溯和覆盖度计算，这套流程的价值也就没了。

## 实操检查点

三个最容易出问题的地方：

1. **重编号没同步下游**。第 2 级重排了 FP 序号，第 4 级用例 ID 还带旧序号，合并后 `fp_id` 对不上。每级合并后立刻跑校验。
2. **分片写到相对路径**。agent 的工作目录可能不同，一律用绝对路径；合并前 `ls` 一下分片数是否等于预期。
3. **agent 互相覆盖**。多个 agent 写同名文件会静默丢数据，分片文件名必须带批次序号（`case-3.yaml`），合并脚本按 ID 去重而不是按文件名。

还有一个不是坑但常被忽略的点：**编排者自己的预算是有限资源**。看到自己已经发了十几轮 `agent()`，就该判断是不是该收口——先落盘、先出报告，把审查留到下一轮。能交付的八成，好过跑到一半断掉的九成。
