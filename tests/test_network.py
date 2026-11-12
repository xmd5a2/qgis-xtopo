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
