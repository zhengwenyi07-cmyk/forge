"""Run the official harness with an LF guard at the container copy boundary."""
from __future__ import annotations

import runpy
from pathlib import Path


def normalize_script(path: Path) -> None:
    """Use bytes: write_text on Windows can translate LF back into CRLF."""
    if path.suffix != ".sh":
        return
    content = path.read_bytes().replace(b"\r\n", b"\n")
    if b"\r" in content:
        raise ValueError(f"Bare CR in shell script: {path}")
    path.write_bytes(content)
    assert b"\r" not in path.read_bytes(), f"Shell script must contain only LF: {path}"


def install_copy_guard(module) -> None:
    original = module.copy_to_container

    def copy_with_lf(container, src, dst):
        normalize_script(Path(src))
        return original(container, src, dst)

    module.copy_to_container = copy_with_lf


def main() -> None:
    import sys
    if sys.platform != "linux":
        raise SystemExit("Run the SWE-bench harness in Linux/Docker, not native Windows.")
    from swebench.harness import docker_utils
    install_copy_guard(docker_utils)
    runpy.run_module("swebench.harness.run_evaluation", run_name="__main__")


if __name__ == "__main__":
    main()
