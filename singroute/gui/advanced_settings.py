"""Dialog for connection fields that are rarely changed on OpenWrt."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from singroute.gui.brand import RoundedComboBox
from singroute.infrastructure.settings import AppSettings


class AdvancedSettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Дополнительные настройки")
        self.setMinimumWidth(570)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(settings.port)

        self.auth_combo = RoundedComboBox()
        self.auth_combo.addItem("Автоматически (ключи → пароль)", "auto")
        self.auth_combo.addItem("Только SSH-ключ", "key")
        self.auth_combo.addItem("Только пароль", "password")
        index = self.auth_combo.findData(settings.auth_mode)
        self.auth_combo.setCurrentIndex(max(0, index))

        self.identity_edit = QLineEdit(settings.identity_file)
        self.identity_edit.setPlaceholderText(
            "Пусто — SSH-agent, стандартные ключи и ~/.ssh/config"
        )
        browse_button = QPushButton("Выбрать…")
        browse_button.clicked.connect(self._browse_identity)
        identity_layout = QHBoxLayout()
        identity_layout.setContentsMargins(0, 0, 0, 0)
        identity_layout.addWidget(self.identity_edit, 1)
        identity_layout.addWidget(browse_button)
        identity_widget = QWidget()
        identity_widget.setLayout(identity_layout)

        self.config_path_edit = QLineEdit(settings.config_path)
        self.service_name_edit = QLineEdit(settings.service_name)
        self.check_updates_on_startup = QCheckBox(
            "Проверять обновления SingRoute при запуске"
        )
        self.check_updates_on_startup.setChecked(settings.check_updates_on_startup)

        form = QFormLayout()
        form.addRow("SSH-порт", self.port_spin)
        form.addRow("Авторизация", self.auth_combo)
        form.addRow("Приватный SSH-ключ", identity_widget)
        form.addRow("Конфиг на роутере", self.config_path_edit)
        form.addRow("Служба OpenWrt", self.service_name_edit)
        form.addRow("Обновления", self.check_updates_on_startup)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def apply_to(self, settings: AppSettings) -> None:
        settings.port = self.port_spin.value()
        settings.auth_mode = str(self.auth_combo.currentData())
        settings.identity_file = self.identity_edit.text().strip()
        settings.config_path = self.config_path_edit.text().strip()
        settings.service_name = self.service_name_edit.text().strip()
        settings.check_updates_on_startup = self.check_updates_on_startup.isChecked()

    def _browse_identity(self) -> None:
        start = self.identity_edit.text().strip() or str(Path.home() / ".ssh")
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите приватный SSH-ключ",
            start,
            "Все файлы (*)",
        )
        if path:
            self.identity_edit.setText(path)
