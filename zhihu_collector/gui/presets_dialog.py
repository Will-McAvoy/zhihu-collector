"""管理常用采集配置对话框。"""

from __future__ import annotations

import uuid
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from zhihu_collector.presets import Preset, PresetManager


class PresetsDialog(QDialog):
    def __init__(self, preset_manager: PresetManager, parent=None):
        super().__init__(parent)
        self.preset_manager = preset_manager
        self.setWindowTitle("管理采集配置")
        self.resize(450, 360)
        self._build()
        self._reload_list()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        tip = QLabel("在此管理自定义配置组合。内置模板被删除后可通过「恢复默认」重新载入。")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget)

        btn_row = QHBoxLayout()
        self.rename_btn = QPushButton("重命名")
        self.copy_btn = QPushButton("复制为新配置")
        self.delete_btn = QPushButton("删除")
        self.reset_btn = QPushButton("恢复默认")

        self.rename_btn.clicked.connect(self._rename)
        self.copy_btn.clicked.connect(self._copy)
        self.delete_btn.clicked.connect(self._delete)
        self.reset_btn.clicked.connect(self._reset)

        btn_row.addWidget(self.rename_btn)
        btn_row.addWidget(self.copy_btn)
        btn_row.addWidget(self.delete_btn)
        btn_row.addWidget(self.reset_btn)
        layout.addLayout(btn_row)

        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

    def _reload_list(self) -> None:
        self.list_widget.clear()
        for p in self.preset_manager.presets:
            item = QListWidgetItem(f"{p.name} ({p.id})")
            item.setData(Qt.ItemDataRole.UserRole, p.id)
            self.list_widget.addItem(item)
            if p.id == self.preset_manager.last_selected_id:
                self.list_widget.setCurrentItem(item)

    def _current_preset(self) -> Preset | None:
        item = self.list_widget.currentItem()
        if item is None:
            return None
        preset_id = item.data(Qt.ItemDataRole.UserRole)
        return self.preset_manager.get_preset(preset_id)

    def _rename(self) -> None:
        preset = self._current_preset()
        if preset is None:
            QMessageBox.information(self, "重命名", "请先选择一套配置。")
            return
        new_name, ok = QInputDialog.getText(self, "重命名配置", "请输入新的配置名称：", text=preset.name)
        if ok and new_name.strip():
            new_name = new_name.strip()
            if any(p.name == new_name and p.id != preset.id for p in self.preset_manager.presets):
                QMessageBox.warning(self, "重命名配置", f"已存在名为「{new_name}」的配置，请使用不同的名称。")
                return
            preset.name = new_name
            self.preset_manager.save_or_update(preset)
            self._reload_list()

    def _copy(self) -> None:
        preset = self._current_preset()
        if preset is None:
            QMessageBox.information(self, "复制配置", "请先选择要复制的配置。")
            return
        new_name, ok = QInputDialog.getText(
            self, "另存为新配置", "请输入新配置的名称：", text=f"{preset.name} - 副本"
        )
        if ok and new_name.strip():
            new_name = new_name.strip()
            if any(p.name == new_name for p in self.preset_manager.presets):
                QMessageBox.warning(self, "另存为新配置", f"已存在名为「{new_name}」的配置，请使用不同的名称。")
                return
            new_id = f"custom_{uuid.uuid4().hex[:8]}"
            d = preset.to_dict()
            d["id"] = new_id
            d["name"] = new_name
            new_preset = Preset(**d)
            self.preset_manager.save_or_update(new_preset)
            self._reload_list()

    def _delete(self) -> None:
        preset = self._current_preset()
        if preset is None:
            QMessageBox.information(self, "删除配置", "请先选择要删除的配置。")
            return
        if len(self.preset_manager.presets) <= 1:
            QMessageBox.warning(self, "删除配置", "至少保留一套配置，不能全部删除。")
            return
        ans = QMessageBox.question(
            self,
            "删除配置",
            f"确定要删除配置「{preset.name}」吗？\n删除配置不会影响已下载的内容、导出文件或任务历史。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.preset_manager.delete_preset(preset.id)
            self._reload_list()

    def _reset(self) -> None:
        ans = QMessageBox.question(
            self,
            "恢复默认配置",
            "确定要恢复内置默认配置（日常图文归档、合并阅读、小量试采）吗？\n自定义配置将被保留在末尾。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            from zhihu_collector.presets import get_default_presets

            existing_ids = {p.id for p in self.preset_manager.presets}
            for default_p in get_default_presets():
                if default_p.id not in existing_ids:
                    self.preset_manager.presets.append(default_p)
                else:
                    # 更新默认模板内容
                    for idx, p in enumerate(self.preset_manager.presets):
                        if p.id == default_p.id:
                            self.preset_manager.presets[idx] = default_p
            self.preset_manager.save()
            self._reload_list()
