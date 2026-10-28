"""
entry/github_issue.py

GitHub Issue 自动修复入口。

流程：
1. 拉取 Issue 标题 + body 作为任务描述
2. Clone 或使用已有的本地 repo
3. 在新分支上运行 agent
4. agent 完成后创建 PR（可选）

用法：
    python -m entry.github_issue \
        --repo owner/repo \
        --issue 42 \
        --local-path /tmp/myrepo

依赖：
    pip install PyGithub gitpython

完整流程（对应 run_on_issue() 中的 7 步）：
  1. fetch_issue()  → GitHub API 拉取 Issue 标题 + 描述
  2. clone_repo()   → 如果本地没有则 clone
  3. create_branch()→ 创建 agent/fix-issue-{number}-{timestamp} 分支
  4. Agent.run()    → Agent 在本地仓库上执行任务
  5. push_branch()  → 推送分支到 GitHub
  6. create_pull_request() → 自动创建 PR
  7. 返回 0（成功）/ 1（失败）
"""

from __future__ import annotations

# logging：运行日志（clone、agent run、push 各阶段的状态）
import logging
# os：GITHUB_TOKEN 环境变量读取
import os
# subprocess：git clone / checkout / push 等 git 命令的底层执行
import subprocess
# sys：sys.path 路径注入 + sys.exit 退出码
import sys
# time：分支名中的时间戳 int(time.time()) 确保分支名唯一
import time
# Path：本地 repo 路径检查和跨平台路径操作
from pathlib import Path

# 项目根目录，用于模块导入
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# click CLI 框架。@click.command() + @click.option 定义命令行参数
import click

logger = logging.getLogger(__name__)


# ===========================================================================
# GitHub 操作
# 通过 PyGithub SDK 调用 GitHub REST API。
# 需要环境变量 GITHUB_TOKEN（Personal Access Token，需 repo 权限）。
# ===========================================================================

def _get_github_client():
    """
    初始化 PyGithub 客户端，从环境变量读 token。

    需要 GITHUB_TOKEN 环境变量（GitHub Personal Access Token，需 repo 权限）。
    Token 创建地址：https://github.com/settings/tokens
    """
    try:
        from github import Github, Auth
    except ImportError:
        raise ImportError("PyGithub not installed. Run: pip install PyGithub")

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise ValueError(
            "GITHUB_TOKEN environment variable is not set.\n"
            "Create a token at https://github.com/settings/tokens"
        )
    return Github(auth=Auth.Token(token))


def fetch_issue(repo_name: str, issue_number: int) -> tuple[str, str, str]:
    """
    拉取 GitHub Issue 内容。

    通过 PyGithub SDK 调用 GitHub REST API 获取 Issue 信息。
    Issue 的 title + body 会被拼接到任务描述中传给 Agent。

    Returns:
        (title, body, html_url)
        - title: Issue 标题（如 "Fix parser crash on empty input"）
        - body:  Issue 描述内容（Markdown 格式，可能为空）
        - html_url: Issue 的 GitHub URL（用于 build_task_prompt 注入 issue_url）
    """
    gh = _get_github_client()
    repo = gh.get_repo(repo_name)
    issue = repo.get_issue(issue_number)
    return issue.title, issue.body or "", issue.html_url


def create_pull_request(
    repo_name: str,
    branch: str,
    title: str,
    body: str,
    base: str = "main",
) -> str:
    """
    创建 PR，返回 PR URL。

    Args:
        repo_name: "owner/repo" 格式，如 "muk610648-design/forge-agent"
        branch:    源分支名称。Agent 在此分支上做了修改，例如 "agent/fix-issue-42-1686543200"
        title:     PR 标题，自动生成："[Agent] Fix issue #{number}: {title}"
        body:      PR 描述，自动生成：包含 "Fixes #{number}" + agent 的 result.summary
        base:      目标分支，默认 main。如果 main 不存在则尝试 master

    Returns:
        PR 的 HTML URL，如 "https://github.com/owner/repo/pull/99"

    注意：
    - 调用前需要已经 push_branch() 推送了分支
    - 如果 base 分支不存在，回退到 "master"
    """
    gh = _get_github_client()
    repo = gh.get_repo(repo_name)

    # 检查 base 分支是否存在，不存在时尝试 master
    try:
        repo.get_branch(base)
    except Exception:
        base = "master"

    pr = repo.create_pull(
        title=title,
        body=body,
        head=branch,
        base=base,
    )
    return pr.html_url


# ===========================================================================
# Git 操作
# 不依赖 gitpython（SDK），直接用 subprocess 调 git CLI。
# 这样 Agent 的 git 工具（tools/git_tool.py）和这里的 git 操作一致，
# 都是用 CLI 而非 SDK，减少依赖耦合。
# ===========================================================================

def _run_git(args: list[str], cwd: str) -> tuple[bool, str]:
    """
    运行 git 命令，返回 (success, output)。

    Args:
        args: git 命令参数列表，如 ["clone", url, local_path]
        cwd:  工作目录（repo 根目录或 /tmp）

    Returns:
        (success: bool, output: str)
        success = 返回码为 0
        output  = stdout + stderr 合并
    """
    try:
        proc = subprocess.run(
            ["git"] + args,
            capture_output=True,
            encoding="utf-8", errors="replace",
            timeout=60,
            cwd=cwd,
        )
        output = (proc.stdout + proc.stderr).strip()
        return proc.returncode == 0, output
    except Exception as e:
        return False, str(e)


def clone_repo(repo_name: str, local_path: str) -> None:
    """
    Clone repo 到本地路径（如果已存在则跳过）。

    如果 local_path 已存在且包含 .git 目录，跳过 clone。
    否则执行 git clone 并检查是否成功。

    Args:
        repo_name: "owner/repo" 格式
        local_path: 本地路径

    Raises:
        RuntimeError: git clone 失败时抛出
    """
    path = Path(local_path)
    if path.exists() and (path / ".git").exists():
        logger.info("Repo already exists at %s, skipping clone", local_path)
        return

    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        url = f"https://{token}@github.com/{repo_name}.git"
    else:
        url = f"https://github.com/{repo_name}.git"

    click.echo(f"Cloning {repo_name} → {local_path} ...")
    ok, out = _run_git(["clone", url, local_path], cwd="/tmp")
    if not ok:
        raise RuntimeError(f"git clone failed: {out}")


def create_branch(local_path: str, branch: str) -> None:
    """
    创建并切换到新分支。

    如果分支已存在则直接切换（不会报错或覆盖）。
    分支名格式：agent/fix-issue-{issue_number}-{timestamp}
    """
    ok, out = _run_git(["checkout", "-b", branch], cwd=local_path)
    if not ok:
        # 分支已存在，切换过去
        _run_git(["checkout", branch], cwd=local_path)


def push_branch(local_path: str, branch: str) -> None:
    """
    推送分支到远端（--set-upstream origin {branch}）。

    Args:
        local_path: 本地 repo 根目录
        branch:     要推送的分支名

    Raises:
        RuntimeError: git push 失败时抛出
    """
    ok, out = _run_git(
        ["push", "--set-upstream", "origin", branch],
        cwd=local_path,
    )
    if not ok:
        raise RuntimeError(f"git push failed: {out}")


# ===========================================================================
# 核心流程
# run_on_issue() 是 GitHub Issue 自动修复的编排函数。
# 7 步完整流程从 Issue 拉取到 PR 创建，每一步出错都有明确的错误处理和退出码。
# ===========================================================================

def run_on_issue(
    repo_name: str,
    issue_number: int,
    local_path: str,
    config_path: str | None = None,
    create_pr: bool = True,
    base_branch: str = "main",
) -> int:
    """
    拉取 Issue，运行 agent，创建 PR。

    Args:
        repo_name:  GitHub 仓库名，格式 "owner/repo"
        issue_number: Issue 编号
        local_path:   本地 repo 路径（不存在则 clone，存在则使用）
        config_path:  配置文件路径（可选，None 时用默认 config/default.yaml）
        create_pr:    是否自动创建 PR（True=创建，False=只修复不推送）
        base_branch:  PR 的目标分支（默认 main，不存在时自动尝试 master）

    Returns:
        0 if success, 1 if failed
    """
    from config.schema import load_config
    from agent.core import Agent, AgentConfig
    from agent.event_log import EventLog
    from agent.task import Task
    from llm.router import create_backend_from_config

    config = load_config(config_path)

    # ── 步骤 1：拉取 Issue ─────────────────────────────────────────────
    # 通过 PyGithub API 获取 Issue 标题、描述和 URL。
    # Issue 的 title + body 被格式化为任务描述传递给 Agent。
    click.echo(f"\nFetching issue #{issue_number} from {repo_name} ...")
    try:
        title, body, issue_url = fetch_issue(repo_name, issue_number)
    except Exception as e:
        click.echo(f"Error fetching issue: {e}", err=True)
        return 1

    click.echo(f"  Title: {title}")
    description = f"Fix GitHub Issue #{issue_number}: {title}\n\n{body}"

    # ── 步骤 2：Clone ─────────────────────────────────────────────────
    # 如果 local_path 已存在且包含 .git 目录则跳过。
    # 否则通过 GITHUB_TOKEN 进行认证 clone。
    try:
        clone_repo(repo_name, local_path)
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        return 1

    # ── 步骤 3：创建工作分支 ──────────────────────────────────────────
    # 分支名：agent/fix-issue-{issue_number}-{unix_timestamp}
    # 时间戳确保分支名全局唯一，不与已有分支冲突。
    branch = f"agent/fix-issue-{issue_number}-{int(time.time())}"
    create_branch(local_path, branch)
    click.echo(f"  Branch: {branch}")

    # ── 步骤 4：构建 Agent ────────────────────────────────────────────
    # 创建 LLMBackend → 注册 12 个工具 → 创建 Agent 实例 → 创建 Task
    try:
        backend = create_backend_from_config({
            "provider": config.llm.provider,
            "model":    config.llm.model,
            "api_key":  config.llm.api_key or None,
            "base_url": config.llm.base_url or None,
            "max_tokens": config.llm.max_tokens,
        })
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        return 1

    from entry.cli import _build_registry
    from tools.runtime import create_runtime
    runtime = create_runtime(sandbox=True, repo_path=local_path)
    preflight = runtime.preflight()
    if not preflight.success:
        runtime.cleanup()
        click.echo(f"Linux Runtime error: {preflight.stderr}", err=True)
        return 1
    registry = _build_registry(config, runtime=runtime)

    agent_config = AgentConfig(
        max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
    )
    agent = Agent(backend, registry, agent_config, runtime=runtime)

    task = Task(
        description=description,
        repo_path=local_path,
        issue_url=issue_url,
        max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
    )

    # ── 步骤 5：运行 Agent ────────────────────────────────────────────
    # Agent.run() 完成修复，返回 RunResult。
    # 如果 Agent 未成功完成（FAILED / GAVE_UP / MAX_STEPS），不继续 push/PR。
    click.echo(f"\nRunning agent on issue #{issue_number} ...")
    t0 = time.time()
    try:
        with EventLog.create(task, log_dir=config.agent.log_dir) as log:
            result = agent.run(task, log)
    finally:
        runtime.cleanup()

    elapsed = time.time() - t0
    click.echo(f"  Status : {result.status.value}")
    click.echo(f"  Steps  : {result.steps_taken}")
    click.echo(f"  Tokens : {result.total_tokens:,}")
    click.echo(f"  Time   : {elapsed:.1f}s")

    if not result.is_success():
        click.echo(f"  Agent did not complete the task.", err=True)
        return 1

    # ── 步骤 6：自动提交 ─────────────────────────────────────────────
    # agent 可能改了文件但没 git commit，这里统一提交。
    _run_git(["add", "-A"], cwd=local_path)
    ok, out = _run_git(
        ["commit", "--no-verify", "-m", f"[Agent] Fix issue #{issue_number}: {title}"],
        cwd=local_path,
    )
    if ok:
        click.echo(f"  Committed: Fix issue #{issue_number}: {title}")
    else:
        # 可能 nothing to commit（改了但没内容变化），不影响后续
        click.echo(f"  Commit: {out}")

    # ── 步骤 7：Push 分支 ────────────────────────────────────────────
    # 推送 agent 的修改到 GitHub。
    # push 失败时不阻断整个流程——修改已在本地，用户可手动 push。
    if create_pr:
        click.echo("\nPushing branch ...")
        try:
            push_branch(local_path, branch)
        except RuntimeError as e:
            click.echo(f"Warning: push failed: {e}", err=True)
            click.echo("Changes are committed locally. Push manually to create a PR.")
            return 0

        # ── 步骤 8：创建 PR ──────────────────────────────────────────
        # PR 标题和描述自动生成：
        # 标题："[Agent] Fix issue #{number}: {title}"
        # 描述："Fixes #{number}" + result.summary + 任务描述前 500 字符
        pr_title = f"[Agent] Fix issue #{issue_number}: {title}"
        pr_body = (
            f"Fixes #{issue_number}\n\n"
            f"This PR was automatically generated by the coding agent.\n\n"
            f"## Summary\n{result.summary}\n\n"
            f"## Task\n{description[:500]}"
        )
        try:
            pr_url = create_pull_request(
                repo_name=repo_name,
                branch=branch,
                title=pr_title,
                body=pr_body,
                base=base_branch,
            )
            click.echo(f"\n✓ PR created: {pr_url}\n")
        except Exception as e:
            click.echo(f"Warning: PR creation failed: {e}", err=True)
            click.echo(f"Branch pushed. Create PR manually from branch: {branch}")

    return 0


# ===========================================================================
# CLI
# 直接执行：python -m entry.github_issue -r owner/repo -i 42 -l /tmp/repo
# ===========================================================================

@click.command()
@click.option("--repo", "-r", required=True, help="GitHub repo (owner/repo)")
@click.option("--issue", "-i", required=True, type=int, help="Issue number")
@click.option(
    "--local-path", "-l", required=True,
    help="Local path to clone/use the repo",
)
@click.option("--config", "-c", default=None, help="Config YAML path")
@click.option("--no-pr", is_flag=True, help="Skip PR creation")
@click.option("--base-branch", default="main", help="Base branch for PR (default: main)")
@click.option("--verbose", "-v", is_flag=True)
def main(
    repo: str,
    issue: int,
    local_path: str,
    config: str | None,
    no_pr: bool,
    base_branch: str,
    verbose: bool,
) -> None:
    """Run the coding agent on a GitHub issue and create a PR."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    sys.exit(run_on_issue(
        repo_name=repo,
        issue_number=issue,
        local_path=local_path,
        config_path=config,
        create_pr=not no_pr,
        base_branch=base_branch,
    ))


if __name__ == "__main__":
    main()
