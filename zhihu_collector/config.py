"""运行路径与可持久化设置。"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from uuid import uuid4
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import logging

logger = logging.getLogger("zhihu_collector.config")


def app_dir() -> Path:
    """可执行文件或源码所在的目录。打包后位于 exe 旁边，开发时位于仓库根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def is_portable_mode() -> bool:
    """便携模式判断：设置了环境变量 ZHIHU_COLLECTOR_PORTABLE 或 exe 同级存在 portable.flag。"""
    if os.environ.get("ZHIHU_COLLECTOR_PORTABLE", "").strip().lower() in ("1", "true", "yes"):
        return True
    return (app_dir() / "portable.flag").is_file()


def _is_directory_writable(directory: Path) -> bool:
    """测试目录是否具有写权限。"""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".write-test-{uuid4().hex}.tmp"
        probe.write_text("1", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def _migrate_legacy_data(legacy_dir: Path, target_dir: Path) -> None:
    """从旧的 exe 旁数据目录迁移数据到 LocalAppData（仅当新目录尚未建立主数据库时）。"""
    legacy_db = legacy_dir / "zhihu.db"
    target_db = target_dir / "zhihu.db"
    if legacy_db.is_file() and not target_db.is_file():
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            for item in legacy_dir.iterdir():
                dest = target_dir / item.name
                if not dest.exists():
                    if item.is_dir():
                        shutil.copytree(item, dest)
                    else:
                        shutil.copy2(item, dest)
            logger.info("已将旧数据迁移至：%s", target_dir)
        except Exception as exc:
            logger.warning("迁移旧数据时遇到问题：%s", exc)


def user_data_root() -> Path:
    """运行时可写根目录：
    1. 非 frozen 模式（源码开发/测试）：默认直接使用仓库根目录；
    2. frozen 模式且显式便携模式：如果 app_dir() 可写，使用 app_dir()；
    3. frozen 模式安装版（如安装在 Program Files）：使用 %LOCALAPPDATA%\\ZhihuCollector。
    """
    if not getattr(sys, "frozen", False):
        return app_dir()

    if is_portable_mode() and _is_directory_writable(app_dir()):
        return app_dir()

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        root = Path(local_app_data) / "ZhihuCollector"
    else:
        root = Path.home() / "AppData" / "Local" / "ZhihuCollector"

    _migrate_legacy_data(app_dir() / "data", root / "data")
    return root


def ensure_runtime_dirs() -> None:
    for path in (
        data_dir(),
        data_dir() / "auth",
        data_dir() / "output",
        log_dir(),
    ):
        path.mkdir(parents=True, exist_ok=True)


def data_dir() -> Path:
    return user_data_root() / "data"


def log_dir() -> Path:
    return user_data_root() / "logs"


def runtime_cache_dir(data_root: Path | None = None) -> Path:
    """可丢弃的浏览器/PDF缓存；用户数据和显式隔离目录不迁移。"""
    override = os.environ.get("ZHIHU_COLLECTOR_RUNTIME_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    configured = data_dir().resolve()
    requested = Path(data_root).resolve() if data_root is not None else configured
    if (requested != configured or is_portable_mode()
            or (not getattr(sys, "frozen", False)
                and user_data_root().resolve() != app_dir().resolve())):
        return requested
    local = os.environ.get("LOCALAPPDATA")
    candidate = (Path(local) if local else Path.home() / "AppData" / "Local") / "ZhihuCollector" / "runtime-cache"
    if _is_directory_writable(candidate):
        return candidate.resolve()
    return requested / "runtime-cache"


def default_database_path() -> Path:
    return data_dir() / "zhihu.db"


def default_storage_state_path() -> Path:
    return data_dir() / "auth" / "zhihu_storage_state.json"


def default_settings_path() -> Path:
    return data_dir() / "settings.json"


def default_presets_path() -> Path:
    return data_dir() / "collection_presets.json"


@dataclass
class Settings:
    """用户可在设置页修改的选项。时间单位为毫秒或次数。"""

    headful_debug: bool = False
    download_images: bool = True
    save_metadata: bool = True
    image_concurrency: int = 3
    page_interval_ms: int = 1500
    request_interval_ms: int = 1200
    retry_count: int = 3
    retry_base_delay_ms: int = 1500
    max_stale_rounds: int = 5
    filename_rule: str = "{index:04d}-{title}"
    output_dir: str = ""
    database_path: str = ""
    export_markdown: bool = True
    export_docx: bool = True
    export_pdf: bool = True
    export_separate: bool = True
    export_merged: bool = False

    def resolved_output_dir(self) -> Path:
        if self.output_dir:
            return Path(self.output_dir)
        return data_dir() / "output"

    def resolved_database_path(self) -> Path:
        if self.database_path:
            return Path(self.database_path)
        return default_database_path()


def _validate_setting_value(field_name: str, value: object, default_value: object) -> object:
    """校验字段类型与合理数值范围，不合法时恢复默认值并记录警告。"""
    if isinstance(default_value, bool):
        if not isinstance(value, bool):
            logger.warning("配置项 %s 类型错误（需布尔值），已重置为默认值 %s", field_name, default_value)
            return default_value
        return value

    if isinstance(default_value, int):
        # 注意：Python 中 bool 是 int 的子类，必须先排除
        if isinstance(value, bool) or not isinstance(value, int):
            logger.warning("配置项 %s 类型错误（需整数），已重置为默认值 %s", field_name, default_value)
            return default_value

        # 数值边界检查
        if field_name == "image_concurrency" and not (1 <= value <= 20):
            logger.warning("配置项 %s 超出范围 1~20，已重置为默认值 %s", field_name, default_value)
            return default_value
        if field_name == "retry_count" and not (0 <= value <= 20):
            logger.warning("配置项 %s 超出范围 0~20，已重置为默认值 %s", field_name, default_value)
            return default_value
        if field_name == "max_stale_rounds" and not (1 <= value <= 50):
            logger.warning("配置项 %s 超出范围 1~50，已重置为默认值 %s", field_name, default_value)
            return default_value
        if field_name in ("page_interval_ms", "request_interval_ms", "retry_base_delay_ms") and value < 0:
            logger.warning("配置项 %s 需为非负整数，已重置为默认值 %s", field_name, default_value)
            return default_value
        return value

    if isinstance(default_value, str):
        if not isinstance(value, str):
            logger.warning("配置项 %s 类型错误（需字符串），已重置为默认值 %s", field_name, default_value)
            return default_value
        return value

    return value


def load_settings(path: Path | None = None) -> Settings:
    path = path or default_settings_path()
    default_instance = Settings()
    if not path.is_file():
        return default_instance
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        backup = path.with_name(f"{path.name}.corrupt-{uuid4().hex[:12]}.bak")
        try:
            os.replace(path, backup)
            logger.warning("设置文件无法读取，已备份至 %s；本次使用默认设置：%s", backup, exc)
        except OSError:
            logger.exception("设置文件损坏且无法备份 %s；本次使用默认设置", path)
        return default_instance
    except OSError:
        logger.exception("设置文件无法读取 %s；本次使用默认设置", path)
        return default_instance
    if not isinstance(raw, dict):
        logger.warning("设置文件结构无效 %s；本次使用默认设置", path)
        return default_instance

    defaults = {f.name: getattr(default_instance, f.name) for f in fields(Settings)}
    validated = {}
    for key, def_val in defaults.items():
        if key in raw:
            validated[key] = _validate_setting_value(key, raw[key], def_val)
        else:
            validated[key] = def_val
    return Settings(**validated)


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    path = path or default_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(asdict(settings), ensure_ascii=False, indent=2)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            logger.warning("无法清理临时设置文件：%s", temporary, exc_info=True)
    return path
