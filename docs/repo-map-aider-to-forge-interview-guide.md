# 从 Aider Repo-map 到 Forge 动态检索：实现原理与面试讲解指南

> 面向对象：希望真正理解 Forge Repo-map，并能在面试中从算法、工程架构、上下文预算、Agent 决策和实验边界五个角度讲清楚的人。
> 当前代码基线：Forge `dev` 分支，动态检索版本及检索触发增强。
> 术语说明：本文统一使用 **Repo-map**。`rope-map`、`ropemap` 和 `personal_ropemap` 都按上下文理解为 Repo-map / Personalized Repo-map。

## 1. 先记住这一句话

Forge 的 Repo-map 经历了三代：

1. **Legacy 静态摘要**：扫描代码、提取符号、按简单启发式选一些内容塞进提示词。
2. **Personalized PageRank 静态地图**：建立文件依赖图，用任务文本和已知文件做个性化种子，把词法相关性与 PageRank 融合，再把预算内的高价值符号放进提示词。
3. **当前 Retrieval 架构**：初始提示词只放极简仓库概览，完整信息进入持久化索引；Agent 通过结构化工具按需查询，每次查询再使用任务相关性和 Personalized PageRank 动态重排。

最重要的设计变化不是“有没有 PageRank”，而是：

> PageRank 从一次性生成静态 Repo-map 的最终排序器，变成了多轮检索中的一个排序信号。

这解决的是大型仓库里“静态地图即使排序正确，也会因为 token 预算而永久丢掉后半部分信息”的问题。

---

## 2. Repo-map 到底解决什么问题

Coding Agent 面对仓库时有三个冲突目标：

- 它需要知道仓库里有哪些模块、类、函数和依赖关系。
- 它不能把整个仓库放进模型上下文。
- 它不能只靠文件名猜，也不能无止境地 `grep → cat → grep`。

Repo-map 本质上是一个 **受 token 预算约束的代码检索与结构压缩问题**：

```text
完整仓库
  ├─ 文件、符号、import、引用、目录结构
  ├─ 当前 Issue、堆栈、已确认文件
  └─ 模型当前对话中发现的新线索
          ↓
候选生成 + 图关系扩展 + 任务相关排序
          ↓
预算内的结构化上下文
```

一个好的 Repo-map 不只是“目录树”，也不只是“函数签名列表”。它至少要回答：

1. 当前问题最可能涉及哪些文件？
2. 这些文件定义了哪些关键符号？
3. 它们依赖谁、又被谁依赖？
4. 哪些符号在全仓库结构中重要？
5. 当前上下文预算下，应该先展示什么？

---

## 3. Aider 的 Repo-map：官方实现拆解

本节依据 Aider 官方的 [Repo-map 文档](https://aider.chat/docs/repomap.html) 和 [`aider/repomap.py`](https://github.com/Aider-AI/aider/blob/main/aider/repomap.py)。Aider `main` 会持续变化，面试时应描述算法思想和当前源码结构，不要把行号说成永久 API。

### 3.1 Aider 的输入和输出

Aider 的核心输入包括：

- `chat_files`：已经进入聊天上下文的文件。
- `other_files`：仓库里其他候选文件。
- `mentioned_fnames`：用户或对话明确提到的文件。
- `mentioned_idents`：对话明确提到的标识符。
- `max_map_tokens`：Repo-map token 预算。

输出不是完整代码，而是类似下面的结构：

```text
src/service.py:
  class UserService
    def create_user(...)
    def delete_user(...)

src/store.py:
  class UserStore
    def save(...)
```

它会保留文件路径、重要定义以及定义附近的结构化代码行，让模型能够判断“下一步应该把哪个文件加入聊天”。

### 3.2 第一步：用 Tree-sitter 提取定义和引用标签

Aider 通过 Tree-sitter 和各语言的 `tags.scm` 查询提取两类标签：

- `name.definition.*` → `def`
- `name.reference.*` → `ref`

每个 `Tag` 至少包含：

```text
(relative_file, absolute_file, line, identifier_name, kind)
```

如果某种语言只能可靠提取定义、不能提取引用，Aider 会用 Pygments 的 `Token.Name` 做保守引用回填。这个回退提高了多语言覆盖率，但精度低于 AST/Tree-sitter 的语义关系。

面试表达：

> Aider 不是把 AST 全量序列化给模型，而是先把多语言源码压缩成 def/ref 标签。图计算和最终渲染都围绕这些标签进行，因此索引粒度比原始文本小很多。

### 3.3 第二步：从 def/ref 构建文件图

Aider 先建立三组结构：

```text
defines[identifier] -> 定义该 identifier 的文件集合
references[identifier] -> 引用该 identifier 的文件列表
definitions[(file, identifier)] -> 具体定义 Tag
```

对同时存在定义和引用的 identifier，建立：

```text
引用文件  ──identifier──>  定义文件
```

图节点是文件，边表达“源文件引用了目标文件定义的符号”。使用 `MultiDiGraph`，因为同一对文件之间可以通过多个 identifier 形成多条边。

### 3.4 边权重不是简单引用次数

Aider 对边权做了多种启发式修正：

- 用户/对话提到的 identifier：提高权重。
- 较长的 snake_case、kebab-case、camelCase 名称：提高权重，因为更可能是具有区分度的业务符号。
- 以下划线开头的 identifier：降低权重。
- 在很多文件中都定义的通用名称：降低权重。
- 来自 `chat_files` 的引用：显著提高权重，让当前对话关注点影响图传播。
- 多次引用使用 `sqrt(reference_count)`，避免高频通用符号线性压倒其他信号。

可以把它概括成：

```text
edge_weight
= identifier_importance
 × chat_context_boost
 × sqrt(reference_count)
```

这里的关键不是具体倍数，而是 Aider 已经把“图中心性”和“当前对话相关性”混到边与 personalization 中。

### 3.5 第三步：Personalized PageRank

普通 PageRank 的直觉是：

> 一个文件被很多重要文件依赖，它就更重要。

其迭代形式可以写成：

```text
r(v) = (1 - α) · p(v)
     + α · Σ[u→v] r(u) · w(u,v) / Σ[k] w(u,k)
```

- `r(v)`：文件 `v` 的排名。
- `α`：阻尼系数，常见值为 0.85。
- `p(v)`：随机跳转/个性化分布。
- `w(u,v)`：从引用文件 `u` 指向定义文件 `v` 的边权。

Aider 会用聊天文件、明确提到的文件和路径组件命中构造 personalization。没有个性化种子时，退化为全局 PageRank。

PageRank 的价值是关系扩展。例如 Issue 只提到 controller，但真正需要修改的是 controller 依赖的 service；图传播能把 service 推到候选前部。

### 3.6 第四步：把文件 rank 再分配到具体符号

Aider 并没有停在“文件排名”。它把每个源文件的 PageRank 按出边权重分配到 `(目标文件, identifier)`：

```text
symbol_rank(target, ident)
  += rank(source) × edge_weight(source,target,ident)
     / source_total_outgoing_weight
```

然后按 `symbol_rank` 排序具体定义标签。

这一步非常重要：最终 token 预算消耗在符号和定义行上，所以排序粒度必须尽量接近最终展示粒度。只给文件排序、再把整个文件的所有符号都塞进去，会被大文件浪费预算。

### 3.7 第五步：在 token 预算内渲染

Aider 将高排名标签转换为带上下文的代码树，然后通过二分搜索寻找合适的标签数量，使最终地图尽量接近 `max_map_tokens`。

大致流程：

```text
ranked_tags
  ↓ 取前 middle 个
TreeContext 渲染定义附近结构
  ↓ 估算 token
太大 → 减少 middle
太小 → 增加 middle
```

当前实现允许约 15% 的预算误差。渲染时还会截断超长行，避免压缩/生成文件中的单行内容破坏整个地图。

### 3.8 缓存和动态预算

Aider 的工程实现还包括：

- 持久化标签缓存，避免每轮重新解析全仓库。
- Tree 渲染缓存和 Repo-map 结果缓存。
- `refresh=auto/files/always/manual` 等刷新策略。
- 当聊天里还没有文件时，地图预算可以按倍数放大，让模型先获得更广的仓库视野。
- 当已有文件进入聊天时，Repo-map 重点补充“其他文件”，避免重复发送已经完整展示的文件。

### 3.9 Aider 方案的优点和边界

优点：

- 多语言 Tree-sitter 覆盖。
- def/ref 图与 PageRank 形成结构化全局视野。
- 能利用聊天文件和用户提及做个性化。
- 排序深入到符号级。
- 最终输出严格围绕 token 预算优化。

边界：

- 它仍然主要生成一个放进上下文的静态地图；预算外信息在这一轮对模型不可见。
- 静态分析很难准确处理反射、动态 import、依赖注入、运行时注册等关系。
- 图中心性不等于 Issue 相关性；基础设施文件容易全局重要，但不一定是当前 bug 的修改点。
- 大仓库首次扫描、标签解析和图构建仍有成本。

---

## 4. Forge 第一代：Legacy Repo-map

Forge 最早的 Repo-map 主要在 `context/repo_map.py` 中完成扫描、符号提取和预算裁剪。

它的基本流程是：

```text
扫描仓库文件
  → 提取类/函数等符号
  → 用简单启发式排列文件
  → 逐文件加入摘要
  → 达到 token 预算后停止
```

这个版本适合验证“给 Agent 一张仓库地图是否有帮助”，但有三个明显问题：

1. **排序信号弱**：主要看路径、符号和简单统计，没有真正利用文件依赖关系。
2. **截断具有永久性**：一次 prompt 中没进入地图的文件，模型可能永远不知道它存在。
3. **大文件挤占预算**：如果按文件顺序加入，早期的大候选可能压缩后续相关文件空间。

当前代码仍保留 `legacy` 策略，用于回归和消融，而不是作为默认方案。

面试表达：

> Legacy 版本证明了 Repo-map 的产品形态，但没有解决“哪些结构最值得进入有限上下文”的核心排序问题，所以第二代才引入依赖图和 Personalized PageRank。

---

## 5. Forge 第二代：Personalized Repo-map

第二代由以下文件组成：

- `context/repo_index.py`：增量文件与符号索引、import/引用图。
- `context/graph_rank.py`：确定性加权 Personalized PageRank。
- `context/map_builder.py`：任务相关排序、符号选择和预算渲染。
- `context/repo_map.py`：兼容旧入口和旧策略。

### 5.1 RepoIndex 的数据结构

每个 `FileRecord` 保存：

- 仓库相对路径、大小、语言。
- 内容 SHA-256。
- `Definition` 列表：名称、qualified name、类型、行号、签名。
- imports。
- 保守 references 和 attributes。
- 解析状态、摘要和词项集合。

Python 使用标准库 AST：

- 递归提取 class、function、async function、method。
- 保留 qualified name，例如 `ClassName.method_name`。
- 解析 `import` 和 `from ... import ...`。
- 用唯一符号定义做保守跨文件引用连接。

非 Python 文件至少通过已有符号提取器获得基础定义，并支持路径、文本和符号检索。

### 5.2 Forge 的文件依赖图

Forge 图的方向与 Aider 类似：

```text
引用/导入方  →  定义/被依赖方
```

边权来源：

- import 命中目标模块：基础权重较高。
- import 别名在文件中被多次使用：通过平方根增加权重。
- 唯一的非局部符号引用：给予较弱权重，避免仅凭同名造成强错误边。

这样的选择偏保守：宁可漏掉部分动态关系，也不让大量同名函数制造错误图结构。

### 5.3 Forge 自己实现 PageRank

`context/graph_rank.py` 没有依赖 NetworkX，而是实现了确定性的加权迭代：

- 节点排序固定，保证同输入同输出。
- 默认 `alpha=0.85`。
- 支持 personalization。
- 处理 dangling 节点。
- 用 L1 error 和 tolerance 判断收敛。
- 迭代达到上限时返回确定性的近似结果。

这样做的理由：

- 运行依赖更轻。
- 算法行为可控，便于评测复现。
- Forge 只需要 PageRank 的一个明确子集，不需要完整图计算框架。

### 5.4 Personalized 的任务种子

第二代会从以下信息生成 personalization/排序信号：

- Issue 文本与文件路径、符号签名的词项重合。
- Issue 中明确出现的完整文件路径。
- Agent 最近搜索或读取过的 `mentioned_paths`。
- 少量平滑值，避免图完全困在单个初始候选周围。

明确路径拥有保底优先级，因为图传播不能否决来自堆栈或 Issue 的直接证据。

### 5.5 词法相关性与 PageRank 融合

Forge 先计算：

```text
lexical(file)
  = Σ query_term 的 IDF 权重
    × 路径命中加权
    × 符号签名命中
```

再分别得到词法名次和图名次，`personalized` 策略使用类似倒数排名融合：

```text
score(file)
  = I(lexical > 0) / (60 + lexical_rank)
  + 1 / (60 + graph_rank_position)
```

采用 rank fusion 而不是直接相加原始分数，可以减少不同量纲和仓库规模对权重的影响。

### 5.6 预算渲染

第二代按文件排名选前部候选，再按符号与任务词重合、引用频次和行号排序符号。

预算策略有两个细节：

- 某个候选块太大时，先减少该文件符号；仍然放不下就跳过它，而不是直接停止整个地图。
- 先保证多个相关文件进入，再用剩余预算为已选文件补充更多符号。

这修复了“一个巨大文件导致后续所有文件永久丢失”的问题。

### 5.7 第二代仍然存在的结构性问题

即使排序明显比 Legacy 好，第二代仍把最终结果作为静态文本放进 system prompt：

- 地图每轮持续占用上下文。
- 超过 4,000 token 预算的仓库信息仍然不可查询。
- Agent 新发现异常、符号或依赖后，虽然可以重排，但历史中已有地图和代码会持续累积。
- PageRank 被迫承担“最终决定哪些信息可见”的责任。

所以第三代不是继续微调排序公式，而是改变信息访问方式。

---

## 6. Forge 当前版本：极简概览 + 持久化索引 + 动态检索

当前默认策略名为：

```yaml
context:
  repo_map_strategy: retrieval
```

整体架构：

```text
                      ┌────────────────────────────┐
                      │ 持久化 RepoIndex           │
                      │ path / symbol / import     │
Git-aware 文件枚举 ──→│ reference / hash / terms  │
                      └──────────────┬─────────────┘
                                     │
Issue ──→ 极简 overview ──→ Agent ──┼─ repo_search
                                     ├─ repo_symbols
新符号/异常/已读文件 ────────────────┼─ repo_related_files
                                     ├─ repo_tree
                                     └─ repo_map(paths)
                                              │
                            词法 + 路径 + 符号 + PPR 动态重排
                                              │
                                      少量候选 → file_view
```

### 6.1 初始 prompt 不再放完整地图

`RepoMap.overview()` 只生成：

- 顶层目录。
- 少量入口文件。
- 与 Issue 词项相关的少量候选。
- 可用结构化检索工具说明。

它使用独立的 `overview_budget`，默认 1,000 token。完整索引永远不会直接序列化到 prompt。

这意味着排名靠后的信息没有被删除，只是暂时没有进入上下文；Agent 可以之后查询。

### 6.2 持久化与增量更新

当前第一版选择原子 JSON 快照，而不是 SQLite：

- 快照位于系统临时目录的 `forge-repo-index`，不进入目标仓库 Git。
- schema 版本当前为 `INDEX_SCHEMA = 2`。
- 每个文件使用内容 hash 区分版本。
- 未变化文件复用解析记录，变化文件重新解析。
- 写快照时先写唯一临时文件，再用 `os.replace` 原子替换。
- 快照损坏、schema 不匹配或根路径不匹配时安全重建。

为什么第一版不用 SQLite：

- 当前查询是单进程内存查询，JSON 快照足够简单。
- SQLite 会增加 schema migration、锁、并发和恢复逻辑。
- 当索引达到更大规模、需要倒排表或跨进程共享时，再迁移到 SQLite 更合理。

### 6.3 五个结构化工具

#### `repo_search(query, limit, path_prefix)`

用途：按路径、符号和源码词项找候选，是首选定位工具。

排序信号：

```text
path_score   = query 与路径词项重合
             + 少量 task 与路径重合

symbol_score = 命中的 qualified symbol 数量

lexical      = path_score × weight.path
             + symbol_score × weight.symbol
```

如果路径和符号都没命中，再使用源码 term 命中乘 `weight.text`。

候选作为 Personalized PageRank 种子，已确认路径额外加种子权重。最终分数：

```text
total(file)
  = lexical(file)
  + weight.pagerank × ppr(file) × indexed_file_count
```

乘文件数是为了把概率量级转换到更容易与 lexical 融合的范围。输出同时记录各分项，便于复现实验。

#### `repo_symbols(query, kind, limit)`

用途：精确查找类、函数、方法等定义。与 `repo_search` 共用排序管线，但要求符号匹配，可按 `kind` 过滤。

#### `repo_related_files(paths, direction, limit)`

用途：从已确认文件沿 import/引用图扩展。

- `outgoing`：当前文件依赖谁。
- `incoming`：谁依赖当前文件。
- `both`：双向展开。

#### `repo_tree(path, depth)`

用途：查看局部目录结构，不把全仓库目录树塞进 prompt。`depth` 有硬上限。

#### `repo_map(paths, token_budget)`

用途：只为已经筛选出的少量文件生成局部符号地图，而不是读取一个巨大 `repo-map.txt`。

### 6.4 查询安全与预算

所有 Repo 查询工具共同保证：

- 路径经 `resolve()` 后必须位于 workspace 根目录内。
- 使用 Git 文件列表和 ignore 语义。
- symlink 解析后不能逃离 workspace。
- `limit`、路径数量、参数长度和 token 都有上限。
- 输出包含 `total_candidates` 和 `truncated`。
- 同一操作和参数重复时返回 `duplicate=true`，提示改变范围或关键词。
- 查询、候选、截断、token 和步骤进入运行记录。

### 6.5 PageRank 在第三代中的新角色

第二代：

```text
PageRank + 词法排序 → 生成最终静态 Repo-map → 放入 prompt
```

第三代：

```text
每次 query 的局部候选
  + 当前任务文本
  + Agent 已确认路径
  + 最新发现的符号/异常
       ↓
重新构造 personalization
       ↓
PageRank 成为动态重排信号之一
```

因此 PageRank 没有被删除，而是从“可见性的最终裁判”降级为“关系相关性的证据”。路径、符号和直接词法证据仍然可以超过全局中心性。

---

## 7. 本次新增：检索工具触发策略

10 题方向性评测暴露出一个关键问题：虽然结构化工具已经注册，模型仍然可能习惯性调用 `shell`、`file_read`，导致 10 题的 `repo_*` 查询次数为 0。

这说明“工具存在”不等于“模型会使用工具”。因此当前版本加入三层触发策略。

### 7.1 第一层：System Prompt 决策协议

提示词明确要求：

1. Retrieval 模式的首次定位动作必须是 `repo_search` 或 `repo_symbols`。
2. 确认候选后使用 `repo_related_files` 扩展。
3. 使用 `repo_map(paths)` 获取局部结构，再用 `file_view` 精读。
4. 多次传统读取后，使用新符号或错误信息重新查询。
5. 索引失败时必须明确说明降级。

工具 schema 的 description 也把 `repo_search/repo_symbols` 标成 PRIMARY localization tool，减少模型只看工具描述时的歧义。

### 7.2 第二层：运行时硬门禁

仅靠 prompt 仍可能被模型忽略，所以 `RunContext.retrieval_gate()` 会拦截：

- `file_read` / `file_view`
- `search_text` / `find_files` / `find_symbol`
- 用于发现文件的 `rg` / `grep` / `find` / `ls` / `cat` / `sed` 等 shell 命令

如果还没有成功执行首次 `repo_search` 或 `repo_symbols`，这些动作不会真正执行，而是返回：

```text
[STRUCTURED RETRIEVAL REQUIRED]
```

这里的“成功”不只是工具没有报错：查询必须返回至少一个真实候选。空查询结果和重复查询都不会解除门禁，防止模型用一个无意义调用形式化地绕过策略。

这不是把主循环重写成固定工作流。模型仍然自主选择 query、limit、path prefix、候选文件和后续动作；框架只约束“先使用低成本结构化定位，再进行高成本原文探索”。

### 7.3 第三层：周期性再检索

配置项：

```yaml
context:
  retrieval_refresh_steps: 3
```

一次成功结构化定位后，允许精读文件。当连续完成 3 次传统搜索/读取后，门禁重新开启，要求 Agent 用刚发现的类名、函数名、异常或依赖进行新一轮 `repo_search/repo_symbols`。

编辑成功后会清空这一轮定位要求，避免在已经进入修改阶段时机械阻塞。

### 7.4 显式降级

如果索引返回 `Repository index unavailable`：

- 记录 `retrieval_degraded_reason`。
- 解除检索门禁。
- 允许传统工具继续工作。
- 最终运行指标能够区分“模型没用检索”和“索引失败后安全降级”。

不会静默退回旧模式。

### 7.5 新增可观测字段

最终 `RunResult.retrieval` 增加：

- `query_attempts`
- `localization_successes`
- `policy_redirects`
- `policy_refreshes`
- `degraded_reason`

这些字段可以回答：

> 模型为什么使用了/没有使用 Repo-map？是模型决策、框架门禁、索引失败，还是预算截断？

---

## 8. 三代方案对比

| 维度 | Legacy | Personalized 静态版 | 当前 Retrieval 版 |
|---|---|---|---|
| 初始上下文 | 静态摘要 | 任务相关静态地图 | 极简概览 |
| 完整信息 | 扫描后直接裁剪 | 索引存在，但最终生成静态地图 | 持久化可查询索引 |
| 排序 | 简单启发式 | 词法 + Personalized PageRank | 每次查询的词法/路径/符号 + PPR |
| PageRank 角色 | 无或很弱 | 最终静态选择器 | 动态检索信号 |
| 多轮演化 | 弱 | 可按近期路径重建，但仍是静态文本 | 原生支持查询—读取—再查询 |
| 预算外信息 | 本轮永久不可见 | 本轮永久不可见 | 可在后续精确查询 |
| 上下文占用 | 持续 | 持续且可能较大 | 初始概览小，工具结果按需进入 |
| 重复查询 | 无结构化控制 | 无结构化控制 | 参数级去重与记录 |
| 降级 | 通常隐式 | 构建失败影响地图 | 显式记录索引失败后放行 |
| 消融 | `legacy` | `personalized/global/lexical` | `retrieval` |

---

## 9. Token 预算如何分层

当前架构将以前混在一个 `repo_map_budget` 中的职责拆开：

- `overview_budget`：初始极简概览。
- `retrieval_result_budget`：单次查询结果。
- `local_map_budget`：指定候选的局部 Repo-map。
- 文件工具自己的读取行数/输出预算。
- `context_window`：单次模型上下文窗口。
- `max_tokens`：单次模型输出。
- `run_token_limit`：整题运行累计 token。

设计原则：

```text
索引很大 ≠ prompt 很大
查询候选很多 ≠ 全部进入历史
历史很长 ≠ 可以挤掉模型输出和完成门禁空间
```

为什么这比单纯加大 `repo_map_budget` 好：

- 大地图每一轮都重复占用输入 token。
- 静态地图内容会随着任务进展变得陈旧。
- 预算越大不代表相关性越高，可能只是加入更多噪声。
- 工具调用能把上下文成本推迟到确实需要时。

---

## 10. 缓存、指纹和一致性

当前索引版本和评测身份包含：

- 索引 schema/version。
- 文件内容 hash。
- 代码指纹。
- Repo-map 策略。
- 概览/检索/局部地图预算。
- 检索融合权重。

这样做是为了避免一种常见评测错误：代码或配置已经变化，却复用旧结果，最后把不同实验误认为同一个实验断点。

当前缓存仍有一个已知限制：缓存路径包含仓库真实根路径。SWE-bench 每题使用不同临时 checkout 时，同一仓库内容也可能无法复用快照。后续应将缓存身份升级为：

```text
repository identity + commit/content fingerprint + index schema
```

同时保持 workspace 和 symlink 安全边界。

---

## 11. 真实评测应该如何解读

动态检索版本在冻结 10 题的小规模实验中：

- 非空补丁：7/10 → 8/10。
- 官方 resolved：4/10 → 5/10。
- 总 token 下降约 1.4%。
- 总耗时受 Pylint 长尾影响上升约 34.9%。
- 但结构化 `repo_*` 查询总数为 0。

所以正确结论是：

> 极简概览和新索引架构在这个小样本上没有造成总体能力退化，但该实验没有真正执行动态检索，不能把 +1 resolved 归因于 Personalized PageRank。

错误说法是：

> “我加了动态 PageRank，所以 SWE-bench 提升了 10 个百分点。”

本次触发策略正是针对这个实验缺口。根据用户要求，本次增强后没有重新运行测试或付费模型评测，因此不能宣称门禁已经带来新的 resolved 提升。

---

## 12. 面试时的两分钟讲法

可以直接按下面的结构讲：

> 我在 Forge 里做了三代 Repo-map。第一代是简单的静态符号摘要，能给模型仓库结构，但大仓库下排序和截断都比较粗糙。第二代参考 Aider 的思路，先构建文件依赖图，再根据 Issue 文本、明确路径和 Agent 已读取文件生成 personalization，运行加权 PageRank，并把词法名次和图名次融合，在固定 token 预算里优先输出相关文件和符号。
>
> 但第二代还有一个结构性问题：不管排序多好，最终还是一段静态文本。预算外的信息会永久丢失，而且地图每轮都占上下文。所以第三代改成了极简仓库概览加持久化索引。完整的路径、符号、import、引用、内容 hash 和图都保存在索引里，Agent 可以调用 repo_search、repo_symbols、repo_related_files、repo_tree 和局部 repo_map 多轮检索。PageRank 没删，而是从最终排序器变成每次查询里的动态重排信号。
>
> 实验时我发现模型虽然拿到了工具，却仍然习惯用 shell 和 file_read，10 题里结构化查询次数为 0。所以我又加了三层触发：system prompt 决策协议、首次定位前的运行时硬门禁，以及连续三次传统读取后的再检索门禁。索引失败会显式记录并降级，不会把 Agent 卡死。这样我不仅优化排序，还把“模型是否真的使用检索”变成可观测、可约束的系统行为。

---

## 13. 面试官追问与回答

### Q1：为什么不用向量数据库？

答：

> 当前主要目标是代码结构定位。路径、qualified symbol、import 和引用图对代码问题有很强的精确信号，而且可以离线、确定性、低依赖地构建。向量检索适合语义近似，但可能把语义相似却依赖无关的文件排前。我会把 embedding 作为未来的候选召回信号，而不是替代符号和图索引。

### Q2：PageRank 为什么适合代码仓库？

答：

> 代码文件之间存在依赖网络。一个模块被很多重要模块使用，通常是架构中的关键节点。PageRank 能把这种递归重要性传播出来。Personalized PageRank 进一步把随机跳转分布偏向当前 Issue、堆栈和已确认文件，使图排名从全局中心性变成任务相关中心性。

### Q3：为什么 PageRank 不能单独决定结果？

答：

> 全局中心节点常常是通用工具或基础设施，不一定是当前 bug 的修改点；动态语言中的图也不完整。因此 Forge 融合路径、词法、符号命中和 PPR，并对 Issue 明确路径保底。图是关系证据，不是否决直接证据的裁判。

### Q4：Aider 和 Forge 最大区别是什么？

答：

> Aider 的经典实现把全仓库 def/ref 图排序后渲染成预算受控的 Repo-map，并随聊天状态动态调整。Forge 第二代与这个方向接近，但当前版本进一步把完整信息留在可查询索引中，初始只给极简概览，Agent 多轮调用结构化工具。也就是说 Aider 重点优化“这张地图展示什么”，Forge 当前重点优化“什么时候查询哪部分地图”。

### Q5：运行时硬门禁会不会限制 Agent 自主性？

答：

> 门禁只约束首次定位和长时间传统探索，不决定查询词、候选、编辑或测试。它类似数据库系统要求先走索引再全表扫描。索引明确失败时会记录原因并放行，所以不会为了形式上的检索把 Agent 卡死。

### Q6：如何验证 Repo-map 真正有效？

答：

> 分两层。离线测 Recall@K、首个相关文件名次、查询延迟、缓存命中、预算截断；端到端使用同一冻结任务、同一模型、同一 max steps，对比 legacy、personalized 和 retrieval 的官方 resolved、token、耗时、无关读取和首次发现最终修改文件的步骤。必须确认结构化查询实际发生，否则不能归因于检索算法。

### Q7：为什么当前结果不能证明动态检索有效？

答：

> 因为那 10 题里 repo 查询次数是 0，模型绕过了工具。结果只能说明极简概览没有明显伤害，不能证明动态 PPR。发现这个问题后，我没有包装数据，而是增加触发门禁和可观测字段。这体现的是评测可信度，而不是只追求好看的指标。

### Q8：当前最大性能问题是什么？

答：

> 一是不同临时 checkout 之间的持久化缓存复用不足，10 题索引 cache hit 为 0；二是 Python 图关系较强，其他语言的 import/reference 仍比较基础；三是动态调用关系只能近似。下一步会优先做基于 commit/content fingerprint 的跨 checkout 缓存，再增强多语言依赖解析。

---

## 14. 阅读源码的推荐顺序

如果要在面试前重新复习，按以下顺序阅读：

1. `context/repo_index.py`：索引记录、文件枚举、AST、依赖图、快照。
2. `context/graph_rank.py`：PageRank 迭代公式。
3. `context/map_builder.py`：第二代 personalized/global/lexical 的静态选择。
4. `tools/repo_query_tool.py`：当前动态查询、PPR 重排、预算和去重。
5. `context/repo_map.py`：Legacy 和兼容入口。
6. `agent/prompt.py`：模型看到的检索协议。
7. `agent/run_context.py`：触发门禁、再检索、降级和可观测状态。
8. `agent/core.py`：工具执行链路、索引注入和最终指标汇总。
9. `config/default.yaml` / `config/schema.py`：可复现实验参数。

---

## 15. 最后总结

Forge Repo-map 的核心演进可以概括为：

```text
“把什么塞进 prompt”
        ↓
“如何用任务相关 PageRank 选得更准”
        ↓
“如何让 Agent 在需要时查询，并动态重排”
        ↓
“如何保证模型真的使用检索，并记录是否生效”
```

真正有面试价值的不是“我用了 PageRank”这一句话，而是你能讲清楚：

- 图的节点、边和权重是什么。
- personalization 从哪里来。
- PageRank 如何与直接任务证据融合。
- 符号如何在 token 预算内渲染。
- 为什么静态地图在大仓库中仍然失败。
- 动态检索如何改变上下文生命周期。
- 工具存在但模型不用时，框架如何发现、约束和降级。
- 实验中哪些结论可以说，哪些不能说。

如果你能把这些讲清楚，你讲的就不再是一个“借鉴 Aider 的 Repo-map 小功能”，而是一套完整的 **代码索引、图排序、上下文工程和 Agent 策略协同设计**。

---

## 16. 本次触发增强的验证边界

本次按用户要求没有运行单元测试、全量测试、Docker 测试或付费模型评测。提交前只进行了五轴静态代码审查、配置传播核对、敏感信息检查和 `git diff --check`。

因此当前可以确认的是实现意图、配置链路和静态控制流一致；不能把它描述为“已经通过回归测试”或“已经提升 SWE-bench resolved”。后续若重新评测，最先观察的不是 resolved，而是：

- `query_attempts > 0`
- `localization_successes > 0`
- `policy_redirects` 是否处于合理范围
- `degraded_reason` 是否为空
- 首次发现最终修改文件的步骤是否提前

只有先证明模型真实使用了检索，才有资格讨论动态 PageRank 对最终能力的贡献。
