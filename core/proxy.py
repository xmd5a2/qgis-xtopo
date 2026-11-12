"""Прокси для сетевых запросов плагина.

Плагин качает данные (Geofabrik, DEM, Sonny) напрямую через urllib,
минуя настройки QGIS, поэтому прокси задаётся здесь: кнопка в шапке
панели открывает диалог, а этот модуль ставит глобальный opener urllib
и прокси Qt для запросов самого QGIS. Поддержаны HTTP и SOCKS5 —
рукопожатие SOCKS5 реализовано внутри, без сторонних зависимостей.
"""

import http.client
import socket
import struct
import urllib.request
from dataclasses import dataclass

from qgis.PyQt.QtCore import QSettings

from . import net

TYPES = ("socks5", "http")


class ProxyError(RuntimeError):
    """Прокси не смог выполнить запрос."""


@dataclass
class ProxyConfig:
    enabled: bool = False
    ptype: str = "socks5"
    host: str = ""
    port: int = 1080
    login: str = ""
    password: str = ""

    def address(self):
        return f"{self.host}:{self.port}"

    def http_url(self):
        """URL для ProxyHandler HTTP-прокси."""
        auth = ""
        if self.login:
            auth = self.login
            if self.password:
                auth += f":{self.password}"
            auth += "@"
        return f"http://{auth}{self.host}:{self.port}"


def _settings(store=None):
    return store if store is not None else QSettings("qgis-xtopo", "qgis-xtopo")


def load(store=None):
    """Конфигурация прокси из настроек."""
    settings = _settings(store)
    return ProxyConfig(
        enabled=settings.value("proxy/enabled", False, type=bool),
        ptype=settings.value("proxy/type", "socks5"),
        host=settings.value("proxy/host", ""),
        port=int(settings.value("proxy/port", 1080)),
        login=settings.value("proxy/login", ""),
        password=settings.value("proxy/password", ""),
    )


def save(config, store=None):
    """Сохраняет конфигурацию прокси."""
    settings = _settings(store)
    settings.setValue("proxy/enabled", config.enabled)
    settings.setValue("proxy/type", config.ptype)
    settings.setValue("proxy/host", config.host)
    settings.setValue("proxy/port", config.port)
    settings.setValue("proxy/login", config.login)
    settings.setValue("proxy/password", config.password)
    settings.sync()


def _recvn(sock, count):
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ProxyError("SOCKS5: соединение оборвано")
        data += chunk
    return data


def _socks5_connect(config, dest_host, dest_port, timeout):
    """TCP-сокет до dest через SOCKS5-рукопожатие (RFC 1928)."""
    timeout = timeout or net.DEFAULT_TIMEOUT
    sock = socket.create_connection((config.host, config.port),
                                    timeout=timeout)
    try:
        sock.settimeout(timeout)
        methods = [0x00]
        if config.login:
            methods.append(0x02)
        sock.sendall(b"\x05" + bytes([len(methods)]) + bytes(methods))
        _version, method = _recvn(sock, 2)
        if method == 0xFF:
            raise ProxyError("SOCKS5: прокси не принял метод авторизации")
        if method == 0x02:
            user = config.login.encode("utf-8")
            password = config.password.encode("utf-8")
            if len(user) > 255 or len(password) > 255:
                raise ProxyError("SOCKS5: логин или пароль длиннее 255 байт")
            sock.sendall(b"\x01" + bytes([len(user)]) + user
                         + bytes([len(password)]) + password)
            if _recvn(sock, 2)[1] != 0x00:
                raise ProxyError("SOCKS5: неверный логин или пароль")
        host = dest_host.encode("idna")
        if len(host) > 255:
            raise ProxyError("SOCKS5: имя хоста длиннее 255 байт")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host
                     + struct.pack(">H", dest_port))
        reply = _recvn(sock, 4)
        if reply[1] != 0x00:
            raise ProxyError(f"SOCKS5: прокси вернул ошибку {reply[1]}")
        atyp = reply[3]
        if atyp == 0x01:
            _recvn(sock, 4)
        elif atyp == 0x03:
            _recvn(sock, _recvn(sock, 1)[0])
        elif atyp == 0x04:
            _recvn(sock, 16)
        _recvn(sock, 2)  # порт привязки
        return sock
    except Exception:
        sock.close()
        raise


def probe(config, timeout=2.0):
    """Проверяет, что прокси принимает соединения.

    Для HTTP достаточно TCP: ответ прокси на запрос зависит от внешней
    сети, а значок должен показывать доступность самого прокси.  Для
    SOCKS5 выполняется приветствие (и авторизация), чтобы открытый порт
    другого сервиса не считался рабочим прокси.
    """
    if not config.enabled or not config.host:
        return False
    try:
        sock = socket.create_connection(
            (config.host, int(config.port)), timeout=timeout
        )
    except (OSError, ValueError):
        return False
    try:
        if config.ptype != "socks5":
            return True
        sock.settimeout(timeout)
        methods = [0x00]
        if config.login:
            methods.append(0x02)
        sock.sendall(b"\x05" + bytes([len(methods)]) + bytes(methods))
        version, method = _recvn(sock, 2)
        if version != 0x05:
            return False
        if method == 0xFF:
            return False
        if method == 0x02:
            user = config.login.encode("utf-8")
            password = config.password.encode("utf-8")
            if len(user) > 255 or len(password) > 255:
                return False
            sock.sendall(b"\x01" + bytes([len(user)]) + user
                         + bytes([len(password)]) + password)
            if _recvn(sock, 2)[1] != 0x00:
                return False
            return True
        return method == 0x00
    except (OSError, ProxyError, UnicodeError):
        return False
    finally:
        sock.close()


class _SocksHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port=None, timeout=None, proxy=None, **kwargs):
        super().__init__(host, port=port, timeout=timeout, **kwargs)
        self._proxy = proxy

    def connect(self):
        self.sock = _socks5_connect(self._proxy, self.host, self.port,
                                    self.timeout)


class _SocksHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port=None, timeout=None, proxy=None, **kwargs):
        super().__init__(host, port=port, timeout=timeout, **kwargs)
        self._proxy = proxy

    def connect(self):
        raw = _socks5_connect(self._proxy, self.host, self.port, self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class _Socks5Handler(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    """urllib-обработчик, соединяющийся через SOCKS5.

    Наследует HTTPHandler и HTTPSHandler, чтобы build_opener не добавлял
    стандартные обработчики: иначе запрос обрабатывает HTTPHandler и идёт
    напрямую, минуя прокси.
    """

    def __init__(self, config, debuglevel=0):
        urllib.request.AbstractHTTPHandler.__init__(self, debuglevel)
        self._config = config
        self._context = None

    def http_open(self, request):
        return self.do_open(self._http_class, request)

    def https_open(self, request):
        return self.do_open(self._https_class, request, context=self._context)

    def _http_class(self, host, **kwargs):
        return _SocksHTTPConnection(host, proxy=self._config, **kwargs)

    def _https_class(self, host, **kwargs):
        return _SocksHTTPSConnection(host, proxy=self._config, **kwargs)


def _handlers(config):
    if not config.enabled:
        return []
    if config.ptype == "socks5":
        return [_Socks5Handler(config)]
    if config.ptype == "http":
        return [urllib.request.ProxyHandler({
            "http": config.http_url(),
            "https": config.http_url(),
        })]
    return []


def _apply_qt_proxy(config):
    """Тот же прокси для сокетов Qt (слои WMS и прочие запросы QGIS)."""
    from qgis.PyQt.QtNetwork import QNetworkProxy
    proxy = QNetworkProxy()
    if config.enabled:
        kinds = {
            "http": QNetworkProxy.HttpProxy,
            "socks5": QNetworkProxy.Socks5Proxy,
        }
        kind = kinds.get(config.ptype)
        if kind is not None:
            proxy = QNetworkProxy(kind, config.host, int(config.port),
                                  config.login, config.password)
    QNetworkProxy.setApplicationProxy(proxy)


def apply(config=None):
    """Ставит прокси для urllib, opener источников DEM и сокетов Qt."""
    if config is None:
        config = load()
    handlers = _handlers(config)
    net.install(urllib.request.build_opener(*handlers))
    # dem_sources держит собственный opener (мультиадресный HTTPS).
    try:
        from . import dem_sources
        dem_sources._OPENER = net.guard(urllib.request.build_opener(
            dem_sources._MultiAddressHTTPSHandler(), *handlers))
    except Exception:  # noqa: BLE001 - без QGIS/Qt модуля не будет
        pass
    # Смена прокси — повод попробовать сеть заново.
    net.reset()
    try:
        _apply_qt_proxy(config)
    except Exception:  # noqa: BLE001 - Qt-сокеты необязательны для тестов
        pass
    return config
