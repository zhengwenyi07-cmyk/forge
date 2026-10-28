"""Local masked credential window. Never prints or uploads API keys."""
from __future__ import annotations

import argparse
import os
from pathlib import Path


def main():
    import tkinter as tk
    from tkinter import messagebox
    import yaml

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "config" / "mimo.local.yaml")
    args = parser.parse_args()
    if not args.output.name.endswith(".local.yaml"):
        parser.error("Credential files must use the ignored *.local.yaml suffix")
    window = tk.Tk()
    window.title("Forge — MiMo 本地配置")
    window.geometry("600x320")
    window.resizable(False, False)
    fields = {}
    for row, (name, label, default) in enumerate([
        ("api_key", "新的 API Key", ""),
        ("model", "模型名称（按控制台填写）", "mimo-v2.6-pro"),
        ("base_url", "OpenAI 兼容接口地址", "https://api.xiaomimimo.com/v1"),
    ]):
        tk.Label(window, text=label, anchor="w").grid(row=row * 2, column=0, padx=20, pady=(10, 0), sticky="w")
        entry = tk.Entry(window, width=76, show="*" if name == "api_key" else "")
        entry.insert(0, default)
        entry.grid(row=row * 2 + 1, column=0, padx=20, pady=3)
        fields[name] = entry
    tk.Label(window, text="仅保存到 config/mimo.local.yaml；该文件被 Git 忽略。", fg="#555555").grid(row=6, column=0, pady=8)

    def save():
        values = {name: entry.get().strip() for name, entry in fields.items()}
        if not all(values.values()) or not values["base_url"].startswith("https://"):
            messagebox.showerror("配置不完整", "请填写 Key、模型名称和 https:// 接口地址。")
            return
        config = {"llm": {"provider": "openai", **values, "max_tokens": 8192}, "agent": {"max_steps": 40, "budget_tokens": 80000, "log_dir": "./logs"}}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive permissions on POSIX; existing Windows user ACLs apply on Windows.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            yaml.safe_dump(config, stream, allow_unicode=True, sort_keys=False)
        fields["api_key"].delete(0, tk.END)
        messagebox.showinfo("已保存", "MiMo 本地配置已保存。密钥不会进入 Git 提交。")
        window.destroy()

    tk.Button(window, text="保存本地配置", command=save, width=24).grid(row=7, column=0, pady=5)
    fields["api_key"].focus_set()
    window.mainloop()


if __name__ == "__main__":
    main()
