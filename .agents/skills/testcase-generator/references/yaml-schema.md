# YAML 数据模型

四份文件共用 `meta` 头，各自有一个顶层列表键。字段名用英文是为了脚本可校验、可 diff；语义中文对照见下表。

```yaml
meta:
  topic: attendance-management          # 目录 slug，与 testcases/<topic>/ 一致
  title: 打卡管理                        # 人读标题
  stage: functional-points               # 本文件所处级别
  generated_at: "2026-10-19"             # ISO 日期，字符串要加引号
  generator: testcase-generator
  knowledge_sources:
    - path: wiki/attendance-management/attendance-rule-configuration.md
      title: 打卡规则配置
```

`path` 一律写**相对项目根目录**的路径。写绝对路径会让产出在别的机器上无法复查。

---

## 一级：01-functional-points.yaml

```yaml
meta: { topic: attendance-management, title: 打卡管理, stage: functional-points }

functional_points:
  - id: FP-01
    name: 规则类型与统计口径
    module: 规则配置
    summary: 三种规则类型（固定时间/按班次/自由上下班）决定考勤统计口径，自由上下班只统计旷工
    source_ref:
      path: wiki/attendance-management/attendance-rule-configuration.md
      section: 三种规则类型
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | `FP-01` 起，两位以上递增，不复用、不重排 |
| `name` | 是 | 一句话能力名，产品经理会认可的那种 |
| `module` | 是 | 归属模块，用于分组和审查分片 |
| `summary` | 是 | 2–3 句，说清这个功能点管什么、边界在哪 |
| `source_ref.path` | 是 | 出处文件 |
| `source_ref.section` | 是 | 原文标题（不带 `#`） |

**切分粒度**：一个功能点 = 一个可独立验证的业务能力。检验方式：如果它的预期结果必须和另一个功能点的预期结果一起才能判定，说明该合并；如果它内部有两套完全独立的判定规则，说明该拆。

---

## 二级：02-scenarios.yaml

```yaml
meta: { topic: attendance-management, title: 打卡管理, stage: scenarios }

scenarios:
  - id: SC-FP-01-01
    fp_id: FP-01
    name: 三种规则类型的统计口径
    description: 分别创建固定时间、按班次、自由上下班三类规则，验证各自产出的考勤判定
    basis: 文档「三种规则类型」表格
    focus: [正向, 数据准确性]
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | `SC-FP-<FP两位>-<序号两位>` |
| `fp_id` | 是 | 必须存在于功能点清单 |
| `name` | 是 | 场景名，短 |
| `description` | 是 | "在什么条件下验什么"，不含动作、不含预期 |
| `basis` | 是 | 场景的需求依据（章节或一句话），说明为什么这个场景值得测 |
| `focus` | 否 | 标签数组，从标签词表取，标出这个场景的重点关注面 |

---

## 三级：03-test-points.yaml

```yaml
meta: { topic: attendance-management, title: 打卡管理, stage: test-points }

test_points:
  - id: TP-FP-01-01-01
    scenario_id: SC-FP-01-01
    statement: 自由上下班规则下不产出迟到/早退判定，只统计旷工
    evidence:
      quote: 自由上下班只统计旷工，工作时长仅作记录，不按固定时间判定上下班。
      path: wiki/attendance-management/attendance-rule-configuration.md
      section: 三种规则类型
    methods:
      applicable: [等价类划分, 逆向场景法]
      skipped:
        - method: 并发法
          reason: 判定只依赖单人当日打卡，无共享可变状态
        - method: 边界值法
          reason: 该判定无数值阈值
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | `TP-FP-<FP两位>-<SC两位>-<序号两位>` |
| `scenario_id` | 是 | 必须存在于场景清单 |
| `statement` | 是 | 一句可判定真伪的话，读完知道怎么验 |
| `evidence.quote` | 是 | **逐字**引用原文，不改写、不拼接 |
| `evidence.path` / `section` | 是 | 引用出处 |
| `methods.applicable` | 是 | 适用方法数组，非空，取自设计方法词表 |
| `methods.skipped` | 是 | 不适用方法及原因；原因要具体，写"不适用"不算原因 |

十种核心方法（边界值法、等价类划分、错误推测法、穷举法、逆向场景法、因果图法、场景法、异常法、并发法、安全性法）**必须逐一出现在 `applicable` 或 `skipped` 里**。漏掉的方法会被校验脚本点名为未过筛——逐点过筛的价值就在于"我知道自己没用它，也知道为什么"，漏掉等于没想过。

`methods.applicable` 是第四级的分母：标了适用的方法，最终必须产出用例，否则校验脚本告警。

---

## 四级：testcases.yaml（交付物）

```yaml
meta:
  topic: attendance-management
  title: 打卡管理
  stage: cases
  version: v1.0
  generated_at: "2026-10-19"
  generator: testcase-generator
  knowledge_sources:
    - path: wiki/attendance-management/attendance-rule-configuration.md
      title: 打卡规则配置
  stats:
    functional_points: 14
    scenarios: 42
    test_points: 108
    cases: 137

open_items:
  - id: OI-01
    question: 外出打卡时缺定位或缺照片，是阻断打卡还是仅记录异常？
    related_case_ids: [TC-ATT-0703]
    source_ref: wiki/attendance-management/attendance-rule-configuration.md

cases:
  - id: TC-ATT-0101
    fp_id: FP-01
    scenario_id: SC-FP-01-01
    point_id: TP-FP-01-01-01
    functional_point: 规则类型与统计口径
    scenario: 规则采用自由上下班类型
    test_point: 自由上下班不产出迟到/早退判定，只统计旷工
    title: 用自由上下班规则打卡后检查考勤统计结果
    design_method: 等价类划分
    tags: [正向, 数据准确性]
    severity: S1
    severity_reason: 统计口径错误会让整批考勤结论失真，且难以事后修正
    preconditions: 已创建自由上下班规则并绑定测试部门成员；当日为工作日
    steps:
      - 员工使用手机打卡完成一次上下班打卡
      - 进入【管理后台】>【企业应用】>【打卡】>【上下班打卡】>【查看】
      - 打开该员工当日考勤明细，查看统计列
    expected:
      - 考勤明细中不出现迟到、早退判定
      - 该员工当日旷工统计为 0
      - 工作时长按实际打卡区间记录
    status: active
    source_context:
      quote: 自由上下班只统计旷工，工作时长仅作记录，不按固定时间判定上下班。
      path: wiki/attendance-management/attendance-rule-configuration.md
      section: 三种规则类型
```

### 用例字段全表

| 字段 | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| `id` | 是 | str | `TC-<ABBR>-<FF><SS>`，见下方 ID 约定 |
| `fp_id` | 是 | str | 所属功能点 |
| `scenario_id` | 是 | str | 所属场景 |
| `point_id` | 是 | str | 所属测试点 |
| `functional_point` | 是 | str | 用例所属功能点（人读），与 `fp_id` 指向的 `name` 一致 |
| `scenario` | 是 | str | 测试场景：在什么条件下 |
| `test_point` | 是 | str | 测试点：要判定什么 |
| `title` | 是 | str | 测试用例：做什么动作 |
| `design_method` | 是 | enum | 单个主方法，取自词表 |
| `tags` | 是 | list | 1–3 个，取自标签词表 |
| `severity` | 是 | enum | `S1`–`S5` |
| `severity_reason` | 是 | str | 定级依据，写失败后果，不写"很重要" |
| `preconditions` | 是 | str | 环境与数据；不写动作 |
| `steps` | 是 | list | 2–6 步，每步一个动作，不嵌预期 |
| `expected` | 是 | list | 测试结果（预期），每项可判定 |
| `actual` | 否 | str | 测试结果（实际），执行后回填；未执行时省略 |
| `status` | 是 | enum | `active` / `pending_confirmation` |
| `source_context` | 是 | obj | `quote`（逐字）+ `path` + `section` |
| `notes` | 否 | str | 补充说明，不放预期 |

### ID 约定

| 层级 | 格式 | 示例 | 含义 |
| --- | --- | --- | --- |
| 功能点 | `FP-<nn>` | `FP-03` | 第 3 个功能点 |
| 场景 | `SC-FP-<nn>-<nn>` | `SC-FP-03-02` | FP-03 的第 2 个场景 |
| 测试点 | `TP-FP-<nn>-<nn>-<nn>` | `TP-FP-03-02-05` | 该场景第 5 个测试点 |
| 用例 | `TC-<ABBR>-<FF><SS>` | `TC-ATT-0305` | ABBR 为项目缩写；FF 功能点序号；SS 用例序号 |

`ABBR` 由 topic 推导，三字母大写（`attendance-management` → `ATT`）。用例 ID 里嵌功能点序号不是装饰：**看到 `TC-ATT-0305` 就知道它属于第 3 个功能点**，审查时不用翻表。每个功能点上限 99 条用例，超出说明该功能点该拆了。

一个 agent 负责多个功能点时，务必按各自 FP 序号取号段，避免跨 agent 撞号。

### 枚举

**设计方法** `design_method` / `methods.applicable`：

`边界值法` `等价类划分` `错误推测法` `穷举法` `逆向场景法` `因果图法` `场景法` `异常法` `并发法` `安全性法` `判定表法` `状态迁移法`

**标签** `tags`：`正向` `异常` `边界` `权限` `数据准确性` `数据一致性` `冒烟` `回归` `安全` `并发` `集成` `导出` `可用性` `待确认`

标签最多 3 个。一条用例同时挂 `正向` 和 `异常` 通常说明它有两个目标，该拆。

**状态** `status`：`active`（需求有据） / `pending_confirmation`（知识未定义，预期待确认）

**等级** `severity`：`S1` 致命 / `S2` 严重 / `S3` 一般 / `S4` 轻微 / `S5` 建议

---

## `open_items`

知识缺口登记表。凡是 `status: pending_confirmation` 的用例，都要在这里有一条记录，说明到底要确认什么。

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | `OI-01` 起 |
| `question` | 是 | 具体到可回答的问题，不要写"需求不清晰" |
| `related_case_ids` | 是 | 受影响的用例 ID 列表 |
| `source_ref` | 否 | 缺口所在文档，便于回去补 |

`question` 的检验标准：产品经理读完能直接回答"是/否"或给出一个数值。"外出打卡缺照片是否阻断"是好问题；"外出打卡的规则不完善"不是。

---

## 三个一句话字段的分工

| 字段 | 回答 | 打卡示例 |
| --- | --- | --- |
| `scenario` | 在什么条件下 | 规则采用自由上下班类型 |
| `test_point` | 要判定什么 | 自由上下班不产出迟到/早退判定 |
| `title` | 做什么动作 | 用自由上下班规则打卡后检查统计结果 |

通读成「在**场景**下，验证**测试点**，操作为**用例**」。读不通就是三个字段混了。

## 写作对照

| 差 | 好 | 为什么 |
| --- | --- | --- |
| 验证打卡是否正常 | 定位在有效范围内打卡成功，记录归属该员工当日考勤 | 前者永远不会失败 |
| 提示信息友好 | 提示「不在打卡范围内」，且不生成打卡记录 | "友好"不可判定 |
| 测试超时锁定 | （知识未定义）待确认：超时后是否锁定 | 需求没写就不能断言 |
| 前置：打开页面，选择规则，点击保存 | 前置：规则已保存并绑定测试部门；步骤：员工打卡 | 前置写成了步骤 |
