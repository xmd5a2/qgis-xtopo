"""Диалог настройки прокси."""

from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLineEdit,
    QSpinBox, QVBoxLayout, QWidget,
)

from ..core import i18n, proxy


class ProxyDialog(QDialog):
    """Конфигурация прокси: тип, адрес, авторизация."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(i18n.tr("Прокси"))
        self.enabled = QCheckBox(i18n.tr("Использовать прокси"))
        self.ptype = QComboBox()
        self.ptype.addItem("SOCKS5", "socks5")
        self.ptype.addItem("HTTP", "http")
        self.host = QLineEdit()
        self.host.setPlaceholderText("127.0.0.1")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(1080)
        self.login = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)

        form = QFormLayout()
        form.addRow(self.enabled)
        form.addRow(i18n.tr("Тип"), self.ptype)
        form.addRow(i18n.tr("Хост"), self.host)
        form.addRow(i18n.tr("Порт"), self.port)
        form.addRow(i18n.tr("Логин"), self.login)
        form.addRow(i18n.tr("Пароль"), self.password)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self._show_config(proxy.load())
        self.enabled.toggled.connect(self._toggle_fields)
        self._toggle_fields(self.enabled.isChecked())

    def _toggle_fields(self, checked):
        for widget in (self.ptype, self.host, self.port, self.login,
                       self.password):
            widget.setEnabled(checked)

    def _show_config(self, config):
        self.enabled.setChecked(config.enabled)
        index = self.ptype.findData(config.ptype)
        self.ptype.setCurrentIndex(max(0, index))
        self.host.setText(config.host)
        self.port.setValue(max(1, int(config.port or 1080)))
        self.login.setText(config.login)
        self.password.setText(config.password)

    def config(self):
        return proxy.ProxyConfig(
            enabled=self.enabled.isChecked(),
            ptype=self.ptype.currentData(),
            host=self.host.text().strip(),
            port=self.port.value(),
            login=self.login.text().strip(),
            password=self.password.text(),
        )

    def accept(self):
        config = self.config()
        if config.enabled and (not config.host or not config.port):
            self.host.setFocus()
            return
        super().accept()
