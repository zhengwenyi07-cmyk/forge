"""
tools/runtime.py

Runtime 抽象层：把"命令执行"从工具实现里解耦出来。

工具（ShellTool / PytestTool / GitTool）只负责构造命令参数，
Runtime 负责实际执行——本地 subprocess 或 Docker 容器。

设计原则：
- 工具层完全不感知 Runtime，通过依赖注入传入
- Runtime 可以在 ToolRegistry 创建时一次性注入，所有工具共享
- LocalRuntime 是默认行为（向后兼容，不传 runtime 等同于之前）
- DockerRuntime 管理容器生命周期，首次执行时懒启动容器

用法：
    # 默认本地
    registry = build_registry()

    # Docker 沙箱
    runtime = DockerRuntime(repo_path="/path/to/repo")
    registry = build_registry(runtime=runtime)
    # agent 跑完后清理
    runtime.cleanup()

    # 或者用上下文管理器自动清理
    with DockerRuntime(repo_path="/path/to/repo") as runtime:
        registry = build_registry(runtime=runtime)
        agent.run(task, log)
"""

from __future__ import annotations

import subprocess
import logging
import uuid
import shlex
import os
import sys
import posixpath
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RunResult — Runtime 执行结果
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    """Runtime 执行单条命令的结果。"""
    returncode: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        """合并 stdout + stderr，工具层直接用。"""
        return self.stdout + self.stderr


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------

class Runtime(ABC):
    """
    命令执行抽象基类。
    所有工具通过 runtime.exec() 执行命令，不直接调 subprocess。
    """

    @abstractmethod
    def exec(
        self,
        cmd: str,
        cwd: str | None = None,
        timeout: int = 30,
    ) -> RunResult:
        """
        执行 shell 命令，返回 RunResult。

        Args:
            cmd:     shell 命令字符串
            cwd:     工作目录（相对或绝对路径）
            timeout: 超时秒数

        Returns:
            RunResult，不抛异常（超时/错误封装在里面）
        """
        ...

    def exec_argv(self, argv: list[str], cwd: str | None = None,
                  timeout: int = 30, stdin: str | None = None) -> RunResult:
        """Execute data arguments without shell interpretation."""
        if stdin is not None:
            return RunResult(-1, "", "Runtime does not support stdin")
        return self.exec(shlex.join(argv), cwd=cwd, timeout=timeout)

    def exec_python(self, script: str, cwd: str | None = None, timeout: int = 30) -> RunResult:
        python = getattr(self, "_helper_python", sys.executable)
        return self.exec_argv([python, "-"], cwd=cwd, timeout=timeout, stdin=script)

    def cleanup(self) -> None:
        """释放 runtime 持有的资源（容器、连接等）。默认无操作。"""

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, *_) -> None:
        self.cleanup()

    @property
    @abstractmethod
    def name(self) -> str:
        """Runtime 名称，用于日志。"""
        ...


# ---------------------------------------------------------------------------
# LocalRuntime — 本地 subprocess（默认）
# ---------------------------------------------------------------------------

class LocalRuntime(Runtime):
    """
    本地执行，直接调 subprocess.run。
    行为和之前完全一致，是默认 runtime。
    """

    def __init__(self, repo_path: str | None = None):
        self.repo_path = repo_path

    @property
    def name(self) -> str:
        return "local"

    def exec(
        self,
        cmd: str,
        cwd: str | None = None,
        timeout: int = 30,
    ) -> RunResult:
        if cmd.startswith('python -c "import base64;'):
            python = subprocess.list2cmdline([sys.executable]) if os.name == "nt" else shlex.quote(sys.executable)
            cmd = python + cmd[len("python"):]
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                executable="/bin/bash" if os.name != "nt" else None,
                capture_output=True,
                encoding="utf-8", errors="replace",
                timeout=timeout,
                cwd=cwd or self.repo_path,
            )
            return RunResult(
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        except subprocess.TimeoutExpired:
            return RunResult(
                returncode=-1,
                stdout="",
                stderr=f"Command timed out after {timeout}s: {cmd!r}",
            )
        except Exception as e:
            return RunResult(returncode=-1, stdout="", stderr=str(e))

    def exec_argv(self, argv, cwd=None, timeout=30, stdin=None):
        try:
            proc = subprocess.run(argv, input=stdin, shell=False, capture_output=True,
                                  encoding="utf-8", errors="replace", timeout=timeout,
                                  cwd=cwd or self.repo_path, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            return RunResult(proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired:
            return RunResult(-1, "", f"Command timed out after {timeout}s")
        except OSError as exc:
            return RunResult(-1, "", str(exc))


# ---------------------------------------------------------------------------
# DockerRuntime — Docker 沙箱
# ---------------------------------------------------------------------------

# 沙箱容器使用的 Docker 镜像
# 包含 Python、git、常用工具，体积合理
SANDBOX_IMAGE = "forge-runtime:py311"

# 容器内 repo 的挂载路径
CONTAINER_WORKDIR = "/workspace"

# Git identity is prepared before project installation. create_runtime detects
# project dependencies; explicit setup_cmds can select a custom environment.
DEFAULT_SETUP_CMDS = [
    "git config --global safe.directory '*'",
    "git config --global user.email agent@forge.dev",
    "git config --global user.name 'Forge Agent'",
]


class DockerRuntime(Runtime):
    """
    Docker 沙箱 Runtime。

    首次调用 exec() 时懒启动容器：
    - 基于 python:3.11-slim 镜像
    - 把 repo_path bind mount 到容器的 /workspace
    - 容器持续运行（tail -f /dev/null），每条命令用 docker exec 执行
    - cleanup() 时停止并删除容器

    这样比每条命令都 docker run 快得多（避免反复启动容器的开销）。

    Args:
        repo_path:   宿主机上 repo 的绝对路径，会被 mount 进容器
        image:       Docker 镜像名，默认 python:3.11-slim
        extra_mounts: 额外的 bind mount，格式 [(host_path, container_path), ...]
        setup_cmds:  容器启动后执行的初始化命令（如 pip install -r requirements.txt）
    """

    def __init__(
        self,
        repo_path: str | Path,
        image: str = SANDBOX_IMAGE,
        extra_mounts: list[tuple[str, str]] | None = None,
        setup_cmds: list[str] | None = None,
        check_cmds: list[str] | None = None,
        setup_timeout: int = 600,
    ) -> None:
        self._repo_path = str(Path(repo_path).resolve())
        self._image = image
        self._extra_mounts = extra_mounts or []
        self._setup_cmds = list(setup_cmds or [])
        self._check_cmds = list(check_cmds or [])
        self._setup_timeout = setup_timeout
        self._environment: dict[str, str] = {}
        self._workdir = CONTAINER_WORKDIR
        self._command_prefix = ""
        self._helper_python = "python"
        self._container_id: str | None = None
        self._allow_network: bool = False
        # 默认 setup 完成后断网（安全隔离）
        # 创建者如果希望始终保持网络，可以设为 False
        self._disconnect_after_setup: bool = True
        # 容器名加随机后缀，避免冲突
        self._container_name = f"coding-agent-sandbox-{uuid.uuid4().hex[:8]}"

    @property
    def name(self) -> str:
        return f"docker({self._image})"

    @property
    def container_id(self) -> str | None:
        return self._container_id

    @property
    def is_running(self) -> bool:
        return self._container_id is not None

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def exec(
        self,
        cmd: str,
        cwd: str | None = None,
        timeout: int = 30,
    ) -> RunResult:
        return self.exec_argv(["bash", "-c", cmd], cwd=cwd, timeout=timeout)

    def exec_argv(self, argv, cwd=None, timeout=30, stdin=None):
        """Keep payloads on stdin, including on Windows hosts."""
        if not self.is_running:
            startup_result = self._start_container()
            if startup_result is not None:
                # 启动失败，返回错误
                return startup_result

        if cwd and (cwd == self._workdir or cwd.startswith(self._workdir + "/")):
            container_cwd = posixpath.normpath(cwd)
        elif cwd and Path(cwd).is_absolute():
            try:
                relative = Path(cwd).resolve().relative_to(self._repo_path).as_posix()
            except ValueError:
                return RunResult(-1, "", "Working directory must be inside the repository")
            container_cwd = posixpath.join(self._workdir, relative)
        elif cwd:
            container_cwd = posixpath.normpath(posixpath.join(self._workdir, cwd.replace("\\", "/")))
        else:
            container_cwd = self._workdir
        if container_cwd != self._workdir and not container_cwd.startswith(self._workdir + "/"):
            return RunResult(-1, "", "Working directory must be inside the repository")

        if self._command_prefix:
            argv = ["bash", "-c", self._command_prefix + ' && exec "$@"', "forge-command", *argv]

        docker_cmd = [
            "docker", "exec", "-i",
            "--workdir", container_cwd,
            self._container_id,
            "timeout", "--signal=TERM", "--kill-after=5", str(timeout), *argv,
        ]

        try:
            proc = subprocess.run(
                docker_cmd,
                input=stdin,
                capture_output=True,
                encoding="utf-8", errors="replace",
                timeout=timeout + 5,   # docker exec 本身有少量开销
            )
            if proc.returncode in (124, 137):
                return RunResult(returncode=-1, stdout=proc.stdout, stderr=f"Command timed out after {timeout}s\n{proc.stderr}")
            return RunResult(
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        except subprocess.TimeoutExpired:
            return RunResult(
                returncode=-1,
                stdout="",
                stderr=f"Command timed out after {timeout}s in container",
            )
        except Exception as e:
            return RunResult(returncode=-1, stdout="", stderr=str(e))

    def cleanup(self) -> None:
        """停止并删除容器。"""
        if not self._container_id:
            return
        logger.info("Stopping sandbox container %s", self._container_name)
        try:
            subprocess.run(
                ["docker", "rm", "-f", self._container_id],
                capture_output=True, timeout=15,
            )
        except Exception as e:
            logger.warning("Failed to remove container %s: %s", self._container_id, e)
        finally:
            self._container_id = None

    # ------------------------------------------------------------------
    # 内部：容器生命周期
    # ------------------------------------------------------------------

    def _start_container(self) -> RunResult | None:
        """
        拉取镜像（如需要）并启动容器。
        返回 None 表示成功，返回 RunResult 表示失败。
        """
        logger.info(
            "Starting sandbox container %s (image=%s, repo=%s)",
            self._container_name, self._image, self._repo_path,
        )

        # 检查 Docker 是否可用
        try:
            check = subprocess.run(["docker", "info", "--format", "{{.OSType}}"], capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return RunResult(-1, "", f"Docker preflight failed: {exc}")
        if check.returncode != 0:
            return RunResult(
                returncode=-1,
                stdout="",
                stderr=(
                    "Docker is not available. "
                    "Make sure Docker Desktop's Linux engine is running."
                ),
            )

        if check.stdout.strip() not in (b'linux', 'linux'):
            return RunResult(-1, "", "Docker must use Linux containers.")
        # 构建 docker run 命令
        run_args = [
            "docker", "run",
            "--detach",                                 # 后台运行
            "--name", self._container_name,
            "--rm",                                     # 停止时自动删除
            "--init",
            "-v", f"{self._repo_path}:{self._workdir}",
            "--workdir", self._workdir,
        ]

        # 网络策略：默认断网更安全，但 setup_cmds 需要网络（apt-get/pip install）
        # 创建时如果用户传了 setup_cmds 或显式设置了 _allow_network=True，就放行网络
        if not self._allow_network:
            run_args += ["--network", "none"]

        # 补上 git 用户配置（避免容器内 git commit 报错 "Please tell me who you are"）
        # 放在 setup_cmds 尾部（apt-get install git 已经先执行完了）
        if any("git" in cmd for cmd in (self._setup_cmds or [])):
            self._setup_cmds = list(self._setup_cmds or [])
            self._setup_cmds.append("git config --global user.email 'agent@forge.dev'")
            self._setup_cmds.append("git config --global user.name 'Forge Agent'")

        # 额外 mount
        for host_path, container_path in self._extra_mounts:
            run_args += ["-v", f"{host_path}:{container_path}"]
        for key, value in self._environment.items():
            run_args += ["-e", f"{key}={value}"]

        run_args += [self._image, "tail", "-f", "/dev/null"]

        try:
            proc = subprocess.run(
                run_args,
                capture_output=True,
                encoding="utf-8", errors="replace",
                timeout=60,  # 拉镜像可能需要时间
            )
        except subprocess.TimeoutExpired:
            return RunResult(
                returncode=-1, stdout="",
                stderr="Timed out starting Docker container (60s). Is Docker running?",
            )
        except OSError as exc:
            return RunResult(-1, "", str(exc))

        if proc.returncode != 0:
            return RunResult(
                returncode=proc.returncode,
                stdout="",
                stderr=f"Failed to start container:\n{proc.stderr}",
            )

        self._container_id = proc.stdout.strip()
        logger.info("Container started: %s", self._container_id[:12])

        # 执行初始化命令
        for setup_cmd in [*self._setup_cmds, *self._check_cmds]:
            result = self.exec(setup_cmd, timeout=self._setup_timeout)
            if not result.success:
                self.cleanup()
                return RunResult(-1, result.stdout, f"Environment preparation failed: {setup_cmd}\n{result.output}\nConfigure runtime.image/setup_commands/check_commands for this project.")

        # setup 完成后断网，恢复沙箱隔离
        # 只有初始有网络（_allow_network=True）且设了 post-setup 断网时才执行
        if self._allow_network and self._disconnect_after_setup:
            try:
                disconnect = subprocess.run(
                    ["docker", "network", "disconnect", "bridge", self._container_name],
                    capture_output=True, timeout=10,
                )
                if disconnect.returncode:
                    self.cleanup()
                    return RunResult(-1, "", "Could not disconnect network after environment preparation")
                logger.info("Disconnected container from network (post-setup isolation)")
            except Exception as e:
                self.cleanup()
                return RunResult(-1, "", f"Failed to disconnect network after setup: {e}")

        return None   # 成功

    def preflight(self) -> RunResult:
        return self.exec("python --version && git --version", timeout=30)

    def for_repo(self, repo_path: str, extra_mounts=None, environment=None):
        runtime = DockerRuntime(repo_path, image=self._image, setup_cmds=list(self._setup_cmds), extra_mounts=extra_mounts,
                                check_cmds=self._check_cmds, setup_timeout=self._setup_timeout)
        runtime._allow_network = self._allow_network
        runtime._disconnect_after_setup = self._disconnect_after_setup
        runtime._environment = dict(environment or {})
        runtime._workdir = self._workdir
        runtime._command_prefix = self._command_prefix
        runtime._helper_python = self._helper_python
        return runtime

    def install_requirements(self, requirements_file: str = "requirements.txt") -> RunResult:
        """
        在容器里安装依赖。快捷方法，等价于 exec("pip install -r requirements.txt")。
        """
        return self.exec(
            f"pip install -r {requirements_file} -q",
            timeout=120,
        )


# ---------------------------------------------------------------------------
# 便捷工厂函数
# ---------------------------------------------------------------------------

def create_runtime(
    sandbox: bool = True,
    repo_path: str | None = None,
    image: str = SANDBOX_IMAGE,
    network: bool = True,
    setup_cmds: list[str] | None = None,
    check_cmds: list[str] | None = None,
    setup_timeout: int = 600,
) -> Runtime:
    """
    根据配置创建合适的 Runtime。

    Args:
        sandbox:    True 则创建 DockerRuntime，False 则 LocalRuntime
        repo_path:  sandbox=True 时必须提供
        image:      Docker 镜像名
        network:    sandbox 模式下是否允许网络（默认 True，因为 setup_cmds 需要网络安装依赖）
        setup_cmds: None 时根据 requirements/pyproject/setup.py/package.json 安装项目依赖；
                    [] 表示使用预装依赖。Git 配置仍会执行。
        check_cmds: 安装完成后执行的环境检查，默认 python -m pip check。
        setup_timeout: 每条安装/检查命令的超时秒数。全部成功后才断网执行任务。

    Returns:
        Runtime 实例
    """
    if not sandbox:
        if os.name == "nt":
            raise ValueError("Native Windows execution is unsupported. Use Docker or run Forge inside Linux/WSL.")
        return LocalRuntime(repo_path=repo_path)

    if not repo_path:
        raise ValueError("repo_path is required when sandbox=True")

    project_setup = detect_project_setup(Path(repo_path)) if setup_cmds is None else list(setup_cmds)

    runtime = DockerRuntime(
        repo_path=repo_path,
        image=image,
        setup_cmds=[*DEFAULT_SETUP_CMDS, *project_setup],
        check_cmds=check_cmds if check_cmds is not None else ["python -m pip check"],
        setup_timeout=setup_timeout,
    )
    if network:
        runtime._allow_network = True

    return runtime


def detect_project_setup(repo: Path) -> list[str]:
    commands = []
    if (repo / "requirements.txt").is_file():
        commands.append("python -m pip install -r requirements.txt")
    if (repo / "pyproject.toml").is_file() or (repo / "setup.py").is_file():
        commands.append("python -m pip install -e .")
    if (repo / "package-lock.json").is_file():
        commands.append("npm ci")
    elif (repo / "package.json").is_file():
        commands.append("npm install")
    return commands
