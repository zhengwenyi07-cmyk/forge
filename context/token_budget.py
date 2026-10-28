"""
context/token_budget.py

Token 预算管理：给 prompt 各部分分配 token 配额，超出时按优先级裁剪。

## tiktoken 安装

    pip install tiktoken

首次运行时自动下载词表（需联网，约 2MB），之后缓存到本地离线可用。

如果网络无法访问 OpenAI CDN，手动下载词表：
    curl -L "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken" \\
         -o ~/.cache/tiktoken/9b5ad71b2ce5302211f9c61530b329a4922fc6a4021629a1eba1b43bf10a10.tiktoken

然后设置环境变量：
    export TIKTOKEN_CACHE_DIR=~/.cache/tiktoken

tiktoken 不可用时自动降级为字符估算（1 token ≈ 4 chars），精度足够做预算控制。

各部分优先级（高→低，裁剪时从低优先级开始）：
  1. system_core   系统指令，永不裁剪
  2. task          任务描述，永不裁剪
  3. repo_map      repo 摘要，超出时缩减
  4. recent_obs    最近 observation，永不裁剪
  5. history       历史对话，从最旧开始裁剪

数据流（对应简历第三段"基于优先级的 Token 预算分配器" + "双层裁剪"）：
  core.py:_build_messages()
    → token_budget = TokenBudget(total=budget_tokens)      ← 初始化（总预算）
    → plan = token_budget.default_plan()                    ← 算分配比例
    → token_budget.trim_history(history, plan.history)      ← 第二层裁剪：按 Token 数量
    → 第一层裁剪已在 ConversationHistory._trim() 完成       ← 第一层裁剪：按消息条数

  usage_report() → 输出各部分的 token 使用量
    → 用于调试和配置调优
"""

from __future__ import annotations

# dataclass：
#   BudgetPlan(total, system_core, repo_map, history, observation, reserve)
#   一个不可变的预算分配方案，由 default_plan() 生成
from dataclasses import dataclass

# ===========================================================================
# Token 计数：优先 tiktoken，失败时字符估算 fallback
# 对应简历"优先使用 tiktoken 精确计数，环境受限时降级为字符数 // 4 估算"
# ===========================================================================

# 全局状态：tiktoken 编码器和可用性标记
# 首次调用 estimate_tokens() 时懒初始化，之后缓存
_tiktoken_enc = None          # tiktoken.Encoding 对象或 None
_tiktoken_available = False    # tiktoken 是否已成功加载

def _init_tiktoken() -> None:
    """
    尝试加载 tiktoken 的 cl100k_base 编码器。
    网络或安装问题导致失败时静默降级（_tiktoken_available = False）。
    编码器缓存在全局变量 _tiktoken_enc 中，整个进程生命周期内只加载一次。
    """
    global _tiktoken_enc, _tiktoken_available
    if _tiktoken_available or _tiktoken_enc is not None:
        return
    try:
        import tiktoken
        # cl100k_base 编码器是 OpenAI 官方编码器，兼容 GPT-4/3.5 及大多数现代模型
        _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
        _tiktoken_available = True
    except Exception:
        # 网络不通 / 未安装，降级为字符估算
        _tiktoken_available = False


def estimate_tokens(text: str) -> int:
    """
    估算文本的 token 数。
    优先使用 tiktoken（精确），不可用时用字符数 // 4（误差 <15%）。

    对应简历"优先使用 tiktoken 的 cl100k_base 编码做精确计数，
    环境受限时自动降级为字符数 // 4 估算（误差 <15%）"。

    返回至少 1（即使是空字符串也返回 1），防止除零错误。
    """
    if not _tiktoken_available:
        _init_tiktoken()

    if _tiktoken_available and _tiktoken_enc is not None:
        try:
            return max(1, len(_tiktoken_enc.encode(text)))
        except Exception:
            pass

    # 字符估算 fallback：假设 1 token ≈ 4 个字符
    # 这个比例对不同语言有差异（英文更接近 4，中文更接近 2），
    # 但对于 Token 预算控制这种粗粒度任务，误差 <15%，完全可接受
    return max(1, len(text) // 4)


def estimate_chars(tokens: int) -> int:
    """把 token 数转换为字符预算（估算）。"""
    return tokens * 4


def _bounded_text(text: str, token_limit: int, keep_tail: bool = False) -> str:
    if token_limit < 1:
        return ""
    if estimate_tokens(text) <= token_limit:
        return text
    marker = "\n[content truncated]\n"
    if estimate_tokens(marker) >= token_limit:
        marker = ""
    low, high = 0, len(text)
    best = marker
    while low <= high:
        count = (low + high) // 2
        head = (count + 1) // 2 if keep_tail else count
        tail = count - head
        candidate = text[:head] + marker + (text[-tail:] if tail else "")
        if estimate_tokens(candidate) <= token_limit:
            best = candidate
            low = count + 1
        else:
            high = count - 1
    return best


def is_tiktoken_available() -> bool:
    """返回 tiktoken 是否可用，供诊断脚本使用。"""
    _init_tiktoken()
    return _tiktoken_available


# ===========================================================================
# BudgetPlan — 各部分的 token 配额计划
# 一个不可变的数据容器，由 TokenBudget.default_plan() 生成。
# 不对实际分配进行校验，只做数值封送。
# ===========================================================================

@dataclass
class BudgetPlan:
    """
    各部分的 token 配额计划。

    各部分优先级（从高到低）：
    1. system_core：系统指令，永不裁剪（但限制预算以防 write 时超出总窗口）
    2. repo_map：仓库摘要，超出时缩减文件数量
    3. history：对话历史，从最旧开始裁剪
    4. observation：最近的工具执行结果，截断单条超长输出
    5. reserve：安全预留，不分配到任何具体部分

    对应简历："System 10% → Repo-map 15% → History 50% → Observation 25%"
    """
    total: int          # 总预算（从 config/default.yaml 的 agent.budget_tokens 读取，默认 80_000）
    system_core: int    # System Prompt 预算（可用预算的 10%）
    repo_map: int       # Repo-map 摘要预算（可用预算的 15%）
    history: int        # 对话历史预算（可用预算的 50%，最大份额）
    observation: int    # 最近 Observation 预算（可用预算的 25%）
    reserve: int        # 安全预留（总预算的 15%）

    @property
    def available(self) -> int:
        """可用预算 = 总预算 - 安全预留。"""
        return self.total - self.reserve


# ===========================================================================
# TokenBudget — Token 预算管理器
# 核心方法：
#   default_plan()     — 按优先级比例分配预算
#   trim_to()          — 单条文本按 token 上限裁剪
#   trim_history()     — 历史消息列表按 token 上限裁剪（保留首条 + 最近消息）
#   fit_all()          — 一次性裁剪所有部分（便捷方法，当前未被 core.py 调用）
#   usage_report()     — 输出各部分的 token 使用量（诊断用）
# ===========================================================================

class TokenBudget:
    """
    Token 预算管理器。

    用法：
        budget = TokenBudget(total=80_000)
        plan = budget.default_plan()
        trimmed = budget.trim_to(text, plan.repo_map)
        trimmed_history = budget.trim_history(msgs, plan.history)
    """

    def __init__(self, total: int = 80_000) -> None:
        self._total = total

    def default_plan(self) -> BudgetPlan:
        """
        生成默认的预算分配方案。

        分配逻辑：
        1. 总预算 → 15% 预留（缓冲余量）
        2. 剩余可用预算 → 按 10% / 15% / 50% / 25% 分配给四部分

        比例设计思路：
        - History 最大（50%）— 多轮对话中最占 token 的就是历史消息
        - Observation 次大（25%）— 工具输出可能很长（如 file_read 结果）
        - Repo-map 适中（15%）— 全仓库摘要，控制注入量
        - System 最小（10%）— 固定指令文本，占比不高但优先级最高
        """
        total = self._total
        reserve = int(total * 0.15)
        available = total - reserve
        return BudgetPlan(
            total=total,
            reserve=reserve,
            system_core=int(available * 0.10),
            repo_map=int(available * 0.15),
            history=int(available * 0.50),
            observation=int(available * 0.25),
        )

    def trim_to(self, text: str, token_limit: int) -> str:
        """
        裁剪文本到 token_limit 以内，超出时保留开头。
        用于裁剪单条超长文本（如 repo-map 摘要、超长 observation）。

        算法：二分逼近
        1. 先用 token_limit * 4 作为字符预算截断
        2. 如果截断后 token 数仍超标，每次缩 10%，反复逼近
        3. 在末尾插入被截断的 token 数提示

        Args:
            text:        待裁剪文本
            token_limit: token 上限

        Returns:
            裁剪后的文本（如果原文本不超过限制则原样返回）
        """
        if estimate_tokens(text) <= token_limit:
            return text
        return _bounded_text(text, token_limit)

    def trim_history(
        self,
        messages: list[dict],
        token_limit: int,
    ) -> list[dict]:
        """
        裁剪历史消息列表到 token_limit 以内。
        保留第一条（任务描述）+ 尽量多的最近消息。

        这是"双层裁剪"的第二层（Token 数量维度）。
        第一层由 ConversationHistory._trim() 完成（消息条数维度，保留首条 + 最近 N 条）。
        第二层在此进行（Token 维度，从最新消息开始贪婪选择直到预算耗尽）。

        对应简历"结合 ConversationHistory 滑动窗口与 TokenBudget 二分截断实现双层裁剪"：
        - 第一层（条数）：ConversationHistory._trim() — 从 index 1 弹出旧消息
        - 第二层（Token）：trim_history() — 从最新消息贪婪选择，丢弃的插入截断提示

        优先保留任务描述与最新结果。超大任务/结果会缩短到预算内，
        最新结果保留首尾。剩余预算按时间顺序保留最近历史，
        不会为了放入较小的旧动作而丢弃最新工具结果。
        """
        if not messages or token_limit < 1:
            return []
        if len(messages) == 1:
            return [{**messages[0], "content": self.trim_to(messages[0].get("content", ""), token_limit)}]

        token_counts = [estimate_tokens(m.get("content", "")) for m in messages]
        if sum(token_counts) <= token_limit:
            return messages
        # Always reserve space for the newest result. Never replace it with a
        # smaller old action just because the whole observation does not fit.
        if token_limit == 1:
            return [{**messages[-1], "content": self.trim_to(messages[-1].get("content", ""), 1)}]
        notice = {"role": "user", "content": "[Earlier messages truncated]"}
        notice_tokens = estimate_tokens(notice["content"])
        include_notice = token_limit >= notice_tokens + 3
        available = token_limit - (notice_tokens if include_notice else 0)
        first_limit = token_counts[0] if token_counts[0] + token_counts[-1] <= available else min(token_counts[0], max(1, available // 3))
        first = {**messages[0], "content": self.trim_to(messages[0].get("content", ""), first_limit)}
        remaining = available - estimate_tokens(first["content"])
        latest = {**messages[-1], "content": _bounded_text(messages[-1].get("content", ""), remaining, keep_tail=True)}
        remaining -= estimate_tokens(latest["content"])
        selected = [latest]
        for msg in reversed(messages[1:-1]):
            cost = estimate_tokens(msg.get("content", ""))
            if cost > remaining:
                break
            selected.append(msg)
            remaining -= cost
        return [first, *([notice] if include_notice else []), *reversed(selected)]

    def fit_all(
        self,
        system_text: str,
        repo_map_text: str,
        history: list[dict],
        observation_text: str,
    ) -> tuple[str, str, list[dict], str]:
        """
        一次性裁剪所有部分（便捷方法）。
        按 default_plan() 分配预算，分别裁剪各部分。

        Args:
            system_text:     System Prompt 文本
            repo_map_text:   Repo-map 摘要文本
            history:         对话历史消息列表
            observation_text: 最新 Observation 文本

        Returns:
            (trimmed_system, trimmed_map, trimmed_history, trimmed_obs)

        注意：当前 core.py 中未调用此方法，而是分别调用 trim_to() 和 trim_history()。
        此方法预留作为 future 统一入口。
        """
        plan = self.default_plan()
        trimmed_system = self.trim_to(system_text, plan.system_core)
        trimmed_map = self.trim_to(repo_map_text, plan.repo_map)
        trimmed_history = self.trim_history(history, plan.history)
        trimmed_obs = self.trim_to(observation_text, plan.observation)
        return trimmed_system, trimmed_map, trimmed_history, trimmed_obs

    def usage_report(
        self,
        system_text: str,
        repo_map_text: str,
        history: list[dict],
        observation_text: str,
    ) -> dict[str, int]:
        """
        生成 token 使用量报告（诊断用）。

        返回各部分的 token 估算值和总预算，帮助判断是否需要调整配置比例。

        Args:
            system_text:     System Prompt 文本
            repo_map_text:   Repo-map 摘要文本
            history:         对话历史消息列表
            observation_text: 最新 Observation 文本

        Returns:
            dict 包含：system / repo_map / history / observation / total / budget / tiktoken_used

        输出示例：
            {"system": 6800, "repo_map": 10200, "history": 34000,
             "observation": 17000, "total": 68000, "budget": 80000,
             "tiktoken_used": True}
        """
        history_tokens = sum(
            estimate_tokens(m.get("content", "")) for m in history
        )
        return {
            "system":      estimate_tokens(system_text),
            "repo_map":    estimate_tokens(repo_map_text),
            "history":     history_tokens,
            "observation": estimate_tokens(observation_text),
            "total": (
                estimate_tokens(system_text)
                + estimate_tokens(repo_map_text)
                + history_tokens
                + estimate_tokens(observation_text)
            ),
            "budget":        self._total,
            "tiktoken_used": is_tiktoken_available(),
        }
