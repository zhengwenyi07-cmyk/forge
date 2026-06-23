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

DEFAULT_MAX_ISSUES = 200          # 默认跑 5 条，确认流程后改 300
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
        # Windows git diff 产生 CRLF，但 Docker Linux 的 patch 只认 LF
        out = out.replace("\r\n", "\n")
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
