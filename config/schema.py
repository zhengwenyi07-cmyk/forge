"""
config/schema.py

配置文件加载与校验。把 config/default.yaml 解析成类型安全的 dataclass。

支持：
- 环境变量展开：${VAR} 语法
- 多层配置合并：default.yaml < 用户指定 yaml < CLI 参数
- 缺失必填项时给出清晰错误信息

配置优先级（从低到高）：
  1. AppConfig 的默认值（代码硬编码）
  2. config/default.yaml（文件配置）
  3. 用户指定的 -c config.yaml（CLI 参数）
  4. CLI 覆盖参数（--model / --provider / --max-steps）

数据类型流：
  config/default.yaml (纯文本)
    → _expand_env() 展开 ${VAR} 为环境变量值
    → yaml.safe_load() 解析为 dict
    → _parse() 递归映射为 AppConfig dataclass
    → merge_cli_overrides() 应用 CLI 参数覆盖
    → AppConfig 实例 → entry 层使用

  注意：同名的 config/schema.py 目录下的 __init__.py 不存在，
  此文件可直接 import: from config.schema import load_config
"""

from __future__ import annotations

# os.environ：_expand_env() 用 os.environ.get(var, "") 读取环境变量
import os
# re.compile：编译 ${VAR} 正则，用于环境变量占位符替换
import re
# dataclass + field：所有配置类都是 dataclass，field(default_factory=...) 处理嵌套默认值
from dataclasses import dataclass, field
# Path：跨平台路径处理，__file__ 获取本文件所在目录，parent / "default.yaml" 拼接配置路径
from pathlib import Path
# Any：_parse() 接收 dict[str, Any]，因为 yaml.safe_load 的返回类型不确定
from typing import Any

# yaml：读取 YAML 格式的配置文件（懒加载，无此依赖时也可 import）
# 如果未安装 PyYAML，load_config() 会给出清晰提示


# ===========================================================================
# 配置 dataclass
# 所有字段都有默认值——不提供配置文件也能全默认运行。
# 类型安全：IDE 补全、mypy 静态检查都生效。
# 对应 resume 中"配置文件一行切换"的底层数据结构支持。
# ===========================================================================

@dataclass
class LLMConfig:
    """
    LLM 提供商配置。
    对应 config/default.yaml 的 llm 节。
    """
    provider: str = "anthropic"         # 模型提供商，支持：anthropic | openai | deepseek | groq | ollama
    model: str = "claude-sonnet-4-5"    # 模型名，如 deepseek-v4-flash / gpt-4o / claude-sonnet-4-5
    api_key: str = ""                    # API Key。可在 yaml 中用 ${ENV_VAR} 占位符（如 ${DEEPSEEK_API_KEY}）
    base_url: str = ""                   # OpenAI-compatible 服务的基础 URL，anthropic 留空
    max_tokens: int = 4096               # 每次 LLM 调用的最大输出 token 数
    headers: dict[str, str] | None = None  # 自定义 HTTP 请求头（如 MiMo Token Plan 的 api-key）


@dataclass
class AgentCfg:
    """
    Agent 运行时核心配置。
    对应 config/default.yaml 的 agent 节。
    """
    max_steps: int = 40         # 单轮运行最大步数，超出熔断
    budget_tokens: int = 80_000 # 总 token 预算（含 15% 预留），传给 TokenBudget
    log_dir: str = "./logs"     # EventLog JSONL 文件输出目录


@dataclass
class ShellToolConfig:
    """
    Shell 工具的安全配置。
    对应 config/default.yaml 的 tools.shell 节。
    """
    timeout: int = 30           # 单条 shell 命令超时秒数
    max_output_tokens: int = 8_000  # shell 输出截断的 token 数上限（实际用字符估算）


@dataclass
class FileToolConfig:
    """
    文件查看工具的展示配置。
    对应 config/default.yaml 的 tools.file 节。
    """
    max_view_lines: int = 100   # file_view 每次显示的最大行数


@dataclass
class ToolsConfig:
    """工具配置聚合。各字段的默认 factory 保证嵌套初始化为默认值。"""
    shell: ShellToolConfig = field(default_factory=ShellToolConfig)
    file: FileToolConfig = field(default_factory=FileToolConfig)


@dataclass
class ContextConfig:
    """
    上下文管理配置。
    对应 config/default.yaml 的 context 节。
    """
    repo_map_budget: int = 8_000   # repo-map 的最大 token 预算，传给 RepoMap.build(budget=...)
    history_window: int = 20       # 保留最近 N 轮对话历史（条数维度，传给 ConversationHistory）


@dataclass
class AppConfig:
    """
    顶层应用配置（所有配置的根容器）。
    对应 config/default.yaml 的完整结构。
    """
    # 各字段使用 default_factory 而非直接 LLMConfig()，因为 dataclass 的可变默认值
    # 规则要求使用 factory，否则多个实例会共享同一个可变的嵌套对象
    llm: LLMConfig = field(default_factory=LLMConfig)
    agent: AgentCfg = field(default_factory=AgentCfg)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    context: ContextConfig = field(default_factory=ContextConfig)


# ===========================================================================
# 加载函数
# 流程：文件路径 → 读取 → 环境变量展开 → YAML 解析 → dataclass → CLI 覆盖
# ===========================================================================

# 环境变量占位符正则：匹配 ${VAR_NAME}。
# 只匹配字母、数字、下划线（\w+），防止 ${...} 中包含路径或特殊字符。
_ENV_RE = re.compile(r"\$\{(\w+)\}")


def _expand_env(text: str) -> str:
    """
    展开 ${VAR} 形式的环境变量占位符。
    不存在的环境变量被替换为空字符串（不会报错）。

    处理流程：
    1. re.sub(_ENV_RE, replace, text) 匹配所有 ${VAR} 模式
    2. os.environ.get(var, "") 取环境变量值，不存在返回空字符串

    Args:
        text: 可能包含 ${VAR} 占位符的原始文本

    Returns:
        环境变量替换后的文本
    """
    def replace(m: re.Match) -> str:
        return os.environ.get(m.group(1), "")
    return _ENV_RE.sub(replace, text)


def load_config(path: str | Path | None = None) -> AppConfig:
    """
    加载配置文件，返回 AppConfig。

    查找顺序：
    1. 显式传入 path → 如果文件存在则直接加载
    2. path=None → 自动查找 config/default.yaml（当前目录 → 本文件所在目录）
    3. 所有路径都不存在 → 返回全默认的 AppConfig（不报错，所有字段用默认值）

    Args:
        path: YAML 文件路径，None 时自动查找 config/default.yaml

    Returns:
        AppConfig 实例，所有字段都有默认值
    """
    if path is None:
        # 自动查找：当前目录 → 项目根目录
        candidates = [
            Path("config/mimo.local.yaml"),
            Path("config/default.yaml"),
            Path(__file__).parent / "default.yaml",
        ]
        for p in candidates:
            if p.exists():
                path = p
                break
        else:
            return AppConfig()   # 找不到配置文件，用全默认值

    config_path = Path(path)
    if not config_path.exists():
        return AppConfig()
    raw = config_path.read_text(encoding="utf-8")
    raw = _expand_env(raw)
    try:
        import yaml as _yaml
    except ImportError:
        try:
            from ruamel.yaml import YAML
            _yaml = YAML(typ="safe")
            _yaml_ruamel = True
        except ImportError:
            raise ImportError(
                "PyYAML is required to load config files. Install: pip install pyyaml"
            )
        else:
            data: dict[str, Any] = _yaml.load(raw) or {}
    else:
        data: dict[str, Any] = _yaml.safe_load(raw) or {}
    return _parse(data)


def _parse(data: dict[str, Any]) -> AppConfig:
    """
    把 yaml dict 解析为 AppConfig。

    对每个顶层键（llm / agent / tools / context）：
    1. data.get(key, {}) 取对应的子 dict
    2. .get(sub_key, default) 取每个字段值，缺失时使用代码默认值
    3. 必要时做类型转换（int(...) 确保 yaml 中写字符串也能正确处理）

    Args:
        data: yaml.safe_load 返回的 dict

    Returns:
        解析完成的 AppConfig 实例
    """
    llm_raw = data.get("llm", {})
    agent_raw = data.get("agent", {})
    tools_raw = data.get("tools", {})
    context_raw = data.get("context", {})

    llm = LLMConfig(
        provider=llm_raw.get("provider", "anthropic"),
        model=llm_raw.get("model", "claude-sonnet-4-5"),
        api_key=llm_raw.get("api_key", ""),
        base_url=llm_raw.get("base_url", "") or "",
        max_tokens=int(llm_raw.get("max_tokens", 4096)),
        headers=llm_raw.get("headers"),
    )

    agent = AgentCfg(
        max_steps=int(agent_raw.get("max_steps", 40)),
        budget_tokens=int(agent_raw.get("budget_tokens", 80_000)),
        log_dir=agent_raw.get("log_dir", "./logs"),
    )

    shell_raw = tools_raw.get("shell", {})
    file_raw = tools_raw.get("file", {})
    tools = ToolsConfig(
        shell=ShellToolConfig(
            timeout=int(shell_raw.get("timeout", 30)),
            max_output_tokens=int(shell_raw.get("max_output_tokens", 8_000)),
        ),
        file=FileToolConfig(
            max_view_lines=int(file_raw.get("max_view_lines", 100)),
        ),
    )

    context = ContextConfig(
        repo_map_budget=int(context_raw.get("repo_map_budget", 8_000)),
        history_window=int(context_raw.get("history_window", 20)),
    )

    return AppConfig(llm=llm, agent=agent, tools=tools, context=context)


def merge_cli_overrides(
    config: AppConfig,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    max_steps: int | None = None,
) -> AppConfig:
    """
    把 CLI 参数覆盖到已加载的 config 上。
    CLI 参数优先级最高（高于 yaml 配置，低于直接修改返回值的调用方）。

    仅当参数值非 None 时才覆盖（不覆盖已有的值）。
    这样 CLI 上没有传的参数不会把配置文件中的值重置为默认。

    Args:
        config:   从 load_config() 加载的基础配置
        provider: --provider CLI 参数
        model:    --model CLI 参数
        api_key:  --api-key CLI 参数
        max_steps: --max-steps CLI 参数

    Returns:
        被覆盖后的配置（直接修改了传入的 config 对象并返回，非副本）
    """
    if provider:
        config.llm.provider = provider
    if model:
        config.llm.model = model
    if api_key:
        config.llm.api_key = api_key
    if max_steps is not None:
        config.agent.max_steps = max_steps
    return config
