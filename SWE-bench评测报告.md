# SWE-bench Lite 评测报告 — Forge Agent + MiMo-V2.5-Pro

> 评测日期：2026/06/17
> 模型：MiMo-V2.5-Pro（Token Plan 免费 API）
> Agent：Forge Agent（项目自研）
> 数据集：SWE-bench Lite（300 个真实 GitHub Issue）

---

## 目录

1. [评测完整流程](#1-评测完整流程)
2. [遇到的问题及解决方案](#2-遇到的问题及解决方案)
3. [评测结果分析](#3-评测结果分析)
4. [后续优化方向](#4-后续优化方向)
5. [总结](#5-总结)

---

## 1. 评测完整流程

### 1.1 阶段零：准备工作

```
① 下载 SWE-bench Lite 数据集（swebench_lite.json，300 条）
② 安装依赖：pyyaml、swebench
③ 配置 MiMo API：config/swebench-mimo.yaml
④ 创建评测脚本 run_swebench_eval.py
```

### 1.2 阶段一：Agent 生成补丁

```
流程：读取 swebench_lite.json
  → 对每个 Issue：
    ① 克隆目标仓库 → 重置到 base_commit
    ② 创建新分支
    ③ 运行 forge-agent（传入 Issue 描述）
    ④ git diff 导出 .patch 补丁文件
    ⑤ 保存结果到 results.json（断点续跑）

配置：
  - 模型：mimo-v2.5-pro（provider: openai）
  - max_steps：25
  - agent 超时：600 秒
  - 目标：200 条 Issue
```

**耗时：约 1 小时/10 条（200 条约 10+ 小时）**

### 1.3 阶段二：Docker 验证补丁

```
流程：
  ① convert_predictions.py → 把 results.json 转为 SWE-bench 预测格式
  ② swebench_win_fix.py → Windows resource 模块补丁
  ③ python -m swebench.harness.run_evaluation → 逐条验证
     - 拉取对应仓库的 Docker 镜像
     - 在 Docker 容器中应用补丁
     - 运行 SWE-bench 官方测试用例
     - 标记 resolved / unresolved

配置：
  - max_workers：2（同时跑 2 个容器）
  - timeout：1800 秒
  - 验证 24 条（全 48 条的一半）
```

**耗时：24 条约 1.5 小时**

---

## 2. 遇到的问题及解决方案

### 2.1 MiMo Token Plan API 认证

**问题：** MiMo Token Plan 使用自定义 `api-key` HTTP 头认证，而非标准的 `Authorization: Bearer`。OpenAI/Anthropic SDK 默认只发 Bearer Token。

**解决：**
- 在 `LLMConfig` 中添加 `headers: dict` 字段
- `OpenAICompatBackend` 和 `AnthropicBackend` 支持 `default_headers` 参数
- `router.py` 透传 headers 到 backend
- `config/swebench-mimo.yaml` 配置：
  ```yaml
  headers:
    api-key: ${MIMO_API_KEY}
  ```

### 2.2 pyyaml 缺失（ModuleNotFoundError）

**问题：** 子进程使用 base conda 的 Python（D:\miniconda），该环境未安装 pyyaml。

**解决：**
- `config/schema.py` 增加 fallback：先尝试 `import yaml`，失败则用 `ruamel.yaml`（已安装）
- 避免在 base conda 额外安装包

### 2.3 Windows 终端编码（GBK vs UTF-8）

**问题：** 中文 Windows 终端编码为 GBK，emoji 和非 ASCII 字符导致 `UnicodeEncodeError`。

**解决：**

- `run_swebench_eval.py` 顶部添加：
  
  ```python
  sys.stdout.reconfigure(encoding="utf-8", errors="replace")
  sys.stderr.reconfigure(encoding="utf-8", errors="replace")
  ```
- 所有 emoji（✅❌🤖📂等）替换为 ASCII 文本（`[PASS]`、`[FAIL]`、`[..]`）

> - > # 2.4 *`临时文件中文路径乱码`*
>   >

**问题：** `tempfile.mktemp()` 在 `C:\Users\郑文艺最叼\AppData\Local\Temp\` 创建文件，路径中的中文通过 cmd.exe 传给子进程时乱码，导致 `FileNotFoundError`。

**解决：**

- 改用 eval 目录（纯英文路径 `D:\fianll-test\forge\swebench_eval\`）存放临时任务文件
- 文件命名：`task_{issue_id}.txt`

### 2.5 subprocess 编码继承

**问题：** `subprocess.run(cmd, shell=True)` 启动的子进程在 Windows 上继承系统 GBK 编码，`click.echo()` 输出 emoji 时崩溃。

**解决：**

- `run_cmd()` 函数传入 `env={**os.environ, "PYTHONIOENCODING": "utf-8"}`

### 2.6 CLRF 换行符导致补丁打不上

**问题：** 这是**最关键**的问题。Windows 上 `git diff` 产生 CRLF（`\r\n`）换行的补丁，但 SWE-bench 的 Docker 容器（Linux）中 `patch` 命令只认 LF（`\n`），导致 43/48 个补丁全部应用失败（"Patch Apply Failed"）。

**修复前：**

```
patch: **** malformed patch at line 639:
patch unexpectedly ends in middle of line
```

**解决：**
- `generate_patch()` 函数增加 `out = out.replace("\r\n", "\n")` 转换
- 对已有的 48 个补丁文件执行批量 CRLF → LF 转换

### 2.7 swebench 在 Windows 上缺少 resource 模块

**问题：** `swebench.harness` 导入了 Unix 专有的 `resource` 模块，Windows 上 `ModuleNotFoundError`。

**解决：**

- 创建 `swebench_win_fix.py`，注入 resource 桩模块
- 提供 `getrlimit()` / `setrlimit()` 的无操作实现

### 2.8 swebench 默认 GBK 读取 UTF-8 文件

**问题：** `swebench.harness.run_evaluation` 内部使用 `Path.read_text()` 读取 JSON 文件，不指定编码，在 Windows GBK 环境下解码 UTF-8 文件失败。

**解决：**

- `convert_predictions.py` 中 `ensure_ascii=True`（非 ASCII 字符转义为 `\uXXXX`）
- 运行评估时设置 `PYTHONUTF8=1` 环境变量

---

## 3. 评测结果分析

### 3.1 阶段一结果（Agent 生成补丁）

| 指标 | 数量 | 比例 |
|------|------|------|
| 总运行 Issue | 200 | 100% |
| ✅ 生成补丁（patched） | **48** | **24.0%** |
| ⏭ 无改动（no_change） | 122 | 61.0% |
| ❌ Agent 异常（agent_failed） | 30 | 15.0% |
| 💥 克隆失败（clone_failed） | 0 | 0% |

**补丁分布（按仓库）：**

| 仓库 | 补丁数 |
|------|--------|
| django/django | 26 |
| scikit-learn/scikit-learn | 5 |
| pytest-dev/pytest | 4 |
| astropy/astropy | 3 |
| matplotlib/matplotlib | 3 |
| pylint-dev/pylint | 3 |
| mwaskom/seaborn | 2 |
| psf/requests | 2 |

**分析：**

1. **24% patched** — 约 1/4 的 Issue 让 Agent 产生了代码修改。考虑到 MiMo 是免费模型，这个比例在可接受范围。

2. **61% no_change** — Agent 尝试了但最终没有修改代码。可能原因：
   - MiMo 模型能力有限，无法理解复杂 Issue
   - 25 步 max_steps 不够，Agent 还没找到修改方案时间就到了
   - Issue 描述在 GBK 环境下传输时部分内容损坏（虽然已修复，但早期数据可能已受影响）

3. **15% agent_failed** — Agent 运行出错退出。主要是早期配置问题（pyyaml 缺失、API Key 验证失败、编码问题），后期修复后此比例应大幅下降。

### 3.2 阶段二结果（Docker 验证补丁）

| 指标 | 数量 | 比例 |
|------|------|------|
| 提交验证 | 24 | 100% |
| 完成验证 | 21 | 87.5% |
| ⚠️ 错误 | 0 | 0% |
| ✅ **resolved（已解决）** | **0** | **0%** |
| ❌ unresolved（未通过） | 21 | 100% |
| 补丁成功应用（patch apply） | 21 | 100% |

**分析：**

1. **0% resolved** — 所有验证的补丁均未通过 SWE-bench 的测试用例。这是最核心的发现。

2. **100% patch apply** — CRLF 修复后，所有补丁都能成功打入 Docker 容器。技术流程完全打通。

3. **为什么 resolved = 0？**
   - 模型生成的补丁不够精确：MiMo-V2.5-Pro 虽然能产生语法正确的修改，但修改的内容不足以通过 SWE-bench 严格的测试套件
   - 测试用例要求严格：每个 Issue 有 `FAIL_TO_PASS` 测试（之前失败，修好后应通过）和 `PASS_TO_PASS`（不能退化）。Agent 的修改往往：
     - 未能修复 `FAIL_TO_PASS` 测试
     - 破坏了已有的 `PASS_TO_PASS` 测试
   - 25 步太少：Agent 可能找到了大致方向但没有足够步数精细调整

4. **3 条未完成** — 可能对应 Docker 镜像不支持的仓库版本。

### 3.3 与行业对比参考

| 模型 | SWE-bench Lite Resolved |
|------|------------------------|
| Claude 4 Sonnet | ~50% |
| Claude 3.5 Sonnet | ~45% |
| GPT-4o | ~35% |
| DeepSeek-V3 | ~30% |
| MiMo-V2.5-Pro（本评测） | **0%** |

> 注意：行业数据为近似值，实际结果取决于 Agent 框架实现、提示词设计和运行参数。

---

## 4. 后续优化方向

### 4.1 短期（低投入，可快速见效）

| 方向 | 预期效果 | 具体做法 |
|------|---------|---------|
| **增加 max_steps** | 减少 no_change | 从 25 提到 50-80，给 Agent 更多探索和修改时间 |
| **增加重试机制** | 减少 agent_failed | 临时性失败（API 超时等）自动重试 1-2 次 |
| **延长 timeout** | 减少超时中断 | agent timeout 从 600 提到 1200 秒 |
| **使用更强模型** | 提高补丁质量 | 如 DeepSeek-V3、Claude Sonnet 替代 MiMo |

### 4.2 中期（需要一定开发投入）

| 方向 | 预期效果 | 具体做法 |
|------|---------|---------|
| **优化 Agent 提示词** | 提高修改质量 | 针对 SWE-bench 场景优化 system prompt，强调测试覆盖 |
| **增加测试感知** | 提高 resolved 率 | Agent 运行完修改后，主动运行相关测试并迭代修正 |
| **多轮修复** | 提高 resolved 率 | 如果验证失败，把测试输出反馈给 Agent 再次修复 |
| **仓库针对性配置** | 提高 patched + resolved | 不同框架（Django/pytest/scikit-learn）使用不同的执行策略 |

### 4.3 长期（架构级改进）

| 方向 | 预期效果 | 具体做法 |
|------|---------|---------|
| **RAG 增强** | 提高 Issue 理解 | 检索相似 Issue 的修复 commit，作为示例提供给 Agent |
| **沙箱内自验证** | 提高补丁正确性 | Agent 在 Docker 沙箱内直接跑测试、验证、迭代 |
| **多 Agent 协作** | 提高复杂 Issue 修复率 | 一个 Agent 分析，一个 Agent 修改，一个 Agent 验证 |

### 4.4 当前评测流程本身的改进

| 问题 | 改进建议 |
|------|---------|
| 阶段一未记录 Agent 耗时分布 | 增加每次 agent 运行的耗时日志，便于分析瓶颈 |
| no_change 未区分"尝试过但放弃"和"完全未开始" | 增加 agent 的退出原因分类 |
| 只有 200 条非全量 | 后续可跑完剩余 100 条，获得完整 300 条基线 |
| Windows 兼容性问题多 | 建议在 Linux 环境运行评测（WSL2 / 云服务器），可避免大部分编码问题 |

---

## 5. 总结

### 评测结论

```
SWE-bench Lite 评测总结
═══════════════════════════════════
 模型:            MiMo-V2.5-Pro（免费 Token Plan）
 Agent:           Forge Agent（max_steps=25）
 阶段一(200条):   48 patched (24%), 122 no_change (61%), 30 failed (15%)
 阶段二(24条):    0 resolved (0%), 21 patch apply success (100%)
═══════════════════════════════════
```

### 关键收获

1. **技术流程已完全打通** — Forge Agent + SWE-bench 评测管线在 Windows 上可正常运行
2. **补丁成功率 24%** — MiMo 能产生语法正确的修改，约 1/4 的 Issue 能让 Agent 写出代码
3. **正确率 0%** — 但修改精度不足，无法通过严格的测试套件
4. **模型是最大瓶颈** — 免费模型的能力上限是当前最主要的限制因素

### 下一步建议

1. **先用更强模型跑一轮小测试**（如 DeepSeek-V3），看 resolved 率是否有质的提升
2. **同时增加 max_steps 到 50**，减少 no_change 比例
3. **在 Linux/WSL2 环境运行**，避免 Windows 编码/换行符问题
4. **对 django 仓库（26 个 patch，占一半以上）深入分析**，看是否有模式可循


