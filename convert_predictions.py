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

# 只转换 patched 状态的
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

if not predictions:
    print("[WARN] 没有找到 patched 状态的补丁，退出")
    exit(0)

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(predictions, f, ensure_ascii=True, indent=2)

print(f"[OK] 转换完成: {len(predictions)} 条预测 → {OUTPUT_FILE}")
