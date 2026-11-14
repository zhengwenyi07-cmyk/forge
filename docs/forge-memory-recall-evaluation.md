# Forge Memory Recall Benchmark：设计、结果与 Agent A/B 方案

## 1. 结论先行

本次建立了一套完全离线、确定性、零模型调用的 Memory Recall Benchmark。它验证的不是“SQLite 能不能写入”，而是：在仓库、commit、路径、符号、验证状态和重复记忆互相干扰时，Forge 能否把正确历史经验排到前面，并正确标记 stale 或失效。

最终完整排序方案在冻结集上的结果为：

- Recall@1 / Recall@3 / Recall@5：`1.000 / 1.000 / 1.000`
- Precision@1 / Precision@3 / Precision@5：`1.000 / 0.333 / 0.200`
- MRR：`1.000`
- 正确文件命中率 / 正确符号命中率：`1.000 / 1.000`
- 跨仓库错误召回率：`0.000`
- stale 识别率 / 失效准确率：`1.000 / 1.000`
- 重复结果率 / 截断率：`0.000 / 0.000`
- 平均 / P95 查询延迟：`12.04 ms / 35.09 ms`
- 平均返回 Token：`400.3`
- 模型/API 调用：`0`

Precision@3 和 Precision@5 的分母固定为 3 和 5，而每个正查询只有一个 ground-truth memory，所以其理论上限分别就是 `1/3` 和 `1/5`；这里的较小数值不是误召回率高。

这些结果证明了冻结合成场景中的离线召回质量，但不能证明 Agent 已使用记忆，更不能单独证明 SWE-bench 能力提升。后两项必须通过第二阶段的 memory off/on 配对实验验证。

## 2. 产物与复现

提交到 Git 的确定性产物：

- `evaluation/memory_recall_manifest.json`：冻结记忆、查询和逐查询 ground truth。
- `evaluation/memory_recall_benchmark.py`：六组消融、逐查询打分、错误归因、JSON/Markdown 输出入口。
- `tests/test_memory_recall_benchmark.py`：冻结清单完整性和关键指标防退化测试。
- 本文档：实验定义、证据、限制和后续 A/B 方案。

本地运行产物位于 Git 忽略目录：

- `evaluation/runs/memory-recall/20261115-final/results.json`：机器可读完整结果。
- `evaluation/runs/memory-recall/20261115-final/report.md`：六组消融的逐查询人类可读报告。
- 同目录的 `databases/` 和 `workspaces/`：每组消融独立的 SQLite 与合成仓库。

复现命令：

```powershell
.\.venv\Scripts\python.exe -m evaluation.memory_recall_benchmark `
  --manifest evaluation/memory_recall_manifest.json `
  --output evaluation/runs/memory-recall/<new-run-id>
```

入口拒绝覆盖已有 `results.json`，避免误把两次实验写到同一目录。每个输出都记录 manifest SHA-256、Python/平台信息、权重、逐查询候选和分数组成，并显式记录 `model_calls: 0`。

## 3. 冻结测试集设计

清单使用两个彼此隔离的合成 Git 仓库和 19 条历史记忆。每个仓库设置不同的规范 remote identity，避免临时目录位于 Forge 工作树下时被错误识别成同一个仓库。12 个查询覆盖：

1. 相似 Issue 召回；
2. 错误信息召回；
3. 文件路径召回；
4. qualified symbol 召回；
5. 成功经验与失败经验冲突；
6. 相同仓库、不同 commit；
7. 其他仓库的同词干扰；
8. 关联文件删除后的失效；
9. 重复记忆去重；
10. 中文查询；
11. 词法不同但含义相近的查询；
12. 正确答案本身已经 stale 的查询。

每条查询都冻结以下 ground truth：

- `expected_memory_ids`
- `correct_files`
- `correct_symbols`
- `should_stale`
- `should_invalidate`

删除文件场景是负查询：它不参与 Recall、Precision、MRR、文件和符号命中率，而由失效准确率单独衡量。Recall 与 Precision 以 Token 预算后真正交付的结果计算，不能用数据库中“搜到但未返回给 Agent”的隐藏候选抬高指标。

## 4. 指标定义

- **Recall@k**：前 k 个已交付结果覆盖 ground truth 的比例。
- **Precision@k**：前 k 个位置中 ground-truth 命中的数量除以 k。
- **MRR**：第一个正确 memory 的排名倒数；未找到为 0。
- **正确文件/符号命中率**：返回结果是否包含 ground truth 文件或 qualified symbol。
- **跨仓库错误召回率**：错误 repository_id 结果数除以全部返回结果数。
- **stale 识别率**：期望 memory 是否被召回，且其 stale 标记是否与 ground truth 一致。
- **失效准确率**：应失效的记录确实被持久化标记且不再返回；其余记录没有被误失效。
- **重复结果率**：重复 memory id 与重复 content hash 数除以全部返回结果数。
- **平均/P95 延迟**：`search + budget render` 的本机 wall time。
- **平均返回 Token / 截断率**：按 Forge 的预算估算器统计实际交付文本。

延迟仅用于这套小型合成数据的回归和方向比较，不代表生产规模负载测试。

## 5. 排序消融结果

| 方案 | R@1 | R@3 | R@5 | MRR | 文件 | 符号 | stale | 失效 | 跨仓库 | 重复 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| lexical only | 0.545 | 0.727 | 0.727 | 0.636 | 0.818 | 0.818 | 0.727 | 1.000 | 0.000 | 0.000 |
| lexical + path | 0.636 | 0.818 | 0.818 | 0.727 | 0.909 | 0.909 | 0.818 | 1.000 | 0.000 | 0.000 |
| lexical + path + symbol | 0.818 | 0.909 | 0.909 | 0.864 | 1.000 | 1.000 | 0.909 | 1.000 | 0.000 | 0.000 |
| full，关闭 fingerprint | 0.909 | 0.909 | 0.909 | 0.909 | 1.000 | 1.000 | 0.909 | 1.000 | 0.000 | 0.000 |
| full，关闭 validation | 0.909 | 1.000 | 1.000 | 0.955 | 1.000 | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 |
| 当前 full | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 |

消融说明：

- lexical only 关闭路径、符号、fingerprint、validation、confidence、recency、stale 和 repeat 信号；候选生成也不接收路径和符号 hint。
- 后两组逐步加入 path、symbol。
- full no fingerprint 同时关闭 exact-fingerprint 奖励和 stale 惩罚，防止“关闭奖励但保留惩罚”的不公平消融。
- full no validation 只关闭验证状态信号。
- full 使用配置中全部可记录权重。

结果表明结构化路径和符号信号分别带来稳定增益；fingerprint 与 validation 对不同冲突场景都提供了额外区分力。完整方案不是靠某一个信号取胜。

## 6. 完整方案逐查询结果

| 查询 | 场景 | 正确排名 | 文件 | 符号 | stale | 失效 | 返回 Token | 延迟 ms |
| --- | --- | ---: | --- | --- | --- | --- | ---: | ---: |
| q01 | 相似 Issue | 1 | ✓ | ✓ | ✓ | ✓ | 751 | 15.34 |
| q02 | 错误信息 | 1 | ✓ | ✓ | ✓ | ✓ | 183 | 6.67 |
| q03 | 文件路径 | 1 | ✓ | ✓ | ✓ | ✓ | 464 | 11.61 |
| q04 | qualified symbol | 1 | ✓ | ✓ | ✓ | ✓ | 319 | 9.25 |
| q05 | 成功/失败冲突 | 1 | ✓ | ✓ | ✓ | ✓ | 304 | 8.14 |
| q06 | 同仓库不同 commit | 1 | ✓ | ✓ | ✓ | ✓ | 730 | 35.09 |
| q07 | 跨仓库干扰 | 1 | ✓ | ✓ | ✓ | ✓ | 184 | 6.34 |
| q08 | 文件删除失效 | 不应返回目标记忆 | — | — | — | ✓ | 174 | 6.27 |
| q09 | 重复记忆 | 1 | ✓ | ✓ | ✓ | ✓ | 458 | 12.17 |
| q10 | 中文查询 | 1 | ✓ | ✓ | ✓ | ✓ | 173 | 6.26 |
| q11 | 语义相近、词法不同 | 1 | ✓ | ✓ | ✓ | ✓ | 745 | 18.14 |
| q12 | 正确答案为 stale | 1 | ✓ | ✓ | ✓ | ✓ | 319 | 9.18 |

完整 ID、返回顺序、分数组成和每组消融的逐查询结果没有省略，均保存在本地 `report.md` 与 `results.json`。

## 7. 错误案例与基于证据的调权

### 7.1 预调参错误

干净的预调参实验保存在忽略目录 `20261114-pretune`。当时 full 的结果为：

- Recall@1：`0.909`
- Recall@5：`1.000`
- MRR：`0.955`

唯一排序错误是 q12：正确的 stale 经验排第 2。其分数为 `4.38`，而一个只具备 `1/6` 词法重合、但 fingerprint 完全一致的无关记忆得 `5.80`。分解后可得到：

- 正确 stale 经验：固定正向分 `5.88 - stale_weight`
- 无关 exact 经验：固定正向分 `3.30 + fingerprint_weight`
- 要让前者胜出，需要 `fingerprint_weight + stale_weight < 2.58`

旧权重为 `2.5 + 1.5 = 4.0`，commit 信号明显压过任务语义。采用保守的新权重：

- fingerprint：`2.5 → 1.5`
- stale：`1.5 → 0.75`
- 合计：`2.25`，低于数据给出的 `2.58` 上界，同时仍保留明显的同 commit 偏好。

复跑后 q12 升至第 1，full 的 Recall@1 与 MRR 都达到 1.0；同仓库不同 commit、成功/失败冲突、其他十个查询没有退化。因此保留该调整。这里没有根据感觉搜索最优值，也没有反复调到极限边界。

### 7.2 各类错误分析

- **没有召回**：full 无；lexical-only 在结构化提示和中文/语义改写场景会漏召回。
- **排名过低**：full 无；预调参 q12 已通过上述证据修复。
- **错误仓库**：所有组均为 0。repository_id 是过滤边界，不只是排序特征。
- **stale 判断错误**：full 无；文件删除样本走 invalidation，不混入 stale 指标。
- **失败经验压过成功经验**：full 无；validation 消融使 R@1 从 1.0 降到 0.909，证明该信号有实际贡献。
- **过度依赖精确关键词**：q11 在 lexical-only 中不能排第 1；路径/符号和其他结构化信号让 full 排第 1。第一版仍没有 embedding，这个样本证明的是结构化语义桥接，不等价于通用语义向量召回。

中文查询额外使用连续 CJK 文本与字符 bigram，并通过参数化 `LIKE` 做召回候选；这是因为 SQLite 默认 FTS5 Unicode tokenizer 对无空格中文改写不能提供可靠的分词匹配。英文仍优先使用 FTS5。

## 8. 自动化防退化

`tests/test_memory_recall_benchmark.py` 包含两层保护：

1. 冻结清单必须包含全部 12 种场景，以及 memory id、文件、符号、stale 和 invalidation ground truth。
2. 在临时目录真实运行 lexical-only 与 full：
   - `model_calls == 0`
   - full R@1 不低于 0.90，R@3/R@5 必须为 1.0，MRR 不低于 0.95
   - 文件、符号、stale、失效必须为 1.0
   - 跨仓库与重复结果必须为 0
   - full 的 R@1 和 MRR 必须严格优于 lexical-only
   - 结果文件必须存在，重复使用同一输出目录必须拒绝

门槛不是把本机毫秒延迟写死，因为 CI 机器性能不同；延迟仍完整记录，适合趋势监控。

## 9. 四个需要严格区分的结论

### 9.1 存储与恢复正确性

由现有 memory 专项测试覆盖：SQLite schema、跨进程恢复、备份、并发原子写、checkpoint、去重、失效、反馈和敏感信息净化。它回答“记忆有没有安全、持久地保存下来”。

### 9.2 离线召回质量

由本 benchmark 覆盖。它回答“给定历史记忆和查询，正确记忆能不能在预算内被排到前面”。当前冻结合成集 full 为 R@1/MRR 1.0。

### 9.3 Agent 是否真正使用了记忆

当前离线 benchmark 不能证明。第二阶段必须从运行日志中观察 memory_search/memory_get、被引用 memory id、首次找到最终修改文件的步骤，以及 memory on/off 行为差异。

### 9.4 真实任务能力是否提升

只能由同模型、同任务、同 Repo-map 与 Token 配置的配对 SWE-bench 官方 Docker 结果说明。即便 Agent 调用了记忆，如果 resolved 没提升或负迁移增加，也不能宣称能力提升。

## 10. 第二阶段：低成本 memory off/on 配对 A/B（仅方案，尚未运行）

### 10.1 任务选择

建议选 6 题，兼顾成本与仓库多样性。确定性规则：

1. 读取原冻结 `evaluation/swebench30.json`，不修改它。
2. 计算 `SHA256("forge-memory-ab-v1:" + instance_id)` 并升序排序。
3. 每个仓库只取第一题，直到取得 6 个不同仓库。
4. 把算法、源 manifest SHA-256、题目和设置保存为新的冻结 manifest；禁止根据预期难度或记忆是否可能有帮助挑题。

memory on 的历史语料必须使用该仓库其他任务的既有运行产物，且严格排除目标题本身、gold patch 和官方测试答案，形成 leave-one-target-out 语料。这样能避免把目标答案伪装成“记忆”。memory off 使用相同代码和设置，只关闭检索/注入。

### 10.2 严格控制变量

- 模型：仅 `mimo-v2.6-flash`
- 两个条件：memory off / memory on
- `max_steps=40`，`workers=2`
- 完全相同的动态 Repo-map、上下文窗口、输出上限、整题 Token、temperature 和工具集
- 配对任务使用相同 base commit 与 Docker image
- 两条件各自保留逐题 checkpoint、`progress.json`、`progress.md`、patch 和检索指标
- 启动前检查现有评测进程；断点只复用同一条件已完成的项
- 结果、数据库、日志、本地配置与 API Key 全部位于 Git 忽略目录

### 10.3 调用量与费用预估方式

6 题 × 2 条件 = `12` 次 Agent run。40 步循环保护下，逻辑模型请求上限为 `480` 次，正常 finish/gave_up 会使实际请求更少。现有动态检索 10 题运行消耗 `7,494,946` 个累计 Token，均值约 `749,495 Token/题`；按相同量级，12 次配对运行的方向性估计约为 `9.0M Token`，但 memory on/off 可能改变步数和上下文，不能把它当作计费承诺。

真正启动前必须重新读取 MiMo 当时的官方输入/输出单价，并从运行记录可获得的输入/输出 Token 拆分计算：

```text
预计费用 = 预计输入 Token × 输入单价 + 预计输出 Token × 输出单价
```

届时应向用户报告：12 次 Agent run、最多 480 次逻辑模型请求、预计输入/输出 Token 区间和人民币费用区间；得到明确确认后才能调用模型。本阶段没有调用模型，也没有产生 API 费用。

### 10.4 逐题和汇总指标

- 官方 SWE-bench Docker `resolved`
- 正确修改文件首次出现步骤
- 无关文件读取数
- 重复搜索数
- memory_search / memory_get 次数、返回 id、实际引用 id
- 总 Token、每题 Token、耗时、finish/max_steps/gave_up
- 定向测试状态和最终 patch
- **负迁移**：off resolved 但 on 未 resolved；或 on 在同样结果下显著增加 Token、无关读取或步骤

统计以同题配对差值为主，而不只比较两个平均值。6 题只属于小规模方向性回归，不能作为总体能力排名；若观察到正向信号，再预注册更大的样本验证。

## 11. 当前限制与后续改进

1. 冻结集是小规模合成数据，覆盖边界条件但不等于真实历史分布。
2. 当前方案仍以词法和结构化字段为主，没有通用 embedding；跨语言深层语义仍可能漏召回。
3. 平均延迟是在 19 条记忆上的结果；需要另建 1 万/10 万条记录的规模基准衡量 SQLite 查询与重排成本。
4. q11 的“语义改写”由路径/符号桥接成功，不能过度宣称自然语言语义理解能力。
5. 本评测没有让 Agent 决策，无法测量记忆是否改变文件选择或补丁质量。
6. 第二阶段应优先完善输入/输出 Token 分账和每次 memory 注入 Token 观测，才能得到可信成本与负迁移分析。

因此，当前可以严谨地说：Forge 的记忆系统已经具备可恢复存储，并在冻结离线召回集上正确完成仓库隔离、结构化多信号排序、stale、失效和去重；是否真正帮助 Agent 和提升真实任务成功率，仍待用户确认后的低成本配对 A/B。
