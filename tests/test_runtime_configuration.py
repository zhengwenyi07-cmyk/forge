import pytest

from config.schema import load_config
from tools.runtime import detect_project_setup, create_runtime


def test_project_setup_installs_requirements_and_project(tmp_path):
    (tmp_path / "requirements.txt").write_text("pyyaml\n")
    (tmp_path / "pyproject.toml").write_text("[build-system]\n")
    assert detect_project_setup(tmp_path) == ["python -m pip install -r requirements.txt", "python -m pip install -e ."]
    runtime = create_runtime(repo_path=str(tmp_path), setup_cmds=[])
    assert not any("pip install" in command for command in runtime._setup_cmds)


def test_runtime_configuration_round_trip(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("runtime:\n  image: custom:latest\n  setup_commands: ['python -m pip install -r dev.txt']\n  check_commands: ['python -c \"import yaml\"']\n  setup_network: false\n  setup_timeout: 75\n")
    cfg = load_config(config)
    runtime = create_runtime(repo_path=str(tmp_path), **cfg.runtime.options())
    assert runtime._image == "custom:latest"
    assert runtime._setup_cmds[-1] == "python -m pip install -r dev.txt"
    assert runtime._check_cmds == ['python -c "import yaml"']
    assert not runtime._allow_network
    assert runtime._setup_timeout == 75


def test_command_list_must_not_be_a_string(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("runtime:\n  setup_commands: 'pip install pyyaml'\n")
    with pytest.raises(ValueError, match="list of nonempty commands"):
        load_config(config)
