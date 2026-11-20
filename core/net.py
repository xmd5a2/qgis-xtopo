"""Предохранитель сетевых запросов.

Без сети плагин должен работать, а не ждать полный таймаут на каждом
запросе.  После первой ошибки соединения запросы к этому хосту
короткое время падают сразу; успешный запрос снимает запрет.  Запрет
делается по хостам: сбой одного сервера не должен останавливать
остальные — в том числе локальные адреса.  Opener также подставляет
таймаут, если вызывающий код его не задал: ``urlopen`` по умолчанию
ждёт бесконечно.
"""

import errno
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# Сколько секунд после сетевой ошибки не пытаться ходить к этому хосту.
OFFLINE_TTL = 20.0

# Таймаут для запросов, где он не задан явно.
DEFAULT_TIMEOUT = 30.0

OFFLINE_REASON = "сеть недоступна"

_GLOBAL_DEFAULT_TIMEOUT = getattr(
    socket, "_GLOBAL_DEFAULT_TIMEOUT", object()
)

_NETWORK_ERRNOS = {
    errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTDOWN,
    errno.EHOSTUNREACH, errno.ECONNREFUSED, errno.ECONNRESET,
    errno.ETIMEDOUT, errno.EPIPE,
}

_lock = threading.Lock()
# Хост → время, до которого запросы к нему запрещены.  Пустой ключ —
# общий запрет (его ставит mark_offline() без хоста).
_offline_until = {}
_last_error = ""


def _host_of(url):
    """Хост из адреса (пустая строка — у адреса нет хоста)."""
    if hasattr(url, "full_url"):
        url = url.full_url
    try:
        return urllib.parse.urlsplit(str(url)).hostname or ""
    except ValueError:
        return ""


def is_network_error(exc):
    """Ошибка означает, что сеть не отвечает (а не ответ сервера)."""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    if isinstance(exc, urllib.error.URLError):
        # Неизвестная схема адреса — это ошибка адреса, а не сети.
        return "unknown url type" not in str(getattr(exc, "reason", exc))
    if isinstance(exc, (socket.gaierror, TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, OSError):
        return exc.errno in _NETWORK_ERRNOS
    return False


def offline(url=None):
    """Сеть недавно не отвечала — запросы можно не пробовать.

    Без адреса — есть ли хоть один недавний сбой, с адресом — сбой
    именно этого хоста.
    """
    with _lock:
        now = time.monotonic()
        if url is None:
            return any(deadline > now for deadline in _offline_until.values())
        if _offline_until.get("", 0.0) > now:
            return True
        return _offline_until.get(_host_of(url), 0.0) > now


def mark_offline(exc=None, host=None):
    """Запретить запросы: к хосту или (без хоста) ко всем сразу."""
    global _last_error
    with _lock:
        _last_error = str(exc or "")
        deadline = time.monotonic() + OFFLINE_TTL
        if host:
            _offline_until[host] = deadline
        else:
            _offline_until.clear()
            _offline_until[""] = deadline


def mark_online(host=None):
    """Снять запрет: для хоста или для всех."""
    global _last_error
    with _lock:
        if host:
            _offline_until.pop(host, None)
        else:
            _offline_until.clear()
        if not _offline_until:
            _last_error = ""


def reset():
    """Разрешает запросы заново (ручная проверка, смена прокси)."""
    mark_online()


class GuardedOpener:
    """Обёртка opener: таймаут по умолчанию и запрет после сбоя."""

    def __init__(self, opener):
        self._opener = opener

    def open(self, fullurl, data=None,
             timeout=_GLOBAL_DEFAULT_TIMEOUT):
        host = _host_of(fullurl)
        if offline(fullurl):
            raise urllib.error.URLError(OFFLINE_REASON)
        if timeout is _GLOBAL_DEFAULT_TIMEOUT or timeout is None:
            timeout = DEFAULT_TIMEOUT
        try:
            response = self._opener.open(fullurl, data, timeout)
        except BaseException as exc:
            if is_network_error(exc):
                mark_offline(exc, host)
            raise
        mark_online(host)
        return response

    def __getattr__(self, name):
        return getattr(self._opener, name)


def guard(opener):
    if isinstance(opener, GuardedOpener):
        return opener
    return GuardedOpener(opener)


def install(opener):
    """Ставит opener с предохранителем глобальным для urllib."""
    guarded = guard(opener)
    urllib.request.install_opener(guarded)
    return guarded
