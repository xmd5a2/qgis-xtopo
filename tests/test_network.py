"""Сетевой предохранитель: без сети запросы не должны висеть.

После первой ошибки соединения opener короткое время падает сразу, без
обращения к сети; успешный запрос запрет снимает. Ответ сервера с
ошибкой (HTTP 4xx/5xx) предохранитель не считает сбоем сети.
"""

import socket
import unittest
import urllib.error
from unittest import mock

from qgis_xtopo.core import net


class _FakeOpener:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def open(self, *args, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class GuardTest(unittest.TestCase):
    def setUp(self):
        net.reset()

    def tearDown(self):
        net.reset()

    def test_network_error_short_circuits(self):
        guarded = net.guard(_FakeOpener(error=urllib.error.URLError("offline")))
        with self.assertRaises(urllib.error.URLError):
            guarded.open("http://example.org/")
        with self.assertRaises(urllib.error.URLError):
            guarded.open("http://example.org/")
        self.assertEqual(guarded._opener.calls, 1)
        self.assertTrue(net.offline())

    def test_failure_blocks_only_its_host(self):
        """Сбой одного сервера не должен останавливать остальные."""
        guarded = net.guard(_FakeOpener(error=urllib.error.URLError("offline")))
        with self.assertRaises(urllib.error.URLError):
            guarded.open("http://example.org/")
        self.assertTrue(net.offline("http://example.org/"))
        self.assertFalse(net.offline("http://localhost:8080/"))
        # Другой хост запрос проходит нормально.
        other = net.guard(_FakeOpener(result=object()))
        other.open("http://localhost:8080/")
        self.assertEqual(other._opener.calls, 1)

    def test_unknown_scheme_is_not_network_failure(self):
        error = urllib.error.URLError("unknown url type: 'ftp'")
        self.assertFalse(net.is_network_error(error))
        guarded = net.guard(_FakeOpener(error=error))
        with self.assertRaises(urllib.error.URLError):
            guarded.open("ftp://example.org/")
        self.assertFalse(net.offline())

    def test_offline_state_can_be_cleared(self):
        net.mark_offline()
        self.assertTrue(net.offline())
        net.mark_online()
        self.assertFalse(net.offline())
        ok = net.guard(_FakeOpener(result=object()))
        ok.open("http://example.org/")

    def test_offline_ttl_expires(self):
        with mock.patch.object(net, "OFFLINE_TTL", -1.0):
            net.mark_offline()
        self.assertFalse(net.offline())

    def test_reset_allows_requests(self):
        guarded = net.guard(_FakeOpener(error=urllib.error.URLError("offline")))
        with self.assertRaises(urllib.error.URLError):
            guarded.open("http://example.org/")
        net.reset()
        # После сброса запрос доходит до opener (и снова падает —
        # это его собственная ошибка, предохранитель не мешает).
        with self.assertRaises(urllib.error.URLError):
            guarded.open("http://example.org/")
        self.assertEqual(guarded._opener.calls, 2)

    def test_http_error_is_not_network_failure(self):
        error = urllib.error.HTTPError(
            "http://example.org/", 503, "unavailable", {}, None
        )
        guarded = net.guard(_FakeOpener(error=error))
        with self.assertRaises(urllib.error.HTTPError):
            guarded.open("http://example.org/")
        self.assertFalse(net.offline())

    def test_default_timeout_is_applied(self):
        fake = _FakeOpener(result=object())
        guarded = net.guard(fake)
        guarded.open("http://example.org/")
        self.assertEqual(fake.calls, 1)

    def test_guard_is_idempotent(self):
        fake = _FakeOpener(result=object())
        guarded = net.guard(fake)
        self.assertIs(net.guard(guarded), guarded)

    def test_network_error_classification(self):
        self.assertTrue(net.is_network_error(urllib.error.URLError("x")))
        self.assertTrue(net.is_network_error(TimeoutError()))
        self.assertTrue(net.is_network_error(ConnectionResetError()))
        self.assertTrue(net.is_network_error(socket.gaierror()))
        self.assertTrue(net.is_network_error(OSError(101, "unreachable")))
        self.assertFalse(net.is_network_error(
            urllib.error.HTTPError("u", 404, "nf", {}, None)
        ))
        self.assertFalse(net.is_network_error(ValueError()))

    def test_connection_error_recognises_wrapped_text(self):
        """Причина видна и через обёртку SourceError с текстом urlopen."""
        self.assertTrue(net.is_connection_error(
            "<urlopen error [Errno 111] В соединении отказано>"
        ))
        self.assertTrue(net.is_connection_error(
            "Не удалось загрузить индекс Geofabrik: timeout"
        ))
        self.assertTrue(net.is_connection_error(
            "SOCKS5: неверный логин или пароль"
        ))
        self.assertTrue(net.is_connection_error(
            "CURL error 7: Failed to connect to example"
        ))
        self.assertFalse(net.is_connection_error("Ошибка разбора файла"))

    def test_last_error_keeps_connection_reason(self):
        net.mark_offline("<urlopen error [Errno 111]>", "example.org")
        self.assertEqual(net.last_error(), "<urlopen error [Errno 111]>")
        net.mark_online()
        self.assertEqual(net.last_error(), "")

    def test_connection_advice_mentions_proxy_only_when_enabled(self):
        try:
            net.set_proxy_label("")
            self.assertNotIn("прокси", net.connection_advice())
            net.set_proxy_label("127.0.0.1:1080")
            advice = net.connection_advice()
            self.assertIn("127.0.0.1:1080", advice)
            self.assertIn("прокси", advice)
        finally:
            net.set_proxy_label("")
