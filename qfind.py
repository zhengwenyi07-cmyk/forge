from pathlib import Path 
import sys 
path, pat = sys.argv[1], sys.argv[2] 
for f in sorted(Path('.').glob(path)): 
    for i, line in enumerate(open(f, encoding='utf-8'), 1): 
        if pat in line: print(f'{f}:{i}: {line.rstrip()[:120]}') 
