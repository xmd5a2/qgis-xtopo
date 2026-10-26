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
    header = f"[out:xml][timeout:3600][maxsize:2000000000]"
    if bbox_part:
        header += f"[bbox:{bbox_part}]"
    return f"{header};{query_text.strip()}\nout body;>;out skel qt;"


def _valid_osm(path):
    path = Path(path)
    if not path.exists() or path.stat().st_size < 100:
        return False
    with path.open("rb") as handle:
        head = handle.read(2048)
    if b"<osm" not in head:
        return False
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - 4096))
        tail = handle.read()
    return b"</osm>" in tail


class OverpassClient:
    TEST_QUERY = "[out:json][timeout:10];node(1);out;"

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
        if b'"elements"' in data or b'"osm3s"' in data or b"<osm" in data:
            return True
        raise OverpassError(f"{endpoint}: unexpected response to the test query")

    def fetch(self, query_name, query_text, bbox, destination, cancel=None, progress=None):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if _valid_osm(destination):
            if progress:
                progress(f"{query_name}: cached")
            return destination
        if not self.endpoints:
            raise OverpassError("No Overpass endpoints configured")

        payload = urllib.parse.urlencode({"data": build_query(query_text, bbox)}).encode("utf-8")
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
                    if _valid_osm(temporary):
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
