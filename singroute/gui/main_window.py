"""Main portable Windows application window."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import threading
from typing import Any

from PySide6.QtCore import QSignalBlocker, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from singroute import __version__
from singroute.application.app_update import (
    ReleaseInfo,
    StagedUpdate,
    UpdateCheckResult,
    check_for_update,
    current_executable_path,
    is_portable_windows_build,
    launch_staged_update,
    stage_update,
)
from singroute.application.operation import MAX_CONFIG_BYTES
from singroute.application.router_update import (
    RouterUpdatePlan,
    RouterUpdateResult,
    apply_router_update,
    prepare_router_update,
    read_router_outbound_summary,
)
from singroute.application.router_connection import (
    RouterInfo,
    inspect_router,
)
from singroute.gui.advanced_settings import AdvancedSettingsDialog
from singroute.gui.worker import Worker
from singroute.infrastructure.credentials import (
    CredentialStore,
    CredentialTarget,
)
from singroute.infrastructure.settings import (
    AppSettings,
    PortableSettingsStore,
)
from singroute.infrastructure.ssh_router import (
    HostKeyMismatchError,
    SshOperationCancelled,
    SshRouterClient,
    SshRouterError,
    UnknownHostKeyError,
)


@dataclass(frozen=True)
class ConnectedRouter:
    client: SshRouterClient
    info: RouterInfo


class MainWindow(QMainWindow):
    operation_progress = Signal(str)

    def __init__(
        self,
        settings_store: PortableSettingsStore | None = None,
        credential_store: CredentialStore | None = None,
    ) -> None:
        super().__init__()
        self.settings_store = settings_store or PortableSettingsStore()
        self.credential_store = credential_store or CredentialStore()
        self.settings = self.settings_store.load()
        self._stored_credential_target = (
            self._credential_target(self.settings)
            if self.settings.remember_password
            else None
        )
        self.thread_pool = QThreadPool.globalInstance()
        self.update_plan: RouterUpdatePlan | None = None
        self._retry_action: Callable[[], None] | None = None
        self._cancel_event: threading.Event | None = None
        self._active_client: SshRouterClient | None = None
        self._connected_client: SshRouterClient | None = None
        self._active_worker: Worker | None = None
        self._background_update_worker: Worker | None = None
        self._success_handler: Callable[[object], None] | None = None
        self._available_release: ReleaseInfo | None = None
        self._status_before_app_update = ""
        self._busy = False
        self._operation_cancellable = False

        self.setWindowTitle("SingRoute")
        self.resize(self.settings.window_width, self.settings.window_height)
        self.setMinimumSize(780, 620)
        self._build_ui()
        self._build_shortcuts()
        self._load_fields()
        self._connect_field_changes()
        self._set_password_placeholder()
        self.operation_progress.connect(self._on_operation_progress)
        if self.settings.auto_connect:
            QTimer.singleShot(0, self._connect_router)
        if self.settings.check_updates_on_startup:
            QTimer.singleShot(1200, self._check_startup_update_when_idle)

    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(10)

        header = QHBoxLayout()
        header_text = QVBoxLayout()
        title = QLabel("SingRoute")
        title.setObjectName("title")
        subtitle = QLabel(
            "Безопасная синхронизация outbound sing-box на OpenWrt"
        )
        subtitle.setObjectName("subtitle")
        header_text.addWidget(title)
        header_text.addWidget(subtitle)
        self.advanced_button = QToolButton()
        self.advanced_button.setText("⚙")
        self.advanced_button.setToolTip("Дополнительные настройки")
        self.advanced_button.setObjectName("gearButton")
        self.advanced_button.setFixedSize(28, 28)
        self.advanced_button.clicked.connect(self._open_advanced_settings)
        header.addLayout(header_text, 1)
        self.version_label = QLabel(f"v{__version__}")
        self.version_label.setObjectName("mutedLabel")
        self.app_update_button = QPushButton("Проверить обновления")
        self.app_update_button.setToolTip("Проверить новые версии SingRoute")
        self.app_update_button.clicked.connect(
            lambda: self._check_app_update(silent=False)
        )
        header.addWidget(self.version_label)
        header.addWidget(self.app_update_button)
        root.addLayout(header)

        self.source_group = QGroupBox("2. Исходный конфиг HAPP / NekoBox")
        source_layout = QVBoxLayout(self.source_group)
        self.source_editor = QPlainTextEdit()
        self.source_editor.setReadOnly(True)
        self.source_editor.setPlaceholderText(
            "Используйте «Вставить из буфера» или «Открыть файл»"
        )
        self.source_editor.setMinimumHeight(90)
        self.source_editor.setMaximumHeight(145)
        source_layout.addWidget(self.source_editor)
        source_actions = QHBoxLayout()
        self.source_name_label = QLabel("Текст ещё не добавлен")
        self.source_name_label.setObjectName("mutedLabel")
        self.open_file_button = QPushButton("Открыть файл…")
        self.paste_button = QPushButton("Вставить из буфера")
        self.clear_source_button = QPushButton("Очистить")
        self.open_file_button.clicked.connect(self._load_source_file)
        self.paste_button.clicked.connect(self._paste_source)
        self.clear_source_button.clicked.connect(self._clear_source)
        source_actions.addWidget(self.source_name_label, 1)
        source_actions.addWidget(self.clear_source_button)
        source_actions.addWidget(self.paste_button)
        source_actions.addWidget(self.open_file_button)
        source_layout.addLayout(source_actions)
        self.connection_group = QGroupBox("1. Подключение")
        connection_layout = QVBoxLayout(self.connection_group)
        connection_row = QHBoxLayout()
        self.host_edit = QLineEdit()
        self.host_edit.setMinimumWidth(125)
        self.username_edit = QLineEdit()
        self.username_edit.setMaximumWidth(120)
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setClearButtonEnabled(True)
        self.password_edit.setMinimumWidth(135)
        self.remember_password_check = QCheckBox("Сохранить пароль")
        connection_row.addWidget(QLabel("Адрес"))
        connection_row.addWidget(self.host_edit, 2)
        connection_row.addWidget(QLabel("Пользователь"))
        connection_row.addWidget(self.username_edit, 1)
        connection_row.addWidget(QLabel("Пароль"))
        connection_row.addWidget(self.password_edit, 2)
        connection_row.addWidget(self.remember_password_check)
        connection_row.addWidget(self.advanced_button)
        connection_layout.addLayout(connection_row)

        connection_actions = QHBoxLayout()
        self.connection_hint = QLabel(self._advanced_summary())
        self.connection_hint.setObjectName("mutedLabel")
        self.connection_state_label = QLabel("Не подключено")
        self.connection_state_label.setObjectName("disconnectedLabel")
        self.auto_connect_check = QCheckBox("Подключаться автоматически")
        self.connect_button = QPushButton("Подключиться")
        self.connect_button.setObjectName("primaryButton")
        self.connect_button.clicked.connect(self._toggle_connection)
        connection_actions.addWidget(self.connection_hint, 1)
        connection_actions.addWidget(self.connection_state_label)
        connection_actions.addWidget(self.auto_connect_check)
        connection_actions.addWidget(self.connect_button)
        connection_layout.addLayout(connection_actions)
        root.addWidget(self.connection_group)
        root.addWidget(self.source_group)

        preview_group = QGroupBox("3. Предварительный просмотр outbound")
        preview_layout = QHBoxLayout(preview_group)
        old_layout = QVBoxLayout()
        old_layout.addWidget(QLabel("Сейчас на роутере"))
        self.old_preview = QPlainTextEdit()
        self.old_preview.setReadOnly(True)
        self.old_preview.setPlaceholderText(
            "Подключитесь, чтобы увидеть текущий outbound"
        )
        old_layout.addWidget(self.old_preview)
        new_layout = QVBoxLayout()
        new_layout.addWidget(QLabel("После обновления"))
        self.new_preview = QPlainTextEdit()
        self.new_preview.setReadOnly(True)
        self.new_preview.setPlaceholderText(
            "Загрузите исходный конфиг для сравнения"
        )
        new_layout.addWidget(self.new_preview)
        preview_layout.addLayout(old_layout, 1)
        preview_layout.addLayout(new_layout, 1)
        root.addWidget(preview_group, 1)

        action_row = QHBoxLayout()
        self.status_label = QLabel("Готово к работе")
        self.show_log_check = QCheckBox("Показать журнал")
        self.show_log_check.toggled.connect(self._set_log_visible)
        self.cancel_button = QPushButton("Отменить операцию")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self._cancel_operation)
        self.apply_button = QPushButton("Обновить роутер")
        self.apply_button.setObjectName("applyButton")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._apply_update)
        action_row.addWidget(self.status_label, 1)
        action_row.addWidget(self.show_log_check)
        action_row.addWidget(self.cancel_button)
        action_row.addWidget(self.apply_button)
        root.addLayout(action_row)

        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumHeight(115)
        self.log_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.log_edit.setPlaceholderText("Журнал операций")
        self.log_edit.setVisible(False)
        root.addWidget(self.log_edit)

        self.setCentralWidget(central)
        self.setStyleSheet(
            """
            QLabel#title { font-size: 22px; font-weight: 650; }
            QLabel#subtitle, QLabel#mutedLabel { color: #667085; }
            QLabel#connectedLabel { color: #15803d; font-weight: 600; }
            QLabel#disconnectedLabel { color: #b42318; font-weight: 600; }
            QGroupBox { font-weight: 600; padding-top: 11px; }
            QGroupBox > * { font-weight: 400; }
            QPushButton { padding: 6px 12px; }
            QToolButton#gearButton { font-size: 16px; padding: 0; }
            QPushButton#primaryButton { background: #2563eb; color: white; border-radius: 4px; }
            QPushButton#primaryButton:disabled { background: #94a3b8; }
            QPushButton#applyButton { background: #15803d; color: white; border-radius: 4px; font-weight: 600; }
            QPushButton#applyButton:disabled { background: #94a3b8; }
            QLineEdit { padding: 5px; }
            QPlainTextEdit { font-family: Consolas, monospace; }
            """
        )

    def _build_shortcuts(self) -> None:
        self.paste_shortcut = QShortcut(
            QKeySequence(QKeySequence.StandardKey.Paste),
            self,
        )
        self.paste_shortcut.activated.connect(self._paste_shortcut_activated)

    def _paste_shortcut_activated(self) -> None:
        focused_widget = QApplication.focusWidget()
        if isinstance(focused_widget, QLineEdit):
            focused_widget.paste()
            return
        self._paste_source()

    def _load_fields(self) -> None:
        self.host_edit.setText(self.settings.host)
        self.username_edit.setText(self.settings.username)
        self.remember_password_check.setChecked(self.settings.remember_password)
        self.auto_connect_check.setChecked(self.settings.auto_connect)

    def _connect_field_changes(self) -> None:
        self.host_edit.textChanged.connect(self._connection_fields_changed)
        self.username_edit.textChanged.connect(self._connection_fields_changed)
        self.password_edit.textChanged.connect(self._invalidate_preview)
        self.remember_password_check.toggled.connect(self._remember_password_changed)
        self.auto_connect_check.toggled.connect(self._auto_connect_changed)

    def _load_source_file(self) -> None:
        start = self.settings.last_import_directory or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Открыть конфиг HAPP или NekoBox",
            start,
            "JSON (*.json);;Все файлы (*)",
        )
        if not path:
            return
        try:
            if Path(path).stat().st_size > MAX_CONFIG_BYTES:
                raise ValueError(
                    "Файл превышает безопасный лимит "
                    f"{MAX_CONFIG_BYTES // (1024 * 1024)} МиБ."
                )
            content = Path(path).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError, ValueError) as error:
            QMessageBox.critical(self, "Ошибка файла", str(error))
            return
        self.settings.last_import_directory = str(Path(path).parent)
        self._set_source_content(content, f"Загружен файл: {Path(path).name}")

    def _paste_source(self) -> None:
        if self._busy:
            return
        text = QApplication.clipboard().text()
        if text.strip():
            self._set_source_content(text, "Конфиг вставлен из буфера")

    def _set_source_content(self, content: str, label: str) -> None:
        if (
            len(content) > MAX_CONFIG_BYTES
            or len(content.encode("utf-8")) > MAX_CONFIG_BYTES
        ):
            QMessageBox.warning(
                self,
                "Исходный конфиг",
                "Конфиг превышает безопасный лимит "
                f"{MAX_CONFIG_BYTES // (1024 * 1024)} МиБ.",
            )
            return
        self.source_editor.setPlainText(content)
        self.source_name_label.setText(label)
        self._invalidate_preview()
        self._prepare_preview_if_ready()

    def _clear_source(self) -> None:
        self.source_editor.clear()
        self.source_name_label.setText("Текст ещё не добавлен")
        self.old_preview.clear()
        self.new_preview.clear()
        self._invalidate_preview()
        self._refresh_preview_for_state()

    def _open_advanced_settings(self) -> None:
        dialog = AdvancedSettingsDialog(self.settings, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        previous = (
            self.settings.port,
            self.settings.auth_mode,
            self.settings.identity_file,
            self.settings.config_path,
            self.settings.service_name,
        )
        dialog.apply_to(self.settings)
        current = (
            self.settings.port,
            self.settings.auth_mode,
            self.settings.identity_file,
            self.settings.config_path,
            self.settings.service_name,
        )
        if current != previous:
            self._disconnect_router("Настройки подключения изменены")
        self.connection_hint.setText(self._advanced_summary())
        self._invalidate_preview()
        self._save_settings()

    def _check_startup_update_when_idle(self) -> None:
        self._check_app_update(silent=True)

    def _check_app_update(self, *, silent: bool) -> None:
        if silent:
            if self._background_update_worker is not None:
                return
            worker = Worker(lambda: check_for_update(__version__))
            self._background_update_worker = worker
            worker.signals.finished.connect(self._background_update_check_finished)
            worker.signals.failed.connect(self._background_update_check_failed)
            self.thread_pool.start(worker)
            return
        if self._busy:
            QMessageBox.information(
                self,
                "Обновление SingRoute",
                "Дождитесь завершения текущей операции и повторите проверку.",
            )
            return
        if self._available_release is not None:
            self._offer_app_update(self._available_release)
            return

        self._status_before_app_update = self.status_label.text()
        self._run_worker(
            lambda: check_for_update(__version__),
            lambda result: self._app_update_check_finished(result, silent=False),
            "Проверяю обновления SingRoute…",
            retry=lambda: self._check_app_update(silent=False),
            cancellable=False,
        )

    @Slot(object)
    def _background_update_check_finished(self, result: object) -> None:
        self._background_update_worker = None
        try:
            self._app_update_check_finished(result, silent=True)
        except Exception as error:
            self._background_update_check_failed(error)

    @Slot(object)
    def _background_update_check_failed(self, error: object) -> None:
        self._background_update_worker = None
        self._append_log(f"Не удалось проверить обновления: {error}")

    def _app_update_check_finished(self, result: object, *, silent: bool) -> None:
        if not isinstance(result, UpdateCheckResult):
            raise TypeError("Некорректный результат проверки обновлений")
        if result.update_available:
            self._available_release = result.latest_release
            self.app_update_button.setText(
                f"Установить v{result.latest_release.version}"
            )
            self._append_log(
                f"Доступно обновление SingRoute v{result.latest_release.version}."
            )
            if not silent:
                self.status_label.setText(
                    f"Доступно обновление SingRoute v{result.latest_release.version}"
                )
                self._offer_app_update(result.latest_release)
            return

        if not silent:
            self.status_label.setText(self._status_before_app_update)
            QMessageBox.information(
                self,
                "Обновление SingRoute",
                f"Установлена актуальная версия SingRoute v{__version__}.",
            )

    def _offer_app_update(self, release: ReleaseInfo) -> None:
        if not is_portable_windows_build():
            QMessageBox.information(
                self,
                "Доступно обновление SingRoute",
                f"Доступна версия v{release.version}. Автоматическая установка "
                "работает в portable SingRoute.exe; среда разработки не изменена.",
            )
            return
        answer = _ask_yes_no(
            self,
            "Доступно обновление SingRoute",
            f"Доступна версия v{release.version}.\n\n"
            "Установить обновление? После загрузки SingRoute автоматически "
            "закроется, обновится и запустится снова.",
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._download_app_update(release)

    def _download_app_update(self, release: ReleaseInfo) -> None:
        self._run_worker(
            lambda: stage_update(release, current_executable_path()),
            self._app_update_downloaded,
            f"Загружаю SingRoute v{release.version}…",
            retry=lambda: self._download_app_update(release),
            cancellable=False,
        )

    def _app_update_downloaded(self, result: object) -> None:
        if not isinstance(result, StagedUpdate):
            raise TypeError("Некорректный результат загрузки обновления")
        application = QApplication.instance()
        if application is None:
            raise RuntimeError("Экземпляр приложения уже завершён.")
        launch_staged_update(result)
        self._append_log(
            f"Обновление v{result.release.version} проверено; перезапускаю SingRoute."
        )
        self.close()
        application.quit()

    def _connection_fields_changed(self) -> None:
        if self._connected_client is not None:
            self._disconnect_router("Параметры подключения изменены")
        self._invalidate_preview()

    def _auto_connect_changed(self, checked: bool) -> None:
        if not checked:
            self.settings.auto_connect = False
            self._save_settings()
        elif self._connected_client is not None:
            self.settings.auto_connect = True
            self._save_settings()

    def _advanced_summary(self) -> str:
        auth_labels = {
            "auto": "авто",
            "key": "ключ",
            "password": "пароль",
        }
        return (
            f"Порт {self.settings.port} · {auth_labels.get(self.settings.auth_mode, 'авто')} · "
            f"{self.settings.config_path}"
        )

    def _remember_password_changed(self, checked: bool) -> None:
        self._set_password_placeholder()
        self._invalidate_preview()
        if not checked:
            self.password_edit.clear()

    def _set_password_placeholder(self) -> None:
        if self.remember_password_check.isChecked():
            self.password_edit.setPlaceholderText("Пусто = пароль из Windows")
        else:
            self.password_edit.setPlaceholderText("Не сохраняется")

    def _invalidate_preview(self, *_: object) -> None:
        if self.update_plan is not None:
            self.update_plan = None
            self.apply_button.setEnabled(False)
            self.status_label.setText(
                "Данные изменены — предварительный просмотр будет обновлён автоматически"
            )

    def _toggle_connection(self) -> None:
        if self._connected_client is not None:
            self._disconnect_router("Отключено")
        else:
            self._connect_router()

    def _connect_router(self) -> None:
        if self._busy or self._connected_client is not None:
            return
        if not self._validate_fields(require_source=False):
            return
        if not self._save_settings():
            return

        def action() -> ConnectedRouter:
            client = self._new_client()
            try:
                client.connect()
                info = inspect_router(
                    client,
                    self.settings.config_path,
                    self.settings.service_name,
                    self.operation_progress.emit,
                )
                return ConnectedRouter(client, info)
            except Exception:
                client.close()
                raise

        self._run_worker(
            action,
            self._connection_finished,
            "Подключаюсь…",
            retry=self._connect_router,
            cancellable=True,
        )

    def _connection_finished(self, result: object) -> None:
        if not isinstance(result, ConnectedRouter):
            raise TypeError("Некорректный результат подключения")
        self._connected_client = result.client
        self.connection_state_label.setText("Подключено")
        self.connection_state_label.setObjectName("connectedLabel")
        self.connection_state_label.style().unpolish(self.connection_state_label)
        self.connection_state_label.style().polish(self.connection_state_label)
        self.connect_button.setText("Отключиться")
        self.settings.auto_connect = self.auto_connect_check.isChecked()
        self._save_settings()
        message = (
            f"Подключение успешно: {result.info.openwrt_release}; "
            f"sing-box: {result.info.sing_box_path}"
        )
        self._append_log(message)
        self.status_label.setText(message)
        self._refresh_preview_for_state()

    def _disconnect_router(self, reason: str = "Отключено") -> None:
        client = self._connected_client
        self._connected_client = None
        if client is not None:
            client.close()
        self.connection_state_label.setText("Не подключено")
        self.connection_state_label.setObjectName("disconnectedLabel")
        self.connection_state_label.style().unpolish(self.connection_state_label)
        self.connection_state_label.style().polish(self.connection_state_label)
        self.connect_button.setText("Подключиться")
        self.update_plan = None
        self.apply_button.setEnabled(False)
        self.old_preview.clear()
        self.new_preview.clear()
        self.status_label.setText(reason)

    def _refresh_preview_for_state(self) -> None:
        if self._connected_client is None or self._busy:
            return
        if self.source_editor.toPlainText().strip():
            self._prepare_preview_if_ready()
        else:
            self._load_current_router_preview()

    def _load_current_router_preview(self) -> None:
        client = self._connected_client
        if client is None or self._busy:
            return

        def action() -> dict[str, Any]:
            self.operation_progress.emit("Читаю текущий outbound роутера…")
            self._active_client = client
            return read_router_outbound_summary(
                client,
                config_path=self.settings.config_path,
            )

        self._run_worker(
            action,
            self._current_router_preview_finished,
            "Загружаю текущий outbound роутера…",
            retry=self._refresh_preview_for_state,
            cancellable=True,
        )

    def _current_router_preview_finished(self, result: object) -> None:
        if not isinstance(result, dict):
            raise TypeError("Некорректный текущий outbound роутера")
        self.update_plan = None
        self.old_preview.setPlainText(
            json.dumps(result, ensure_ascii=False, indent=2)
        )
        self.new_preview.clear()
        self.apply_button.setEnabled(False)
        self.status_label.setText(
            "Текущий outbound загружен — добавьте исходный конфиг"
        )
        self._append_log("Текущий outbound роутера загружен.")

    def _prepare_preview_if_ready(self) -> None:
        if self._connected_client is None or self._busy:
            return
        if not self.source_editor.toPlainText().strip():
            return
        self._prepare_preview()

    def _prepare_preview(self) -> None:
        client = self._connected_client
        if client is None:
            return
        if not self._validate_fields(require_source=True):
            return
        imported_text = self.source_editor.toPlainText()
        def action() -> RouterUpdatePlan:
            self.operation_progress.emit("Читаю текущий конфиг роутера…")
            self._active_client = client
            return prepare_router_update(
                imported_text,
                client,
                config_path=self.settings.config_path,
            )

        self._run_worker(
            action,
            self._preview_finished,
            "Готовлю предварительный просмотр…",
            retry=self._prepare_preview_if_ready,
            cancellable=True,
        )

    def _preview_finished(self, result: object) -> None:
        if not isinstance(result, RouterUpdatePlan):
            raise TypeError("Некорректный результат подготовки превью")
        has_changes = (
            result.preview["old_outbound"] != result.preview["new_outbound"]
        )
        self.update_plan = result if has_changes else None
        self.old_preview.setPlainText(
            json.dumps(result.preview["old_outbound"], ensure_ascii=False, indent=2)
        )
        self.new_preview.setPlainText(
            json.dumps(result.preview["new_outbound"], ensure_ascii=False, indent=2)
        )
        self.apply_button.setEnabled(has_changes)
        if has_changes:
            self.status_label.setText("Изменения подготовлены — проверьте превью")
            self._append_log("Превью подготовлено; конфиг роутера ещё не изменён.")
        else:
            self.status_label.setText("Конфиг роутера уже соответствует исходному")
            self._append_log("Проверка завершена: изменения не требуются.")

    def _apply_update(self) -> None:
        client = self._connected_client
        if self.update_plan is None or client is None:
            return
        answer = _ask_yes_no(
            self,
            "Подтверждение обновления",
            "Новый конфиг пройдёт проверку, затем будет создана временная "
            "резервная копия и перезапущен sing-box. После успешной проверки "
            "копия удалится; при ошибке выполнится откат.\n\n"
            "Продолжить?",
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        plan = self.update_plan
        if not self._save_settings():
            return

        def action() -> RouterUpdateResult:
            self.operation_progress.emit("Начинаю безопасное обновление роутера…")
            self._active_client = client
            return apply_router_update(
                plan,
                client,
                service_name=self.settings.service_name,
            )

        self._run_worker(
            action,
            self._update_finished,
            "Проверяю и обновляю конфиг роутера…",
            retry=self._apply_update,
            cancellable=False,
        )

    def _update_finished(self, result: object) -> None:
        if not isinstance(result, RouterUpdateResult):
            raise TypeError("Некорректный результат обновления")
        self._append_log(result.message)
        if result.backup_deleted:
            self._append_log("Временная резервная копия удалена с роутера.")
        elif result.backup_path:
            self._append_log(f"Резервная копия оставлена: {result.backup_path}")
        self.status_label.setText(result.message)
        if result.success:
            self.apply_button.setEnabled(False)
            self.update_plan = None
            self.old_preview.clear()
            self.new_preview.clear()
            if result.backup_path and not result.backup_deleted:
                self.show_log_check.setChecked(True)
                QMessageBox.warning(
                    self,
                    "Конфиг обновлён с предупреждением",
                    _result_details(result),
                )
            else:
                QMessageBox.information(self, "Готово", result.message)
            self._prepare_preview_if_ready()
        else:
            self.show_log_check.setChecked(True)
            QMessageBox.warning(
                self,
                "Обновление не выполнено",
                _result_details(result),
            )

    def _run_worker(
        self,
        action: Callable[[], Any],
        on_success: Callable[[object], None],
        status: str,
        *,
        retry: Callable[[], None],
        cancellable: bool,
    ) -> None:
        if self._busy:
            return
        self._retry_action = retry
        self._success_handler = on_success
        self._cancel_event = threading.Event()
        self._operation_cancellable = cancellable
        self._set_busy(True, status)
        worker = Worker(action)
        self._active_worker = worker
        worker.signals.finished.connect(self._worker_finished)
        worker.signals.failed.connect(self._worker_failed)
        self.thread_pool.start(worker)

    @Slot(object)
    def _worker_finished(self, result: object) -> None:
        on_success = self._success_handler
        self._success_handler = None
        self._active_worker = None
        self._active_client = None
        self._set_busy(False)
        self._retry_action = None
        if on_success is None:
            self._show_error(RuntimeError("Результат фоновой операции потерян."))
            return
        try:
            on_success(result)
        except Exception as error:
            self._show_error(error)

    @Slot(object)
    def _worker_failed(self, error: object) -> None:
        failed_client = self._active_client
        self._success_handler = None
        self._active_worker = None
        self._active_client = None
        self._set_busy(False)
        if isinstance(error, SshOperationCancelled):
            if failed_client is self._connected_client:
                self._disconnect_router("Подключение прервано")
            self._retry_action = None
            self.status_label.setText("Операция отменена")
            self._append_log("Операция отменена пользователем.")
            return
        if isinstance(error, SshRouterError) and not isinstance(
            error, (UnknownHostKeyError, HostKeyMismatchError)
        ):
            if failed_client is self._connected_client:
                self._disconnect_router("SSH-соединение потеряно")
        if isinstance(error, UnknownHostKeyError):
            retry = self._retry_action
            self._retry_action = None
            answer = _ask_yes_no(
                self,
                "Новый SSH-ключ роутера",
                f"Роутер {error.info.host}:{error.info.port} предъявил ключ:\n\n"
                f"{error.info.algorithm}\n{error.info.fingerprint}\n\n"
                "Сверьте fingerprint с роутером. Доверять этому ключу?",
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.settings.trusted_host_keys[
                    f"{error.info.host}:{error.info.port}"
                ] = error.info.trust_token
                self._save_settings()
                self._append_log(
                    f"SSH fingerprint подтверждён: {error.info.fingerprint}"
                )
                if retry is not None:
                    retry()
            return
        self._retry_action = None
        self._show_error(error)

    def _cancel_operation(self) -> None:
        if not self._busy or not self._operation_cancellable:
            return
        self.cancel_button.setEnabled(False)
        self.status_label.setText("Отменяю операцию…")
        if self._cancel_event is not None:
            self._cancel_event.set()
        if self._active_client is not None:
            self._active_client.close()

    def _show_error(self, error: object) -> None:
        message = str(error)
        self.status_label.setText("Ошибка")
        self.show_log_check.setChecked(True)
        self._append_log(f"Ошибка: {message}")
        title = (
            "SSH-ключ изменился"
            if isinstance(error, HostKeyMismatchError)
            else "Ошибка"
        )
        QMessageBox.critical(self, title, message)

    def _set_log_visible(self, visible: bool) -> None:
        self.log_edit.setVisible(visible)

    def _set_busy(self, busy: bool, status: str | None = None) -> None:
        self._busy = busy
        self.source_group.setEnabled(not busy)
        self.connection_group.setEnabled(not busy)
        self.advanced_button.setEnabled(not busy)
        self.app_update_button.setEnabled(not busy)
        self.cancel_button.setVisible(busy and self._operation_cancellable)
        self.cancel_button.setEnabled(busy and self._operation_cancellable)
        self.apply_button.setEnabled(
            not busy
            and self.update_plan is not None
            and self._connected_client is not None
        )
        if not busy:
            self._cancel_event = None
            self._operation_cancellable = False
        if status is not None:
            self.status_label.setText(status)

    def _new_client(self) -> SshRouterClient:
        current = self._settings_from_fields()
        password = self.password_edit.text()
        if not password and current.remember_password:
            try:
                password = self.credential_store.get_password(
                    self._credential_target(current)
                ) or ""
            except Exception as error:
                raise SshRouterError(
                    f"Не удалось прочитать сохранённый пароль Windows: {error}"
                ) from error
        client = SshRouterClient(
            host=current.host,
            user=current.username,
            port=current.port,
            identity_file=current.identity_file or None,
            password=password or None,
            key_passphrase=password or None,
            auth_mode=current.auth_mode,
            trusted_host_key=current.trusted_host_keys.get(current.host_key_id()),
            cancel_event=self._cancel_event,
            progress_callback=self.operation_progress.emit,
        )
        self._active_client = client
        return client

    def _validate_fields(self, *, require_source: bool) -> bool:
        source_text = self.source_editor.toPlainText()
        if require_source and not source_text.strip():
            QMessageBox.warning(
                self,
                "Исходный конфиг",
                "Вставьте текст конфига или откройте JSON-файл.",
            )
            return False
        if require_source and (
            len(source_text) > MAX_CONFIG_BYTES
            or len(source_text.encode("utf-8")) > MAX_CONFIG_BYTES
        ):
            QMessageBox.warning(
                self,
                "Исходный конфиг",
                "Конфиг превышает безопасный лимит "
                f"{MAX_CONFIG_BYTES // (1024 * 1024)} МиБ.",
            )
            return False
        if not self.host_edit.text().strip():
            QMessageBox.warning(self, "Подключение", "Укажите адрес роутера.")
            return False
        if not self.username_edit.text().strip():
            QMessageBox.warning(self, "Подключение", "Укажите SSH-пользователя.")
            return False
        if not self.settings.config_path.startswith("/"):
            QMessageBox.warning(
                self,
                "Путь к конфигу",
                "В дополнительных настройках укажите абсолютный путь OpenWrt.",
            )
            return False
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.settings.service_name):
            QMessageBox.warning(
                self,
                "Служба",
                "Имя службы в дополнительных настройках содержит недопустимые символы.",
            )
            return False
        if self.settings.identity_file and not Path(self.settings.identity_file).is_file():
            QMessageBox.warning(
                self,
                "SSH-ключ",
                "Файл SSH-ключа из дополнительных настроек не найден.",
            )
            return False
        return True

    def _settings_from_fields(self) -> AppSettings:
        return AppSettings(
            host=self.host_edit.text().strip(),
            port=self.settings.port,
            username=self.username_edit.text().strip(),
            config_path=self.settings.config_path,
            service_name=self.settings.service_name,
            auth_mode=self.settings.auth_mode,
            identity_file=self.settings.identity_file,
            remember_password=self.remember_password_check.isChecked(),
            auto_connect=self.settings.auto_connect,
            check_updates_on_startup=self.settings.check_updates_on_startup,
            last_import_directory=self.settings.last_import_directory,
            window_width=self.width(),
            window_height=self.height(),
            trusted_host_keys=dict(self.settings.trusted_host_keys),
        )

    def _save_settings(self) -> bool:
        current = self._settings_from_fields()
        target = self._credential_target(current)
        previous_target = self._stored_credential_target
        entered_password = self.password_edit.text()
        try:
            self.settings_store.save(current)
        except Exception as error:
            self._append_log(f"Настройки не сохранены: {error}")
            return False

        self.settings = current
        try:
            if current.remember_password and entered_password:
                self.credential_store.set_password(target, entered_password)
                with QSignalBlocker(self.password_edit):
                    self.password_edit.clear()
            elif not current.remember_password:
                self.credential_store.delete_password(target)

            if previous_target is not None and previous_target != target:
                self.credential_store.delete_password(previous_target)
        except Exception as error:
            self._append_log(f"Не удалось обновить пароль Windows: {error}")
            return False

        self._stored_credential_target = target if current.remember_password else None
        self.connection_hint.setText(self._advanced_summary())
        self._set_password_placeholder()
        return True

    @staticmethod
    def _credential_target(settings: AppSettings) -> CredentialTarget:
        return CredentialTarget(settings.host, settings.port, settings.username)

    @Slot(str)
    def _on_operation_progress(self, message: str) -> None:
        self.status_label.setText(message)
        self._append_log(message)

    def _append_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_edit.appendPlainText(f"[{timestamp}] {message}")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._busy:
            if not self._operation_cancellable:
                QMessageBox.information(
                    self,
                    "Обновление выполняется",
                    "Безопасное обновление уже началось. Дождитесь его завершения, "
                    "чтобы не оставить роутер с частично применённым конфигом.",
                )
                event.ignore()
                return
            answer = _ask_yes_no(
                self,
                "Прервать операцию",
                "Прервать текущую проверку и закрыть приложение?",
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._cancel_operation()
            event.accept()
            return
        if self._connected_client is not None:
            self._connected_client.close()
            self._connected_client = None
        self._save_settings()
        event.accept()


def _ask_yes_no(
    parent: QWidget,
    title: str,
    text: str,
    default_button: QMessageBox.StandardButton,
) -> QMessageBox.StandardButton:
    dialog = QMessageBox(parent)
    dialog.setIcon(QMessageBox.Icon.Question)
    dialog.setWindowTitle(title)
    dialog.setText(text)
    dialog.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    )
    dialog.setDefaultButton(default_button)
    dialog.setEscapeButton(QMessageBox.StandardButton.No)
    yes_button = dialog.button(QMessageBox.StandardButton.Yes)
    no_button = dialog.button(QMessageBox.StandardButton.No)
    if yes_button is not None:
        yes_button.setText("Да")
    if no_button is not None:
        no_button.setText("Нет")
    return QMessageBox.StandardButton(dialog.exec())


def _result_details(result: RouterUpdateResult) -> str:
    details = [result.message]
    if result.backup_path and not result.backup_deleted:
        details.append(f"Резервная копия оставлена: {result.backup_path}")
    if result.validation_result.exit_code != 0:
        output = (
            result.validation_result.stderr.strip()
            or result.validation_result.stdout.strip()
        )
        if output:
            details.append(f"Проверка sing-box: {output}")
    return "\n\n".join(details)
