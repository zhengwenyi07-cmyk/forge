"""
run_verification.py  —  SWE-bench 补丁验证（阶段二）

流程：
  1. 打 Windows 兼容补丁
  2. 加载 predictions.json
  3. 运行 swebench 评测（Docker）
  4. 输出结果

前提：
  - Docker Desktop 已启动
  - swebench 已安装
  - convert_predictions.py 已运行（生成了 predictions.json）

用法：
    python run_verification.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

EVAL_DIR = "D:\\fianll-test\\forge\\swebench_eval"
DATASET = "D:\\fianll-test\\forge\\forge-agent\\swebench_lite.json"
PREDICTIONS = f"{EVAL_DIR}\\predictions.json"
REPORT_DIR = f"{EVAL_DIR}\\verification"
SCRIPT_DIR = Path(__file__).parent

# 确保 predictions.json 存在
if not Path(PREDICTIONS).exists():
    print(f"[ERR] 未找到 {PREDICTIONS}")
    print("[HINT] 请先运行: python convert_predictions.py")
    sys.exit(1)

# 1. 打 Windows 兼容补丁
print("[1/4] 打 Windows 兼容补丁...")
subprocess.run(
    [sys.executable, str(SCRIPT_DIR / "swebench_win_fix.py")],
    check=True, capture_output=True,
)

# 2. 统计待验证补丁
preds = json.load(open(PREDICTIONS, encoding="utf-8"))
print(f"[2/4] 待验证补丁: {len(preds)} 条")

# 3. 运行评测
print(f"[3/4] 运行 SWE-bench 评测 (Docker)...")
Path(REPORT_DIR).mkdir(parents=True, exist_ok=True)
# 设置 PYTHONUTF8=1，让 Windows 下 Python 默认用 UTF-8 读文件（否则 GBK 乱码）
env = {**os.environ, "PYTHONUTF8": "1"}
cmd = [
    sys.executable, "-m", "swebench.harness.run_evaluation",
    "--dataset_name", DATASET,
    "--predictions_path", PREDICTIONS,
    "-id", "forge_eval_1",
    "--max_workers", "2",
    "--timeout", "1800",
    "--report_dir", REPORT_DIR,
]
print(f"  命令: {' '.join(cmd)}")
result = subprocess.run(cmd, env=env)
if result.returncode != 0:
    print(f"[WARN] swebench 退出码 {result.returncode}（可能部分失败）")

# 4. 输出结果
print(f"[4/4] 查看结果...")
result_file = Path(REPORT_DIR) / "results.json"
if result_file.exists():
    results = json.loads(result_file.read_text(encoding="utf-8"))
    total = len(results)
    resolved = sum(1 for r in results if r.get("resolved", False))
    print()
    print("=" * 42)
    print("  SWE-bench 验证结果")
    print("=" * 42)
    print(f"  验证补丁:  {total}")
    print(f"  ✅ 已解决:  {resolved} ({resolved/total*100:.1f}%)")
    print(f"  ❌ 未通过:  {total - resolved}")
    print("=" * 42)
    print(f"  详细报告: {REPORT_DIR}")
else:
    print(f"[WARN] 结果文件未生成: {result_file}")
    print("[HINT] 检查 Docker Desktop 是否正在运行")
