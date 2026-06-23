"""
swebench_win_fix.py  —  Windows 兼容补丁
把此文件放到项目根目录，运行 swebench 前先执行：
    python swebench_win_fix.py
它会注入缺失的 resource 模块。
"""
import sys
from pathlib import Path

# resource 模块是 Linux 专有，Windows 上没有。
# swebench.harness.prepare_images 会 import resource。
# 这个补丁注入一个兼容的 resource 桩模块，让 swebench 能在 Windows 上运行。

STUB = """\
# Windows stub for Unix resource module (injected by swebench_win_fix.py)
# 提供 resource.getrlimit / setrlimit 的无操作实现

RLIMIT_NOFILE = 0
RLIMIT_STACK = 1
RLIMIT_AS = 2

def getrlimit(what):
    # 返回（软限制，硬限制）= 无限制
    return (2147483647, 2147483647)

def setrlimit(what, limits):
    pass
"""

def apply():
    site_packages = None
    # 查找 site-packages 路径
    for p in sys.path:
        if "site-packages" in p.lower():
            site_packages = Path(p)
            break

    if not site_packages:
        # 直接注入 sys.modules
        import types
        mod = types.ModuleType("resource")
        exec(STUB, mod.__dict__)
        sys.modules["resource"] = mod
        print("[OK] resource 模块已注入到 sys.modules")
        return

    stub_path = site_packages / "resource.py"
    if stub_path.exists():
        print(f"[OK] resource 模块已存在: {stub_path}")
        return

    stub_path.write_text(STUB, encoding="utf-8")
    print(f"[OK] 已创建 resource 桩模块: {stub_path}")

if __name__ == "__main__":
    apply()
