"""
multi_agent/blacklist.py

子 agent 工具黑白名单。

设计原则：
- 所有子 agent 共享一份黑名单（绝对不能用的工具）
- explore/review 子 agent 各有自己的白名单（只允许的工具）
- fork 子 agent 用父 agent 全部工具 - 黑名单
"""

# ---------------------------------------------------------------------------
# 全局黑名单：所有子 agent 都不能用的工具
# ---------------------------------------------------------------------------

SUB_AGENT_BLACKLIST: frozenset[str] = frozenset({
    "task",          # 不能派孙 agent（强制仅一层深度）
    # "ask_user",    # 不能问用户问题（预留，当前项目无此工具）
    # "plan_mode",   # 不能切换规划模式（预留，当前项目无此工具）
    # "stop_task",   # 不能停止兄弟任务（预留，当前项目无此工具）
})

# ---------------------------------------------------------------------------
# explore 子 agent 白名单 — 纯只读代码探索
# ---------------------------------------------------------------------------

EXPLORE_TOOL_NAMES: frozenset[str] = frozenset({
    "search_text",
    "find_files",
    "find_symbol",
    "file_read",
    "file_view",
})

# ---------------------------------------------------------------------------
# review 子 agent 白名单 — 代码审查（只读 + 可运行 linter / 测试 / git 查看）
# ---------------------------------------------------------------------------

REVIEW_TOOL_NAMES: frozenset[str] = frozenset({
    "search_text",
    "find_files",
    "find_symbol",
    "file_read",
    "file_view",
    "shell",          # 可运行 linter 和 pytest
    "git_status",
    "git_diff",
})
