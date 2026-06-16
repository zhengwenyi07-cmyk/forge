from context.repo_map import RepoMap
rm = RepoMap('.')
files = rm._scan()
files.sort(key=lambda f: f.importance_score(), reverse=True)

# 打印排名前 10
for i, fi in enumerate(files[:10], 1):
    score = fi.importance_score()
    tops = sum(1 for s in fi.symbols if s.is_toplevel)
    print(f'{i:2d}. score={score:6.1f}  top={tops:2d}  size={fi.size:6d}B  {fi.rel_path}')