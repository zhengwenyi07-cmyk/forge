# SWE-bench Lite 评测指南

## 目录

1. [Forge Agent 能否用 SWE-bench 评测？](#1-forge-agent-能否用-swe-bench-评测)
2. [评测流程概览](#2-评测流程概览)
3. [准备工作（从零开始）](#3-准备工作从零开始)
4. [阶段一：运行 agent 生成补丁](#4-阶段一运行-agent-生成补丁)
5. [阶段二：Docker 验证补丁](#5-阶段二docker-验证补丁)
6. [FAQ](#6-faq)

---

## 1. Forge Agent 能否用 SWE-bench 评测？

**可以，但需要一定的适配工作。**

SWE-bench Lite 是 300 个真实 GitHub Issue 的评测集。每个 Issue 包含：
- 目标仓库 + 基准 commit（修改前的代码状态）
- Issue 标题 + 描述（告诉 agent 要修什么）
- 测试脚本（验证 agent 的修改是否正确）

Forge Agent 能做的事情：
- ✅ 接收 Issue 描述 → 理解问题 → 探索代码 → 修改 → 提交
- ✅ 通过 `--sandbox` 用 Docker 容器隔离执行
- ❌ **缺少** SWE-bench 官方评测流程的对接（需要额外写一个评测脚本）

---

## 2. 评测流程概览

```
① 获取 SWE-bench Lite 数据集
    ↓
② 安装评测依赖（Docker + swebench 包）
    ↓
③ 对每个 Issue：
     ├─ 克隆目标仓库的基准 commit
     ├─ forge-agent 运行并产生修改（生成 .patch 补丁文件）
     └─ 用 SWE-bench 的 Docker 环境验证补丁
    ↓
④ 汇总结果 → 算出 % resolved
```

---

## 3. 准备工作（从零开始）

### 3.1 硬件要求

| 项目 | 最低要求 | 建议 |
|------|---------|------|
| 磁盘空间 | 60 GB | 100 GB（Docker 镜像 + 300 个仓库很占空间）|
| 内存 | 8 GB | 16 GB |
| 网络 | 可访问 GitHub + Docker Hub | 宽带（首次需下载大量镜像）|
| 运行时间 | — | 跑完 300 个 Issue 大约需要 6-24 小时 |

### 3.2 需要安装的软件

| 软件 | 用途 | 下载地址 |
|------|------|---------|
| **Docker Desktop** | 运行评测容器 | https://www.docker.com/products/docker-desktop/ |
| **Git** | 克隆仓库 | 你已安装 |
| **Python 3.8+** | 运行评测脚本 | 你已安装（当前 conda forge 环境）|

### 3.3 需要安装的 Python 包

在 cmd（先激活 conda `forge` 环境）中运行：

```cmd
REM 激活 forge 环境（如果是我的话，这句我已经在了）
conda activate forge

REM 进入项目目录
cd D:\fianll-test\forge\forge-agent

REM 安装评测必要的包
pip install swebench
pip install datasets
```

如果下载慢，加 `-i https://pypi.tuna.tsinghua.edu.cn/simple` 参数。

<!-- 注意：下面假设 `swebench` 包可正常安装，安装后会提供 `swebench` 命令行工具。如果 swebench 安装不上（国内网络问题），见 FAQ。 -->

### 3.4 配置小米 MiMo API（如果你用 MiMo 模型）

项目里已为你创建了评测专用配置文件 `config/swebench-mimo.yaml`，用 MiMo-V2.5-Pro 模型。

**设置 API Key（cmd 中运行）：**

```cmd
set MIMO_API_KEY=tp-xxxxx
```

把 `tp-xxxxx` 换成你实际的 API Key。

> ⚠️ **注意：** 每次**重新打开 cmd** 都需要重新设一次这个环境变量。如果不想每次重设，可以：
> 1. 打开 `config/swebench-mimo.yaml`
> 2. 把 `${MIMO_API_KEY}` 直接换成你的 key（`tp-xxxxx`）
>
> 但注意：这样 key 会明文保存在文件里，别把文件分享出去。

---

## 4. 阶段一：运行 agent 生成补丁

### 第 1 步：启动 Docker Desktop

- 双击桌面上的 **Docker Desktop** 图标
- 等待右下角 Docker 图标变稳定（不再转圈）
- 打开 cmd，运行以下命令确认 Docker 正常工作：

  ```cmd
  docker ps
  ```

  出现类似以下输出说明 Docker 正常：

  ```
  CONTAINER ID   IMAGE     COMMAND   CREATED   STATUS    PORTS     NAMES
  ```

  （如果提示 `docker: command not found`，说明 Docker Desktop 没装好或没启动）

### 第 2 步：下载 SWE-bench Lite 数据集

在 cmd 中运行：

```cmd
python -c "
from datasets import load_dataset

# 下载 SWE-bench Lite 数据集（约 300 个 Issue）
dataset = load_dataset('swe-bench/swe-bench-lite-split', split='test')
print(f'数据集大小: {len(dataset)} 个 Issue')

# 保存到本地 JSON 文件，方便后续使用
import json
with open('swebench_lite.json', 'w', encoding='utf-8') as f:
    json.dump([dataset[i] for i in range(len(dataset))], f, ensure_ascii=False, indent=2)

print('已保存到 swebench_lite.json')
"
```

如果这个命令跑不通（`datasets` 库下载数据集需要网络），也可以用下面这个备用方法手动下载数据（见 FAQ）。

验证下载成功：

```cmd
python -c "import json; data=json.load(open('swebench_lite.json', encoding='utf-8')); print(f'共 {len(data)} 条数据'); print('第一条示例:'); print(json.dumps(data[0], ensure_ascii=False, indent=2)[:500])"
```

### 第 3 步：查看数据集结构

```cmd
python -c "import json; data = json.load(open('swebench_lite.json', encoding='utf-8')); item = data[0]; print('字段列表:', list(item.keys())); print(); print('repo:', item['repo']); print('base_commit:', item['base_commit']); print('issue number:', item.get('issue', '?')); print(); print('问题描述（前300字）:'); print(item['problem_statement'][:300])"
```

你会看到类似：

```
字段列表: ['repo', 'base_commit', 'hints_text', 'created_at', 'text', 'commit_hash', 'repo_directory', 'version', 'FAIL_TO_PASS', 'PASS_TO_PASS', 'environment_setup_commit']

repo: django/django
base_commit: abc123...
问题描述: Fix Error: ...
```

每个字段的含义：
- **`repo`** — 目标仓库（如 `django/django`）
- **`base_commit`** — 修改前的基准 commit hash
- **`text`** — Issue 的标题 + 描述（这就是喂给 agent 的任务描述）
- **`FAIL_TO_PASS`** — 之前失败但修改后应该通过的测试
- **`PASS_TO_PASS`** — 修改前后都应该通过的测试（回归测试）
- **`commit_hash`** — 正确的修复 commit（用于评分比对的参考答案）

### 第 4 步：编写评测脚本

将以下内容保存为 `run_swebench_eval.py`，放在 `D:\fianll-test\forge\forge-agent\` 目录下：

```python
"""
SWE-bench Lite 评测脚本。

流程：
  1. 读取 swebench_lite.json
  2. 对每个 Issue：
      a. 克隆目标仓库到 /tmp/swebench_repos/{repo}/
      b. 重置到 base_commit → 创建新分支
      c. 运行 forge-agent（传入 Issue 描述）
      d. 生成 .patch 补丁文件
      e. （可选）用 Docker 验证补丁是否正确
  3. 统计结果

用法：
    python run_swebench_eval.py --max-issues 5
    python run_swebench_eval.py --max-issues 300 --eval-dir D:/swebench_eval
"""

"""
SWE-bench Lite 评测脚本（v2 — 断点续跑 + 精简输出）

流程：
  1. 读取 swebench_lite.json
  2. 检查已有进度，跳过已完成的 Issue
  3. 对每个未完成的 Issue：
      a. 克隆目标仓库 / 重置到 base_commit
      b. 运行 forge-agent 修复
      c. 生成 .patch 补丁文件
      d. 保存进度
  4. 汇总统计

用法：
    python run_swebench_eval.py                         # 跑前 5 条（默认）
    python run_swebench_eval.py --max-issues 300        # 全量
    python run_swebench_eval.py --max-issues 300 --eval-dir D:/swebench_eval
    python run_swebench_eval.py --resume                 # 继续上次未完成的
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Windows GBK 终端兼容：注册 stdout/stderr 的 UTF-8 编码
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ===========================================================================
# 默认配置
# ===========================================================================

DEFAULT_MAX_ISSUES = 5          # 默认跑 5 条，确认流程后改 300
DEFAULT_EVAL_DIR = "D:\\fianll-test\\forge\\swebench_eval"
CONFIG_FILE = "config/swebench-mimo.yaml"
MAX_STEPS = 25                    # 每个 Issue 最大 agent 步数
AGENT_TIMEOUT = 600               # 每个 Issue 超时（秒）


# ===========================================================================
# 工具函数
# ===========================================================================

def run_cmd(cmd, cwd=None, timeout=60):
    """运行命令，返回 (success, stdout, stderr)"""
    try:
        env = None
        if sys.platform == "win32":
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.run(
            cmd, capture_output=True, encoding="utf-8", errors="replace",
            cwd=cwd, timeout=timeout, shell=True, env=env,
        )
        return proc.returncode == 0, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "", "TIMEOUT"
    except Exception as e:
        return False, "", str(e)


# ===========================================================================
# 核心步骤
# ===========================================================================

def clone_and_reset(repo_name, base_commit, repo_dir):
    """克隆目标仓库并重置到 base_commit"""
    repo_path = Path(repo_dir) / repo_name.replace("/", "_")

    if not repo_path.exists():
        print(f"  [..] 克隆 {repo_name} ...")
        ok, out, err = run_cmd(
            f"git clone https://github.com/{repo_name}.git {repo_path}",
            timeout=300,
        )
        if not ok:
            print(f"  [ERR] 克隆失败: {err[:200]}")
            return None
    else:
        print(f"  [OK] 使用已有仓库")

    # 重置到 base_commit
    print(f"  [..] 重置到 {base_commit[:8]} ...")
    ok, out, err = run_cmd(
        f"git checkout --force {base_commit} && git clean -fd",
        cwd=str(repo_path), timeout=30,
    )
    if not ok:
        print(f"  [ERR] 重置失败: {err[:200]}")
        return None

    return str(repo_path)


def run_agent(issue_text, repo_path, issue_id):
    """用 forge-agent 修复 Issue，静默运行（不显示 agent 内部输出）"""
    # 用临时文件传递任务描述，避免 cmd 命令行中双引号冲突
    # 使用 EVAL_DIR 路径避免中文用户名导致 cmd.exe 乱码
    task_file = Path(REPOS_DIR).parent / f"task_{issue_id}.txt"
    task_file.write_text(issue_text, encoding="utf-8")
    cmd = (
        f"python -m entry.cli --config {CONFIG_FILE} run "
        f'--task-file "{task_file}" '
        f"--max-steps {MAX_STEPS} "
        f"--repo {repo_path}"
    )
    # 捕获输出，不打印到控制台
    ok, out, err = run_cmd(cmd, timeout=AGENT_TIMEOUT)
    task_file.unlink(missing_ok=True)
    if not ok:
        # 写完整错误到日志文件
        err_log = Path(PATCHES_DIR) / f"error_{issue_id}.log"
        err_log.write_text(f"CMD: {cmd}\n\nSTDERR:\n{err}\n\nSTDOUT:\n{out}", encoding="utf-8")
        print(f"  [WARN] agent 异常退出（完整错误 → {err_log}）")
    return ok, out, err


def generate_patch(repo_dir, issue_id):
    """从 agent 的修改生成补丁文件"""
    patch_dir = Path(PATCHES_DIR)
    patch_dir.mkdir(parents=True, exist_ok=True)
    patch_file = patch_dir / f"issue_{issue_id:04d}.patch"

    ok, out, err = run_cmd("git diff", cwd=repo_dir, timeout=10)
    if out.strip():
        patch_file.write_text(out, encoding="utf-8")
        return str(patch_file)
    return None


# ===========================================================================
# 进度管理（断点续跑）
# ===========================================================================

def load_progress(progress_file):
    """加载已有进度，返回 {idx: result_dict} 字典"""
    if not Path(progress_file).exists():
        return {}
    try:
        data = json.load(open(progress_file, encoding="utf-8"))
        if isinstance(data, list):
            return {r["idx"]: r for r in data}
        return {}
    except Exception:
        return {}


def save_progress(progress_file, results):
    """追加保存一条结果到进度文件"""
    with open(progress_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


def print_summary(results, total_count):
    """打印当前统计摘要"""
    total = len(results)
    patched = sum(1 for r in results if r["status"] == "patched")
    no_change = sum(1 for r in results if r["status"] == "no_change")
    agent_failed = sum(1 for r in results if r["status"] == "agent_failed")
    clone_failed = sum(1 for r in results if r["status"] == "clone_failed")
    remaining = total_count - total

    print()
    print("=" * 52)
    print("  SWE-bench Lite 评测进度")
    print("=" * 52)
    print(f"  总计:        {total_count}")
    print(f"  已完成:      {total}  ({total/total_count*100:.1f}%)")
    print(f"  剩余:        {remaining}")
    print(f"  ─────────────────────────")
    print(f"  [PASS] 生成补丁:  {patched}  ({patched/total*100:.1f}% of completed)" if total else "")
    print(f"  [SKIP] 无改动:    {no_change}")
    print(f"  [FAIL] Agent失败: {agent_failed}")
    print(f"  [ERR] 克隆失败:  {clone_failed}")
    print("=" * 52)


# ===========================================================================
# 主流程
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description="SWE-bench Lite 评测脚本")
    parser.add_argument("--max-issues", type=int, default=DEFAULT_MAX_ISSUES,
                        help=f"最多评测多少条（默认 {DEFAULT_MAX_ISSUES}）")
    parser.add_argument("--eval-dir", default=DEFAULT_EVAL_DIR,
                        help=f"评测数据目录（默认 {DEFAULT_EVAL_DIR}）")
    parser.add_argument("--resume", action="store_true",
                        help="断点续跑：跳过已完成的 Issue")
    args = parser.parse_args()

    # 目录
    global REPOS_DIR, PATCHES_DIR
    EVAL_DIR = args.eval_dir
    REPOS_DIR = f"{EVAL_DIR}/repos"
    PATCHES_DIR = f"{EVAL_DIR}/patches"
    progress_file = f"{EVAL_DIR}/results.json"
    Path(REPOS_DIR).mkdir(parents=True, exist_ok=True)
    Path(PATCHES_DIR).mkdir(parents=True, exist_ok=True)

    # 加载数据集
    data = json.load(open("swebench_lite.json", encoding="utf-8"))
    total_count = min(len(data), args.max_issues)
    print(f"[SWE-bench Lite] 共 {len(data)} 条，本次跑前 {total_count} 条\n")

    # 加载已有进度
    completed = load_progress(progress_file) if args.resume else {}
    if completed:
        print(f"[RESUME] 断点续跑模式：已跳过 {len(completed)} 条已完成的 Issue")
        print_summary(list(completed.values()), total_count)
        print()

    # 主循环
    results = list(completed.values())  # 已有结果
    for i, item in enumerate(data[:total_count]):
        idx = i

        # 跳过已完成的
        if idx in completed:
            repo = item["repo"]
            status = completed[idx]["status"]
            icon = {"patched": "[PASS]", "no_change": "[SKIP]", "agent_failed": "[FAIL]", "clone_failed": "[ERR]"}.get(status, "[?]")
            print(f"[{i+1}/{total_count}] {icon} {repo}  → {status}（跳过）")
            continue

        repo = item["repo"]
        commit = item["base_commit"]
        issue_text = item["problem_statement"]
        issue_id = item.get("instance_id", f"{repo}-{i}")
        icon = ""

        print(f"[{i+1}/{total_count}] {repo}")
        print(f"  Model: {CONFIG_FILE.replace('config/','').replace('.yaml','')} | Issue: {issue_id} | 步数上限: {MAX_STEPS}")

        # 步骤 A：克隆并重置
        repo_dir = clone_and_reset(repo, commit, REPOS_DIR)
        if not repo_dir:
            result = {"idx": idx, "issue_id": issue_id, "repo": repo, "status": "clone_failed", "patch_file": None}
            results.append(result)
            save_progress(progress_file, results)
            print(f"  -> 状态: [ERR] clone_failed\n")
            continue

        # 步骤 B：创建新分支
        branch = f"agent-fix-{int(time.time())}"
        run_cmd(f"git checkout -b {branch}", cwd=repo_dir, timeout=10)

        # 步骤 C：运行 agent（静默）
        print(f"  [..] 运行 agent（最多 {MAX_STEPS} 步）...", end="", flush=True)
        t0 = time.time()
        agent_ok, agent_out, agent_err = run_agent(issue_text, repo_dir, idx)
        elapsed = time.time() - t0
        print(f" 耗时 {elapsed:.0f}s")

        # 步骤 D：生成补丁
        patch_file = generate_patch(repo_dir, idx)

        # 判定状态
        if not agent_ok:
            status = "agent_failed"
        elif patch_file:
            status = "patched"
        else:
            status = "no_change"

        # 状态图标
        icons = {"patched": "[PASS]", "no_change": "[SKIP]", "agent_failed": "[FAIL]", "clone_failed": "[ERR]"}
        icon = icons.get(status, "❓")

        result = {"idx": idx, "issue_id": issue_id, "repo": repo,
                  "status": status, "patch_file": patch_file}
        results.append(result)

        # 保存进度
        save_progress(progress_file, results)

        print(f"  → 状态: {icon} {status}\n")

    # 最终汇总
    print_summary(results, total_count)
    print(f"\n补丁目录: {PATCHES_DIR}/")
    print(f"详细结果: {progress_file}")


if __name__ == "__main__":
    main()

```

### 第 5 步：试跑一小批

先用 5 个 Issue 验证流程能走通：

```cmd
python run_swebench_eval.py
```

这需要一段时间（每个 Issue 大约 2-5 分钟，5 个约 10-25 分钟）。它会：
1. 克隆目标仓库（只克隆一次，后续复用）
2. 重置到 Issue 对应的基准 commit
3. 跑 agent 修复
4. 生成 .patch 补丁文件到 `/tmp/swebench_eval/patches/`
5. 输出统计结果



-----------------断点续跑：

  python run_swebench_eval.py --max-issues 200 --resume

加 --resume 参数。不加的话它会忽略 results.json，从头开始跑

这样就会读取 D:\fianll-test\forge\swebench_eval\results.json 里已完成的 5 条，从第 6 条继续。



### 第 6 步：跑全量 300 条

确认 5 条能跑通后，打开 `run_swebench_eval.py`，把开头的：

```python
MAX_ISSUES = 5
```

改成：

```python
MAX_ISSUES = 300      # 跑全量
```

然后重新运行：

```cmd
python run_swebench_eval.py
```

**⚠️ 注意：全量跑大约需要 6-24 小时，建议晚上睡觉前跑。**

### 第 7 步：看结果

跑完后查看统计：

```cmd
python -c "
import json
results = json.load(open('/tmp/swebench_eval/results.json'))
total = len(results)
patched = sum(1 for r in results if r['status'] == 'patched')
print(f'总数: {total}')
print(f'产生补丁: {patched}')
print(f'成功率: {patched/total*100:.1f}%')
"
```

> **注意：** 这个成功率是"agent 产生了补丁"的比例，不是"补丁正确修复了 Issue"的比例。要验证补丁是否正确，需要用 SWE-bench 官方的 Docker 评测环境跑测试用例——那需要额外配置 Docker 评测镜像（见后续章节）。

---

## 5. 阶段二：Docker 验证补丁

> ⏱ **在你开始之前：** 你已经跑完了阶段一，200 条中 48 条生成了补丁。阶段二就是用 Docker 验证这 48 个补丁**能否真正通过测试**。

### 5.1 整体流程

```
你的成果：48 个 .patch 文件（在 D:\fianll-test\forge\swebench_eval\patches\）
              ↓
① 安装 swebench-harness（评测框架）
              ↓
② 转换 results.json 为 SWE-bench 预测格式
              ↓
③ 启动 Docker Desktop
              ↓
④ 运行评测 → 自动拉取 Docker 镜像 → 打补丁 → 跑测试
              ↓
⑤ 看最终结果：多少 Issue 真正 resolved
```

### 5.2 安装 SWE-bench 评测框架

```cmd
REM 激活环境
conda activate forge

REM 安装 swebench-harness（官方评测包）
pip install swebench-harness

REM 如果慢就加清华镜像
pip install swebench-harness -i https://pypi.tuna.tsinghua.edu.cn/simple
```

如果提示 `swebench-harness` 找不到，也可以装完整版（更大但功能全）：

```cmd
pip install swebench
```

验证安装成功：

```cmd
python -c "from swebench.harness import run_evaluation; print('swebench OK')"
```

### ⚠️ Windows 兼容补丁

`swebench` 用了一个 Linux 专有的 `resource` 模块，Windows 上需要打一个桩补丁。项目里已为你创建了 `swebench_win_fix.py`：

```cmd
python swebench_win_fix.py
```

看到 `[OK]` 即可。只需运行一次，后续 swebench 命令都会自动生效。

### 5.3 转换预测格式

SWE-bench 评测需要 predictions 文件是特定格式——每条包含 `instance_id` 和 `model_patch`（补丁内容）。你的 `results.json` 里存的是文件路径，需要展开为实际内容。

将以下内容保存为 `convert_predictions.py`：

```python
"""
convert_predictions.py

把 run_swebench_eval.py 的 results.json + .patch 文件
转换为 SWE-bench 评测需要的 predictions.json 格式

用法：
    python convert_predictions.py
"""
import json
from pathlib import Path

EVAL_DIR = "D:\\fianll-test\\forge\\swebench_eval"
PATCHES_DIR = f"{EVAL_DIR}/patches"
RESULTS_FILE = f"{EVAL_DIR}/results.json"
OUTPUT_FILE = f"{EVAL_DIR}/predictions.json"

# 只提交 patched 状态的
results = json.load(open(RESULTS_FILE, encoding="utf-8"))
predictions = []

for r in results:
    if r["status"] != "patched":
        continue

    patch_path = r.get("patch_file")
    if not patch_path or not Path(patch_path).exists():
        print(f"  [WARN] 补丁文件不存在: {patch_path}，跳过")
        continue

    patch_content = Path(patch_path).read_text(encoding="utf-8")
    if not patch_content.strip():
        print(f"  [WARN] 补丁为空: {patch_path}，跳过")
        continue

    predictions.append({
        "instance_id": r.get("issue_id", f"{r['repo']}-{r.get('idx', 0)}"),
        "model_patch": patch_content,
        "model_name_or_path": "forge-agent-mimo-v2.5-pro",
    })

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(predictions, f, ensure_ascii=False, indent=2)

print(f"转换完成: {len(predictions)} 条预测 → {OUTPUT_FILE}")
```

运行：

```cmd
python convert_predictions.py
```

成功输出示例：

```
转换完成: 48 条预测 → D:\fianll-test\forge\swebench_eval\predictions.json
```

### 5.4 启动 Docker Desktop

如果你还没装 Docker Desktop：

1. 访问 https://www.docker.com/products/docker-desktop/
2. 下载 **Docker Desktop for Windows**
3. 安装（一路下一步，推荐选 **WSL 2** 后端）
4. 安装后启动，等待右下角鲸鱼图标稳定
5. 打开 cmd，验证：

```cmd
docker ps
```

输出应该类似：

```
CONTAINER ID   IMAGE     COMMAND   CREATED   STATUS    PORTS     NAMES
```

（空的容器列表 = 正常运行）

### 5.5 运行评测

> ⚠️ **注意：** SWE-bench 评测需要从 Docker Hub 拉取镜像，每个仓库约 1-5 GB。你只跑了 astropy 等几个仓库的 Issue，所以只需要拉取这几个仓库的镜像。首次拉取较慢，后续快。

```cmd
REM 进入项目目录
cd D:\fianll-test\forge\forge-agent

REM 先打 Windows 兼容补丁（只需一次）
python swebench_win_fix.py

REM 运行评测
python -m swebench.harness.run_evaluation \
    --dataset_name "D:\fianll-test\forge\forge-agent\swebench_lite.json" \
    --predictions_path "D:\fianll-test\forge\swebench_eval\predictions.json" \
    -id forge_eval_1 \
    --max_workers 2 \
    --timeout 1800 \
    --report_dir "D:\fianll-test\forge\swebench_eval\verification"
```

或者用 Python 脚本方式（更可控）：

```python
"""
run_verification.py  —  运行 SWE-bench 补丁验证

用法：
    python run_verification.py
"""
import json
import subprocess
import sys

EVAL_DIR = "D:\\fianll-test\\forge\\swebench_eval"
DATASET = "D:\\fianll-test\\forge\\forge-agent\\swebench_lite.json"
PREDICTIONS = f"{EVAL_DIR}\\predictions.json"
REPORT_DIR = f"{EVAL_DIR}\\verification"

# 1. 打 Windows 兼容补丁
subprocess.run([sys.executable, "swebench_win_fix.py"], check=True)

# 2. 加载 predictions 看有多少条
preds = json.load(open(PREDICTIONS, encoding="utf-8"))
print(f"[INFO] 共 {len(preds)} 个补丁待验证")

# 3. 运行评测
cmd = [
    sys.executable, "-m", "swebench.harness.run_evaluation",
    "--dataset_name", DATASET,
    "--predictions_path", PREDICTIONS,
    "--max_workers", "2",
    "--timeout", "1800",
    "--report_dir", REPORT_DIR,
]
print(f"[INFO] 运行命令: {' '.join(cmd)}")
subprocess.run(cmd, check=True)

# 4. 查看结果
result_file = f"{REPORT_DIR}\\results.json"
if __name__ == "__main__":
    try:
        results = json.load(open(result_file, encoding="utf-8"))
        total = len(results)
        resolved = sum(1 for r in results if r.get("resolved", False))
        print(f"\n======= 最终结果 =======")
        print(f"  验证总数: {total}")
        print(f"  ✅ 已解决: {resolved} ({resolved/total*100:.1f}%)")
        print(f"  ❌ 未通过: {total - resolved}")
    except FileNotFoundError:
        print(f"[WARN] 结果文件未生成，请检查 Docker 是否正常运行")
```

### 5.6 看最终结果

跑完后查看摘要：

```cmd
python -c "
import json

results = json.load(open('D:/fianll-test/forge/swebench_eval/verification/final_results.json'))
total = len(results)
resolved = sum(1 for r in results if r.get('resolved', False))
failed = sum(1 for r in results if r.get('resolved') == False)

print('====== SWE-bench Lite 最终评测结果 ======')
print(f'  已验证补丁:    {total}')
print(f'  ✅ 已解决:     {resolved} ({resolved/total*100:.1f}%)')
print(f'  ❌ 未通过:     {failed}')
print()

# 列出具体 resolved 的 Issue
print('--- 已解决的 Issue ---')
for r in results:
    if r.get('resolved'):
        print(f'  ✅ {r[\"instance_id\"]}')
print()
print('--- 未通过的 Issue ---')
for r in results:
    if not r.get('resolved'):
        print(f'  ❌ {r[\"instance_id\"]}  ({r.get(\"error\", \"未知错误\")[:80]})')
"
```

### 5.7 如果不跑 Docker 验证，有没有替代方案？

如果你暂时不想装 Docker（或者 Docker 在 Windows 上遇到问题），可以做**快速检查**来初步评估补丁质量：

```cmd
REM 统计补丁大小（行数越多通常改动越大）
python -c "
import json, os
from pathlib import Path

results = json.load(open('D:/fianll-test/forge/swebench_eval/results.json'))
patches_dir = 'D:/fianll-test/forge/swebench_eval/patches'

for r in results:
    if r['status'] == 'patched':
        patch = Path(patches_dir) / f\"issue_{r['idx']:04d}.patch\"
        if patch.exists():
            lines = len(patch.read_text().splitlines())
            print(f\"  {r['issue_id']}: {lines} 行改动\")
"
```

**补丁质量的粗略判断标准：**

| 特征 | 可能意味着 |
|------|-----------|
| 只有 1-3 行改动 | 可能是简单的配置/导入修复，也可能是误改 |
| 10-50 行改动 | 典型的 Bug 修复 |
| 100+ 行改动 | 较大改动，可能包含了不必要的变更 |
| 只改了空格/注释 | ❌ 无效补丁 |
| 改了测试文件 | 可能是绕过测试，要注意 |

### 5.8 常见问题

**Q: `swebench-harness` 安装报错？**

A: 如果 pip 装不上，可以不用 swebench 包，手动用 Docker 验证：

```cmd
REM 用 Docker 手动验证某个补丁
cd D:\fianll-test\forge\swebench_eval\repos\astropy_astropy

REM 重置到 base_commit
git checkout --force BASE_COMMIT_HASH
git clean -fd

REM 打补丁
git apply D:\fianll-test\forge\swebench_eval\patches\issue_0000.patch

REM 跑仓库自带的测试
python -m pytest tests/ -x --tb=short
```

**Q: Docker 拉取镜像太慢？**

A: 配置国内镜像加速：

```cmd
REM Docker Desktop → 设置 → Docker Engine → 添加 registry-mirrors
REM 推荐镜像加速器：https://docker.mirrors.ustc.edu.cn
REM 或：https://registry.docker-cn.com
```

**Q: 提示 `docker: command not found`？**

A: Docker Desktop 没启动。双击桌面 Docker Desktop 图标，等右下角鲸鱼图标稳定后再试。

**Q: 验证结果发现 resolved = 0？**

A: 这是正常现象。SWE-bench 的测试非常严格——补丁必须精确修复 Issue，测试全部通过才算 resolved。48 个补丁中即使只有几个 resolved 也是不错的成果。可以查看 verification 目录下的详细日志，分析每个补丁具体哪条测试没通过。

---

## 6. FAQ

### Q1: `pip install swebench` 装不上怎么办？

SWE-bench 官方包可能在国内网络下难以直接安装。替代方案：

**方案 A：使用国内镜像**

```cmd
pip install swebench -i https://pypi.tuna.tsinghua.edu.cn/simple
```

**方案 B：不使用 swebench 包，手动跑**

上面的 `run_swebench_eval.py` 脚本不依赖 `swebench` 包——它只用了 `git`、`json`、`subprocess` 等标准库就能跑。你完全可以**不安装 swebench 包**，直接运行脚本即可。

### Q2: `datasets.load_dataset('swe-bench/swe-bench-lite-split')` 下载失败？

如果 Hugging Face 被屏蔽，可以用 GitHub 上的数据文件：

```cmd
REM 从 GitHub 直接下载 SWE-bench Lite 数据
curl -L -o swebench_lite.json ^
  https://raw.githubusercontent.com/princeton-nlp/SWE-bench/main/swebench_lite.json
```

如果 `curl` 没有，用 Python：

```cmd
python -c "
import urllib.request, json
url = 'https://raw.githubusercontent.com/princeton-nlp/SWE-bench/main/swebench_lite.json'
with urllib.request.urlopen(url) as f:
    data = json.load(f)
with open('swebench_lite.json', 'w', encoding='utf-8') as out:
    json.dump(data, out, ensure_ascii=False, indent=2)
print(f'下载完成，共 {len(data)} 条')
"
```

### Q3: Docker Desktop 启动失败或 docker 命令用不了？

- Windows 上 Docker Desktop 需要开启 **WSL 2** 或 **Hyper-V**
- 确认 BIOS 中开启了虚拟化（Intel VT-x / AMD-V）
- 下载 Docker Desktop 后启动，等右下角鲸鱼图标稳定
- 运行 `docker ps` 看是否正常

### Q4: 全量跑 300 个 Issue 太慢怎么办？

- 先只跑前 5-10 个，确认流程没问题
- 如果要跑全量，可以用 `--max-workers 3` 同时跑 3 个 Issue（注意 API 配额）
- 如果 API 有限额，建议限速，比如每 30 秒启动一个

### Q5: 我的 API Key 不够跑 300 个 Issue 怎么办？

每个 Issue 大约消耗 50k-200k token。300 个 Issue 大约需要 15M-60M token，按 DeepSeek 价格算大约 $3-$12。注意预算。

### Q6: 补丁如何才算"正确"？

可以在 SWE-bench 官网上提交补丁进行验证，或者使用本地 Docker 评测环境。最直观的判断方式：

```cmd
REM 检查补丁是否非空（至少 agent 有修改）
wc -l /tmp/swebench_eval/patches/*.patch

REM 检查补丁是否合理（改动的是代码逻辑，不是空格/注释）
cat /tmp/swebench_eval/patches/issue_000.patch
```

---

## 总结

```
准备工作：
  1. 启动 Docker Desktop
  2. pip install swebench datasets（可选）
  3. 下载数据集 → swebench_lite.json

运行评测：
  4. python run_swebench_eval.py（先跑 5 条）
  5. 没问题后改成 300 条跑全量

看结果：
  6. 查看 /tmp/swebench_eval/results.json
  7. 查看生成的 .patch 文件

轻松上手建议：先跑 5 条，确认流程通顺，再全量。
```
