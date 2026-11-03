from __future__ import annotations

import os

import pytest

from data_agent.config import ConfigError, load_config

ENV_KEYS = (
    "OPENAI_API_KEY",
    "DATA_AGENT_MODEL",
    "OPENAI_BASE_URL",
    "DATA_AGENT_DB_PATH",
    "DATA_AGENT_MAX_ROWS",
    "DATA_AGENT_QUERY_TIMEOUT",
)


@pytest.fixture()
def isolated_env():
    """隔离配置相关环境变量，测试结束后恢复原值。"""
    saved = {key: os.environ.get(key) for key in ENV_KEYS}
    for key in ENV_KEYS:
        os.environ.pop(key, None)
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _write_env(tmp_path, **values):
    """在临时目录写一个包含给定键值对的 .env 文件。"""
    path = tmp_path / ".env"
    path.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8"
    )
    return path


def test_loads_from_dotenv(tmp_path, isolated_env):
    """能从指定 .env 读取并解析配置。"""
    env_file = _write_env(
        tmp_path,
        OPENAI_API_KEY="key-from-file",
        DATA_AGENT_MODEL="model-from-file",
        DATA_AGENT_MAX_ROWS="7",
        DATA_AGENT_QUERY_TIMEOUT="1.5",
    )
    config = load_config(env_file=env_file)

    assert config.api_key == "key-from-file"
    assert config.model == "model-from-file"
    assert config.max_rows == 7
    assert config.query_timeout_s == 1.5


def test_default_env_file_is_used(tmp_path, isolated_env, monkeypatch):
    """不传参数时使用默认 .env 路径。"""
    env_file = _write_env(
        tmp_path,
        OPENAI_API_KEY="key-from-default",
        DATA_AGENT_MODEL="model-from-default",
    )
    monkeypatch.setattr("data_agent.config.DEFAULT_ENV_FILE", env_file)

    config = load_config()

    assert config.api_key == "key-from-default"
    assert config.model == "model-from-default"


def test_system_env_overrides_dotenv(tmp_path, isolated_env, monkeypatch):
    """系统环境变量优先于 .env 中的同名项。"""
    env_file = _write_env(
        tmp_path,
        OPENAI_API_KEY="key-from-file",
        DATA_AGENT_MODEL="model-from-file",
    )
    monkeypatch.setenv("DATA_AGENT_MODEL", "model-from-env")

    config = load_config(env_file=env_file)

    assert config.model == "model-from-env"
    assert config.api_key == "key-from-file"


def test_missing_keys_raise_with_variable_names(tmp_path, isolated_env):
    """缺必填项时报错信息包含变量名。"""
    with pytest.raises(ConfigError) as excinfo:
        load_config(env_file=tmp_path / "absent.env")

    message = str(excinfo.value)
    assert "OPENAI_API_KEY" in message
    assert "DATA_AGENT_MODEL" in message


def test_require_llm_false_allows_missing_keys(tmp_path, isolated_env):
    """不要求 LLM 时（纯注入评测）可缺 key/model，但其余配置照常解析。"""
    env_file = _write_env(tmp_path, DATA_AGENT_MAX_ROWS="9")

    config = load_config(env_file=env_file, require_llm=False)

    assert config.api_key == ""
    assert config.model == ""
    assert config.max_rows == 9


def test_invalid_number_raises(tmp_path, isolated_env):
    """数值配置非法时报 ConfigError。"""
    env_file = _write_env(
        tmp_path,
        OPENAI_API_KEY="key",
        DATA_AGENT_MODEL="model",
        DATA_AGENT_MAX_ROWS="not-a-number",
    )
    with pytest.raises(ConfigError, match="DATA_AGENT_MAX_ROWS"):
        load_config(env_file=env_file)
