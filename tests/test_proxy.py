"""Прокси: конфигурация и SOCKS5-рукопожатие.

SOCKS5 проверяется на локальном фейковом сервере в потоке — внешняя
сеть не нужна. Живой прокси (например socks5 127.0.0.1:1122) можно
проверить переменной QGIS_XTOPO_TEST_PROXY, без неё тест пропускается.
"""

import os
import select
import socket
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from qgis.PyQt.QtCore import QSettings

from qgis_xtopo.core import proxy


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _TargetHTTPServer:
    def __init__(self):
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()


class _FakeSocks5Server:
    """Минимальный SOCKS5-прокси: приветствие, CONNECT, ретрансляция."""

    def __init__(self, require_auth=None):
        self.require_auth = require_auth
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.connects = []
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        self.thread.join(timeout=2)

    def _serve(self):
        while not self._stop.is_set():
            try:
                client, _addr = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,),
                             daemon=True).start()

    def _handle(self, client):
        try:
            client.settimeout(10)
            head = self._recvn(client, 2)
            methods = self._recvn(client, head[1])
            ok = 0x00 in methods
            if self.require_auth is not None:
                if 0x02 not in methods:
                    client.sendall(b"\x05\xff")
                    return
                client.sendall(b"\x05\x02")
                auth = self._recvn(client, 2)
                user = self._recvn(client, auth[1])
                password = self._recvn(client, self._recvn(client, 1)[0])
                ok = (user.decode(), password.decode()) == self.require_auth
                client.sendall(b"\x01" + (b"\x00" if ok else b"\x01"))
                if not ok:
                    return
            else:
                if not ok:
                    client.sendall(b"\x05\xff")
                    return
                client.sendall(b"\x05\x00")
            request = self._recvn(client, 4)
            if request[1] != 0x01 or request[3] != 0x03:
                client.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            host = self._recvn(client, self._recvn(client, 1)[0]).decode()
            port = int.from_bytes(self._recvn(client, 2), "big")
            target = socket.create_connection((host, port), timeout=10)
            self.connects.append((host, port))
            client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            self._relay(client, target)
        except (OSError, ValueError):
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    @staticmethod
    def _recvn(sock, count):
        data = b""
        while len(data) < count:
            chunk = sock.recv(count - len(data))
            if not chunk:
                raise OSError("обрыв")
            data += chunk
        return data

    @staticmethod
    def _relay(left, right):
        sockets = [left, right]
        try:
            while True:
                readable, _w, _x = select.select(sockets, [], [], 2)
                if not readable:
                    return
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    (right if source is left else left).sendall(data)
        except OSError:
            pass


class ProxyConfigTest(unittest.TestCase):
    def _store(self):
        """Отдельный файл настроек на тест: без блокировок между прогонами."""
        fd, path = tempfile.mkstemp(prefix="xtopo_proxy_", suffix=".ini")
        os.close(fd)
        os.unlink(path)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return QSettings(path, QSettings.IniFormat)

    def test_roundtrip(self):
        path = self._store()
        config = proxy.ProxyConfig(
            enabled=True, ptype="http", host="proxy.local", port=3128,
            login="user", password="secret",
        )
        proxy.save(config, store=path)
        loaded = proxy.load(store=path)
        self.assertTrue(loaded.enabled)
        self.assertEqual(loaded.ptype, "http")
        self.assertEqual(loaded.host, "proxy.local")
        self.assertEqual(loaded.port, 3128)
        self.assertEqual(loaded.login, "user")
        self.assertEqual(loaded.password, "secret")

    def test_defaults_disabled(self):
        loaded = proxy.load(store=self._store())
        self.assertFalse(loaded.enabled)
        self.assertEqual(loaded.ptype, "socks5")


class Socks5Test(unittest.TestCase):
    def test_urlopen_through_socks5(self):
        config = proxy.ProxyConfig(enabled=True, ptype="socks5",
                                   host="127.0.0.1", port=0)
        with _TargetHTTPServer() as target, _FakeSocks5Server() as socks:
            config.port = socks.port
            opener = urllib.request.build_opener(proxy._Socks5Handler(config))
            url = f"http://127.0.0.1:{target.port}/"
            with opener.open(url, timeout=10) as response:
                self.assertEqual(response.read(), b"ok")
            self.assertIn(("127.0.0.1", target.port), socks.connects)

    def test_auth(self):
        config = proxy.ProxyConfig(enabled=True, ptype="socks5",
                                   host="127.0.0.1", port=0,
                                   login="user", password="secret")
        with _TargetHTTPServer() as target:
            with _FakeSocks5Server(require_auth=("user", "secret")) as socks:
                config.port = socks.port
                opener = urllib.request.build_opener(
                    proxy._Socks5Handler(config))
                url = f"http://127.0.0.1:{target.port}/"
                with opener.open(url, timeout=10) as response:
                    self.assertEqual(response.read(), b"ok")
            with _FakeSocks5Server(require_auth=("user", "wrong")) as socks:
                config.port = socks.port
                opener = urllib.request.build_opener(
                    proxy._Socks5Handler(config))
                with self.assertRaises(proxy.ProxyError):
                    opener.open(f"http://127.0.0.1:{target.port}/",
                                timeout=10)


@unittest.skipUnless(
    os.environ.get("QGIS_XTOPO_TEST_PROXY"),
    "нужен QGIS_XTOPO_TEST_PROXY, например socks5://127.0.0.1:1122",
)
class LiveProxyTest(unittest.TestCase):
    def test_real_proxy(self):
        url = urlparse(os.environ["QGIS_XTOPO_TEST_PROXY"])
        config = proxy.ProxyConfig(
            enabled=True, ptype=url.scheme, host=url.hostname,
            port=url.port or 1080,
        )
        proxy.apply(config)
        try:
            with urllib.request.urlopen(
                    "https://example.com/", timeout=30) as response:
                self.assertEqual(response.status, 200)
        finally:
            proxy.apply(proxy.ProxyConfig())


if __name__ == "__main__":
    unittest.main()
