"""Предохранитель сетевых запросов.

Без сети плагин должен работать, а не ждать полный таймаут на каждом
запросе.  После первой ошибки соединения запросы короткое время падают
сразу; успешный запрос снимает запрет.  Opener также подставляет
таймаут, если вызывающий код его не задал: ``urlopen`` по умолчанию
ждёт бесконечно.
"""

import errno
import socket
import threading
import time
import urllib.error
import urllib.request

# Сколько секунд после сетевой ошибки не пытаться ходить в сеть.
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
_offline_until = 0.0
_last_error = ""


def is_network_error(exc):
    """Ошибка означает, что сеть не отвечает (а не ответ сервера)."""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, (socket.gaierror, TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, OSError):
        return exc.errno in _NETWORK_ERRNOS
    return False


def offline():
    """Сеть недавно не отвечала — запросы можно не пробовать."""
    with _lock:
        return time.monotonic() < _offline_until


def mark_offline(exc=None):
    global _offline_until, _last_error
    with _lock:
        _offline_until = time.monotonic() + OFFLINE_TTL
        _last_error = str(exc or "")


def mark_online():
    global _offline_until, _last_error
    with _lock:
        _offline_until = 0.0
        _last_error = ""


def reset():
    """Разрешает запросы заново (ручная проверка, смена прокси)."""
    mark_online()


def last_error():
    with _lock:
        return _last_error


class GuardedOpener:
    """Обёртка opener: таймаут по умолчанию и запрет после сбоя."""

    def __init__(self, opener):
        self._opener = opener

    def open(self, fullurl, data=None,
             timeout=_GLOBAL_DEFAULT_TIMEOUT):
        if offline():
            raise urllib.error.URLError(OFFLINE_REASON)
        if timeout is _GLOBAL_DEFAULT_TIMEOUT or timeout is None:
            timeout = DEFAULT_TIMEOUT
        try:
            response = self._opener.open(fullurl, data, timeout)
        except BaseException as exc:
            if is_network_error(exc):
                mark_offline(exc)
            raise
        mark_online()
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
