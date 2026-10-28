"""
agent/prompt.py

System prompt 模板管理。

职责：
- 维护 agent 的 system prompt 模板
- 根据运行时信息（工具列表、repo 概况）渲染最终 prompt
- 提供 Reflection prompt 模板

设计原则：
- prompt 集中在这里，修改 prompt 不需要改 core.py
- 模板用 str.format() 而不是 jinja2，减少依赖
- 每个 prompt 都有对应的函数，便于测试和调整

数据流：
  core.py:_build_messages()
    → build_system_prompt(tools, repo_summary)   → 渲染 system prompt（注入 repo-map）
    → build_task_prompt(description, issue_url)   → 渲染第一条 user 消息
  core.py:run()（Reflection 触发时）
    → reflection_test_failed() / reflection_no_edit(n)   → 注入反思 prompt 到 history
"""

# PEP 604 前向引用支持（Python 3.7+ 兼容语法）
from __future__ import annotations

# LLMToolSchema：工具的 JSON Schema 描述（name / description / parameters）
# build_system_prompt 接收 list[LLMToolSchema] 并格式化为易读的列表文本
from llm.base import LLMToolSchema


# ===========================================================================
# System prompt
# 这是 Agent 收到的"第一性指令"——在每个 LLM 请求中作为 system message 发送。
# 包含三部分：
#   1. 角色定义 + 工作流程（固定文本）
#   2. Repository 节（动态：repo_path + repo_map 摘要）→ 对应简历第二段"在 ReAct
#      循环的消息组装阶段注入 System Prompt"
#   3. Available tools 节（动态：由 ToolRegistry.get_schemas() 传入）
# ===========================================================================

_SYSTEM_TEMPLATE = """\
You are an autonomous coding agent. Use the available tools to accomplish the task.

## Guidelines
- Think step by step before each action (use the thought field)
- If you are stuck after several attempts, reflect on your approach and try differently
- Make the smallest change that fixes the problem
- Use file_edit for unique replacements or apply_patch for unified diffs. file_write is only for new or small files.
- After the last edit, use the test tool with a targeted path or explicit command. Report failures accurately.
- Modification tasks require a new diff and a targeted test attempt before finish is accepted.
- Commands run in Linux bash. Use repository-relative paths.
- When done, call finish. If you truly cannot solve it, call give_up with an explanation

## Repository
Path: {repo_path}
{repo_summary}

## Available tools
{tool_descriptions}
"""

# repo_map 不可用时的 fallback 文本。
# 当 RepoMap.build() 返回空或未生成时使用此占位符。
# 提示 LLM 使用 find_files 和 file_read 自行探索代码库。
_NO_REPO_SUMMARY = "(Repository summary not yet available — use find_files and file_read to explore)"


def build_system_prompt(
    repo_path: str,
    tools: list[LLMToolSchema],
    repo_summary: str | None = None,
) -> str:
    """
    渲染完整的 system prompt。

    调用方：core.py:_build_messages()（每个 step 调用一次）。
    repo_summary 由 RepoMap.build() 生成（tree-sitter 提取的仓库符号摘要）。

    Args:
        repo_path:    repo 根目录路径（显示在 ## Repository 节中）
        tools:        已注册工具的 schema 列表（格式化后填入 ## Available tools）
        repo_summary: repo-map 生成的摘要字符串（注入 {repo_summary} 占位符）。
                      None 时使用 _NO_REPO_SUMMARY fallback 文本

    Returns:
        渲染好的 system prompt 字符串
    """
    # 把 LLMToolSchema 列表格式化为人类可读的 markdown 列表
    tool_descriptions = _format_tool_descriptions(tools)
    # repo_map 为空时使用 fallback 文本，引导 LLM 自行探索
    summary = repo_summary or _NO_REPO_SUMMARY

    # 用 str.format() 替换 _SYSTEM_TEMPLATE 中的三个占位符
    # 选择 str.format() 而非 jinja2：零外部依赖，构建一步到位
    return _SYSTEM_TEMPLATE.format(
        repo_path=repo_path,
        repo_summary=summary,
        tool_descriptions=tool_descriptions,
    )


def _format_tool_descriptions(tools: list[LLMToolSchema]) -> str:
    """把工具列表格式化为易读的描述块。

    输出格式：
        - **shell**: Execute a shell command and return its output...
        - **file_read**: Read the contents of a file...

    每个工具对应一行，工具名用 markdown 粗体标记，后接 description。
    这些工具名必须与 LLM function calling 时传入的 tools 参数中的 name 一致。
    """
    if not tools:
        return "(no tools available)"
    lines = []
    for tool in tools:
        lines.append(f"- **{tool.name}**: {tool.description}")
    return "\n".join(lines)


# ===========================================================================
# Reflection prompts
# 当 Agent 陷入低效行为时，core.py 将这些 prompt 注入对话历史（作为 user 消息）。
# 这不是简单的"重试"，而是引导性元认知（guided metacognition）——迫使 LLM 从
# 系统 1 思维（快速直觉）切换到系统 2 思维（慢速分析）。
# 设计参考：Daniel Kahneman 的"思考，快与慢"中的双系统理论。
# ===========================================================================

# Reflection A：测试失败时触发（core.py:250-261）。
# 注入时机：LLM 调用了 test / pytest 工具且 observation.status != SUCCESS。
# 引导性提问（不是"错了重来"）：
#   1. 认真读错误信息，根因是什么？
#   2. 最近一次编辑是否正确？是否引入了新 bug？
#   3. 是否需要更多上下文才能继续？
# 对应简历"测试失败时自动注入错误分析提示引导 LLM 反思根因"。
REFLECTION_TEST_FAILED = """\
[REFLECTION] The tests just failed. Before your next action, consider:
1. Read the full error message above carefully — what is the root cause?
2. Is your last edit correct? Did it introduce a new bug?
3. Do you need to look at more context before editing again?

Be specific about what you will do differently. What is your next action?\
"""

# Reflection B：连续 N 步无文件编辑时触发（core.py:264-273）。
# 注入时机：steps_without_edit >= reflection_no_edit_steps（默认 6）。
# 防止 Agent 陷入"无尽探索"——一直 find_files → file_read → search_text 但不修改。
# 引导性提问：
#   1. 是否有足够上下文了？有 → 动手改
#   2. 没有 → 精确说出还缺什么，一步拿到，然后改
# 对应简历"连续 N 步无文件修改时触发方向反思"。
REFLECTION_NO_EDIT = """\
[REFLECTION] You have taken {n} steps without editing any file.
You may be stuck in an exploration loop. Consider:
1. Do you have enough context to make a change now?
2. If yes — make the edit
3. If no — identify exactly what you still need, get it in one targeted step, then edit

What specific action will move the task forward?\
"""

# Reflection C：死循环检测时触发（core.py:164-174）。
# 注入时机：连续 N 步 (tool_name, params) 元组完全一致。
# 注：此 prompt 当前由 core.py 中的 _is_looping() 检测到死循环后直接 GIVE_UP，
# 不使用此 prompt。此模板为预留（后续可用作"最后警告"而非直接熔断）。
REFLECTION_LOOP_DETECTED = """\
[REFLECTION] You have repeated the same action {n} times in a row.
This suggests you are stuck. Stop and reconsider:
1. What are you trying to achieve with this action?
2. Why isn't it working?
3. What completely different approach could you try?

Do not repeat the same action again.\
"""


def reflection_test_failed() -> str:
    """返回测试失败的 Reflection prompt 文本（无参数，纯字符串）。"""
    return REFLECTION_TEST_FAILED


def reflection_no_edit(n: int) -> str:
    """
    返回"连续 N 步无编辑"的 Reflection prompt 文本。

    Args:
        n: 连续无编辑的步数，注入 prompt 中的 {n} 占位符

    Returns:
        渲染好的 Reflection prompt 字符串
    """
    return REFLECTION_NO_EDIT.format(n=n)


def reflection_loop_detected(n: int) -> str:
    """
    返回死循环检测的 Reflection prompt 文本（当前预留）。

    Args:
        n: 连续重复的步数，注入 prompt 中的 {n} 占位符

    Returns:
        渲染好的 Reflection prompt 字符串
    """
    return REFLECTION_LOOP_DETECTED.format(n=n)


# ===========================================================================
# Task prompt（用户消息，描述任务）
# 在单次 run 模式下作为对话历史的第一条 user 消息。
# 在 chat 模式下每轮用户输入会作为一个新的 Task 传给 Agent.run()，
# 但历史中此 prompt 位于 index 0（永久保留，不被滑动窗口丢弃）。
# ===========================================================================

# 任务描述模板。
# 三个占位符：
#   {repo_path}   → 目标 repo 的本地路径
#   {description} → 用户输入的任务描述（经 .strip() 处理）
#   {issue_section} → GitHub Issue URL 块（可选，由 build_task_prompt 控制是否填充）
_TASK_TEMPLATE = """\
Task for repository at {repo_path}:

## Task
{description}
{issue_section}
## Instructions
- Use the tools available to complete the task
- When done, call finish with a summary\
"""

# GitHub Issue URL 嵌入模板。
# 当 Task.issue_url 非空时（GitHub Issue 自动修复模式），此块被嵌入 _TASK_TEMPLATE。
# 块开头有换行，确保嵌入后格式正确。
_ISSUE_SECTION_TEMPLATE = """
## GitHub Issue
URL: {issue_url}
"""


def build_task_prompt(
    description: str,
    repo_path: str,
    issue_url: str | None = None,
) -> str:
    """
    构建任务描述的用户消息（对话的第一条 user 消息）。

    调用方：
    - core.py:run() 第 124-127 行（单次 run 模式：CLI run / GitHub Issue）
    - ChatSession 每轮不会重复调用此函数（chat 模式复用共享 history）

    Args:
        description: 任务描述（自然语言），例如 "修复 tests/test_parser.py 中的失败测试"
        repo_path:   repo 根目录
        issue_url:   GitHub issue URL（可选），由 entry/github_issue.py 传入

    Returns:
        格式化的任务 prompt 字符串
    """
    issue_section = ""
    if issue_url:
        issue_section = _ISSUE_SECTION_TEMPLATE.format(issue_url=issue_url)

    return _TASK_TEMPLATE.format(
        repo_path=repo_path,
        description=description.strip(),
        issue_section=issue_section,
    )
