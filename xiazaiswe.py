import json, os
from datasets import load_dataset
from tqdm import tqdm

# 加载数据集（库自带下载缓存/断点续传）
dataset = load_dataset('swe-bench/SWE-bench_Lite', split='test')
total = len(dataset)
print(f'数据集大小: {total} 个 Issue')

json_path = 'swebench_lite.json'

# ------ 断点续写：读已有数据 ------
if os.path.exists(json_path):
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            existing = json.load(f)
        start = len(existing)
        print(f'已有 {start} 条，将从第 {start} 条继续...')
    except (json.JSONDecodeError, FileNotFoundError):
        existing = []
        start = 0
else:
    existing = []
    start = 0

# ------ 逐条读取 + 进度条 + 定期保存 ------
for i in tqdm(range(start, total), desc='保存进度', unit='条'):
    existing.append(dataset[i])
    if (i + 1) % 50 == 0:          # 每 50 条存一次盘
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)

# 最终完整保存
with open(json_path, 'w', encoding='utf-8') as f:
    json.dump(existing, f, ensure_ascii=False, indent=2)

print(f'完成！已保存到 {json_path}，共 {len(existing)} 条')