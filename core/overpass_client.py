import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class OverpassError(RuntimeError):
    pass


def build_query(query_text, bbox):
    bbox_part = bbox.as_overpass() if bbox is not None else ""
    header = "[out:xml][timeout:3600][maxsize:2000000000]"
    if bbox_part:
        header += f"[bbox:{bbox_part}]"
    return f"{header};{query_text.strip()}\nout body;>;out skel qt;"


def _has_error_remark(data):
    """Есть ли в документе замечание об ошибке Overpass.

    Ответ с ошибкой приходит с кодом 200 и снаружи выглядит как обычный
    OSM: ``<osm ...><remark> runtime error: ...</remark></osm>``.  Это
    не данные, а отказ сервера.  Обычное примечание ``<note>`` данными
    не считается и отвергать его нельзя.
    """
    return b"<remark" in data


def valid_osm(path):
    """Похож ли файл на ответ Overpass с данными."""
    path = Path(path)
    if not path.exists():
        return False
    size = path.stat().st_size
    if size < 100:
        return False
    with path.open("rb") as handle:
        if size <= 65536:
            data = handle.read()
            return (
                b"<osm" in data
                and not _has_error_remark(data)
                and b"</osm>" in data
            )
        head = handle.read(8192)
        if b"<osm" not in head or _has_error_remark(head):
            return False
        handle.seek(max(0, size - 4096))
        tail = handle.read()
    return b"</osm>" in tail and not _has_error_remark(tail)


def _has_elements(data):
    """Есть ли в ответе объекты, а не пустой документ или отказ."""
    if b'"remark"' in data or b"<remark" in data:
        return False
    if b'"elements"' in data:
        try:
            payload = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            return False
        elements = payload.get("elements") if isinstance(payload, dict) else None
        return bool(elements) if isinstance(elements, list) else False
    if b"<osm" in data:
        return b"<node" in data or b"<way" in data or b"<relation" in data
    return False


class OverpassClient:
    # Проверка сервера: запрос в Берлине.  Региональные зеркала (например,
    # overpass.osm.ch) на такой запрос отвечают пустым, но валидным
    # документом: такой сервер для других областей бесполезен, и
    # принимать его за рабочий нельзя.
    TEST_QUERY = (
        "[out:json][timeout:25];node(52.51,13.38,52.53,13.42);out 1;"
    )

    def __init__(self, endpoints, timeout=180, retries=1, user_agent=None):
        self.endpoints = [endpoint for endpoint in endpoints if endpoint]
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent or "QGIS-xtopo/0.4 (+https://github.com/xmd5a2/qgis-xtopo)"

    def check(self, endpoint, cancel=None):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Overpass check cancelled")
        payload = urllib.parse.urlencode({"data": self.TEST_QUERY}).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=payload,
            headers={
                "User-Agent": self.user_agent,
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                "Accept": "application/json, application/osm3s+xml, application/xml, text/xml",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=min(self.timeout, 20)) as response:
                data = b""
                while len(data) < 8192:
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("Overpass check cancelled")
                    chunk = response.read(1024)
                    if not chunk:
                        break
                    data += chunk
        except InterruptedError:
            raise
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OverpassError(f"{endpoint}: {exc}") from exc
        if _has_elements(data):
            return True
        raise OverpassError(
            f"{endpoint}: сервер не вернул объектов по тестовому запросу "
            "(региональное зеркало?)"
        )

    def fetch(self, query_name, query_text, bbox, destination, cancel=None,
              progress=None, use_cache=True):
        return self.fetch_raw(
            query_name, build_query(query_text, bbox), destination,
            cancel=cancel, progress=progress, use_cache=use_cache,
        )

    def fetch_raw(self, query_name, full_query, destination, cancel=None,
                  progress=None, use_cache=True):
        """Скачивает готовый запрос Overpass (заголовок уже собран).

        ``use_cache=False`` запрещает возвращать ранее скачанный файл:
        он нужен, когда текст запроса изменился и старый ответ больше
        не подходит.
        """
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if use_cache and valid_osm(destination):
            if progress:
                progress(f"{query_name}: cached")
            return destination
        if not self.endpoints:
            raise OverpassError("No Overpass endpoints configured")

        payload = urllib.parse.urlencode({"data": full_query}).encode("utf-8")
        errors = []
        for attempt in range(self.retries + 1):
            for endpoint in self.endpoints:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("Download cancelled")
                temporary = destination.with_suffix(destination.suffix + ".part")
                try:
                    request = urllib.request.Request(
                        endpoint,
                        data=payload,
                        headers={
                            "User-Agent": self.user_agent,
                            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                            "Accept": "application/osm3s+xml, application/xml, text/xml",
                        },
                    )
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        with temporary.open("wb") as handle:
                            while True:
                                if cancel is not None and cancel.is_set():
                                    raise InterruptedError("Download cancelled")
                                chunk = response.read(65536)
                                if not chunk:
                                    break
                                handle.write(chunk)
                    if valid_osm(temporary):
                        os.replace(temporary, destination)
                        if progress:
                            progress(f"{query_name}: downloaded")
                        return destination
                    errors.append(f"{endpoint}: incomplete response")
                except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
                    errors.append(f"{endpoint}: {exc}")
                finally:
                    if temporary.exists():
                        temporary.unlink(missing_ok=True)
                if progress:
                    progress(f"{query_name}: retrying another endpoint")
            if attempt < self.retries:
                time.sleep(min(30, 2 ** attempt))

        raise OverpassError(
            f"Could not download {query_name}. Tried {len(self.endpoints)} endpoint(s): "
            + "; ".join(errors[-5:])
        )
