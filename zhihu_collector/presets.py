"""常用采集配置（Preset）管理。

保存、切换和管理常用选项组合，支持白名单校验、原子写入和损坏恢复。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from zhihu_collector.config import default_presets_path
from zhihu_collector.utils.logger import get_logger

logger = get_logger("presets")

PRESETS_VERSION = 1

DEFAULT_PRESETS_DATA = [
    {
        "id": "daily_archive",
        "name": "日常图文归档",
        "export_markdown": True,
        "export_docx": False,
        "export_pdf": False,
        "export_separate": True,
        "export_merged": False,
        "download_images": True,
        "save_metadata": True,
        "max_items": 0,
        "remember_output_dir": False,
        "output_dir": "",
    },
    {
        "id": "merged_reading",
        "name": "合并阅读",
        "export_markdown": False,
        "export_docx": True,
        "export_pdf": True,
        "export_separate": False,
        "export_merged": True,
        "download_images": True,
        "save_metadata": True,
        "max_items": 0,
        "remember_output_dir": False,
        "output_dir": "",
    },
    {
        "id": "small_sample",
        "name": "小量试采",
        "export_markdown": True,
        "export_docx": False,
        "export_pdf": False,
        "export_separate": True,
        "export_merged": False,
        "download_images": True,
        "save_metadata": False,
        "max_items": 5,
        "remember_output_dir": False,
        "output_dir": "",
    },
]


@dataclass
class Preset:
    id: str
    name: str
    export_markdown: bool = True
    export_docx: bool = False
    export_pdf: bool = False
    export_separate: bool = True
    export_merged: bool = False
    download_images: bool = True
    save_metadata: bool = True
    update_only: bool = False
    execution_intent: str = ""
    max_items: int = 0
    remember_output_dir: bool = False
    output_dir: str = ""

    def validate(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("配置 ID 不能为空")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("配置名称不能为空")
        if len(self.name) > 50:
            raise ValueError("配置名称长度不能超过 50 个字符")

        # 检查布尔类型
        for attr in (
            "export_markdown",
            "export_docx",
            "export_pdf",
            "export_separate",
            "export_merged",
            "download_images",
            "save_metadata",
            "update_only",
            "remember_output_dir",
        ):
            val = getattr(self, attr)
            if not isinstance(val, bool):
                raise ValueError(f"配置项 {attr} 必须是布尔值")

        # 检查 max_items：注意 bool 是 int 的子类，必须显式排除
        if isinstance(self.max_items, bool) or not isinstance(self.max_items, int) or self.max_items < 0:
            raise ValueError("篇数上限必须为非负整数")

        if self.execution_intent not in {"", "library", "export"}:
            raise ValueError("执行方式只能是更新内容库、更新并导出，或沿用原来的导出行为")

        if not isinstance(self.output_dir, str):
            raise ValueError("输出目录必须是字符串")

        if self.execution_intent == "library":
            return

        if not (self.export_markdown or self.export_docx or self.export_pdf):
            raise ValueError("至少需要选择一种导出格式（Markdown、Word 或 PDF）")

        if not (self.export_separate or self.export_merged):
            raise ValueError("至少需要选择一种输出方式（单独文件或合并导出）")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def get_default_presets() -> list[Preset]:
    return [Preset(**item) for item in DEFAULT_PRESETS_DATA]


class PresetManager:
    """管理预设文件的读取、持久化、另存与删除。"""

    def __init__(self, path: Path | None = None):
        self.path = path or default_presets_path()
        self.presets: list[Preset] = []
        self.last_selected_id: str = "daily_archive"
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            self.presets = get_default_presets()
            self.last_selected_id = self.presets[0].id
            self.save()
            return

        try:
            content = self.path.read_text(encoding="utf-8")
            raw = json.loads(content)
            if not isinstance(raw, dict) or "presets" not in raw:
                raise ValueError("预设配置格式错误，缺少 presets 节点")
            parsed_presets: list[Preset] = []
            for item in raw.get("presets", []):
                if not isinstance(item, dict):
                    continue
                # 白名单过滤
                cleaned = {}
                allowed_fields = {f.name for f in fields(Preset)}
                for k, v in item.items():
                    if k in allowed_fields:
                        cleaned[k] = v
                preset = Preset(**cleaned)
                preset.validate()
                parsed_presets.append(preset)

            if not parsed_presets:
                raise ValueError("预设列表为空或无有效配置")

            self.presets = parsed_presets
            last_id = str(raw.get("last_selected_id") or "")
            if any(p.id == last_id for p in self.presets):
                self.last_selected_id = last_id
            else:
                self.last_selected_id = self.presets[0].id

        except Exception as exc:
            logger.warning("读取预设配置失败：%s，保留损坏文件并恢复默认预设", exc)
            try:
                corrupted_backup = self.path.with_name(f"{self.path.name}.corrupted-{time.time_ns()}")
                shutil.copy2(self.path, corrupted_backup)
                logger.info("已备份损坏的预设文件至：%s", corrupted_backup)
            except Exception as backup_exc:
                logger.error("备份损坏预设文件失败：%s", backup_exc)

            self.presets = get_default_presets()
            self.last_selected_id = self.presets[0].id
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": PRESETS_VERSION,
            "last_selected_id": self.last_selected_id,
            "presets": [p.to_dict() for p in self.presets],
        }
        encoded = json.dumps(data, ensure_ascii=False, indent=2)

        # 原子写入：写临时文件后 replace
        temp_fd, temp_file_path = tempfile.mkstemp(dir=self.path.parent, prefix="presets_", suffix=".tmp")
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                f.write(encoded)
            for attempt in range(4):
                try:
                    os.replace(temp_file_path, self.path)
                    break
                except PermissionError as exc:
                    # Windows 同步程序或杀毒扫描可能短暂占用目标。
                    if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 3:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        except Exception:
            if os.path.exists(temp_file_path):
                try:
                    os.unlink(temp_file_path)
                except OSError:
                    pass
            raise

    def get_preset(self, preset_id: str) -> Preset | None:
        for p in self.presets:
            if p.id == preset_id:
                return p
        return None

    def save_or_update(self, preset: Preset) -> None:
        preset.validate()
        previous = list(self.presets)
        previous_selected = self.last_selected_id
        replaced = False
        for idx, existing in enumerate(self.presets):
            if existing.id == preset.id:
                self.presets[idx] = preset
                replaced = True
                break
        if not replaced:
            self.presets.append(preset)
        self.last_selected_id = preset.id
        try:
            self.save()
        except Exception:
            self.presets = previous
            self.last_selected_id = previous_selected
            raise

    def delete_preset(self, preset_id: str) -> bool:
        if len(self.presets) <= 1:
            raise ValueError("至少保留一套配置，不能删除全部配置")
        for idx, p in enumerate(self.presets):
            if p.id == preset_id:
                previous = list(self.presets)
                previous_selected = self.last_selected_id
                del self.presets[idx]
                if self.last_selected_id == preset_id:
                    self.last_selected_id = self.presets[0].id
                try:
                    self.save()
                except Exception:
                    self.presets = previous
                    self.last_selected_id = previous_selected
                    raise
                return True
        return False

    def select_preset(self, preset_id: str) -> Preset | None:
        preset = self.get_preset(preset_id)
        if preset is not None:
            previous_selected = self.last_selected_id
            self.last_selected_id = preset_id
            try:
                self.save()
            except Exception:
                self.last_selected_id = previous_selected
                raise
            return preset
        return None
