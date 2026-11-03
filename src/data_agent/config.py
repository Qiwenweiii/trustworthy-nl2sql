"""配置来自项目根目录的 .env 与系统环境变量；系统环境变量优先。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "demo.db"
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


class ConfigError(RuntimeError):
    """环境变量缺失或取值非法。"""


@dataclass(frozen=True)
class Config:
    """一次运行所需的全部配置，来自 .env 或系统环境变量。"""

    api_key: str
    base_url: str | None
    model: str
    db_path: Path
    max_rows: int
    query_timeout_s: float


def ensure_db_exists(config: Config) -> None:
    """演示库文件缺失时抛 ConfigError。

    CLI 与评测 runner 都用它，"退出码 2 = 配置不满足"的文案只有一份。刻意不放进
    load_config：配置解析不该依赖磁盘状态，否则配置相关测试会被演示库文件牵着走。
    """
    if not config.db_path.exists():
        raise ConfigError(
            f"演示库不存在: {config.db_path}\n先运行: python scripts/init_db.py"
        )


def _positive_int(name: str, default: int) -> int:
    """读取正整数环境变量；未设置时返回默认值。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数，当前值无法解析") from exc
    if value <= 0:
        raise ConfigError(f"{name} 必须大于 0")
    return value


def _positive_float(name: str, default: float) -> float:
    """读取正数环境变量；未设置时返回默认值。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是数字，当前值无法解析") from exc
    if value <= 0:
        raise ConfigError(f"{name} 必须大于 0")
    return value


def load_config(env_file: Path | None = None, *, require_llm: bool = True) -> Config:
    """加载 .env 与环境变量，校验必填项后返回 Config。

    require_llm=False 时跳过 api_key/model 校验，供只走注入路径的评测使用；
    此时 api_key 与 model 可能为空字符串，调用方不得拿去建 LLM 客户端。
    """
    load_dotenv(env_file or DEFAULT_ENV_FILE, override=False)

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("DATA_AGENT_MODEL", "").strip()

    missing = [name for name, value in (("OPENAI_API_KEY", api_key), ("DATA_AGENT_MODEL", model)) if not value]
    if missing and require_llm:
        raise ConfigError(
            "缺少环境变量: "
            + ", ".join(missing)
            + "（请在项目根目录 .env 中配置，参考 .env.example；已存在的系统环境变量优先）"
        )

    base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or None
    raw_db = os.environ.get("DATA_AGENT_DB_PATH", "").strip()
    db_path = Path(raw_db).expanduser().resolve() if raw_db else DEFAULT_DB_PATH

    return Config(
        api_key=api_key,
        base_url=base_url,
        model=model,
        db_path=db_path,
        max_rows=_positive_int("DATA_AGENT_MAX_ROWS", 200),
        query_timeout_s=_positive_float("DATA_AGENT_QUERY_TIMEOUT", 5.0),
    )
