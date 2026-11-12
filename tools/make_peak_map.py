#!/usr/bin/env python3
"""Интерактивная карта проверенных пиков на подложке OpenTopoMap.

Карта — обычный HTML с Leaflet; тайлы подложки запрашиваются по мере
просмотра у локального сервера, который сам скачивает их с
OpenTopoMap через SOCKS5-прокси и кэширует на диск.  Так заранее
ничего не качается, а при повторном открытии тайлы берутся из кэша.

Запуск:

    python3 tools/make_peak_map.py --csv /tmp/peaks/peaks.csv \\
        --out /tmp/peaks/map --serve

Скрипт печатает адрес (по умолчанию http://127.0.0.1:8765/); его нужно
открыть в браузере.  Без --serve генерируется только index.html —
тогда тайлы не подгрузятся.
"""

import argparse
import csv
import http.server
import json
import math
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

TILE_URL = "https://a.tile.opentopomap.org/{z}/{x}/{y}.png"
LEAFLET_URLS = {
    "leaflet.js": "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
    "leaflet.css": "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
}
USER_AGENT = "qgis-xtopo-peak-review/1.0 (peak placement check)"

VERDICT_COLOURS = {
    "error": "#d00000",
    "suspect": "#e08a00",
    "ok": "#2a9d3f",
}
VERDICT_LABELS = {
    "error": "ошибка",
    "suspect": "подозрительный",
    "ok": "в порядке",
}

TRANSPARENT_GIF = (
    "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)


def fetch(url, destination, proxy, timeout=30):
    """Скачивает файл (через прокси, если задан); True при успехе."""
    destination = Path(destination)
    if destination.exists() and destination.stat().st_size > 0:
        return True
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    command = [
        "curl", "-sS", "--fail", "--compressed", "--max-time", str(timeout),
        "-A", USER_AGENT,
    ]
    if proxy:
        command.extend(["--socks5-hostname", proxy.replace("socks5://", "")])
    command.extend(["-o", str(temporary), url])
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        temporary.unlink(missing_ok=True)
        return False
    temporary.replace(destination)
    return True


# Понятные названия регионов по имени файла OSM.
REGIONS = (
    ("caucasus", "Кавказ"),
    ("georgia", "Кавказ"),
    ("armenia", "Кавказ"),
    ("azerbaijan", "Кавказ"),
    ("switzerland", "Швейцария"),
    ("pakistan", "Пакистан"),
    ("nepal", "Непал"),
)


def region_name(source_file, csv_path):
    """Название региона по имени файла OSM или по имени CSV."""
    name = (source_file or "").lower()
    for marker, label in REGIONS:
        if marker in name:
            return label
    stem = Path(csv_path).stem.lower()
    for marker, label in REGIONS:
        if marker in stem:
            return label
    return Path(csv_path).stem


def load_peaks(csv_path):
    peaks = []
    with Path(csv_path).open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["verdict"] not in VERDICT_COLOURS:
                continue

            def number(key):
                try:
                    return float(row[key])
                except (KeyError, ValueError):
                    return float("nan")

            peaks.append({
                "id": row["osm_id"],
                "category": row.get("category", ""),
                "lon": number("lon"),
                "lat": number("lat"),
                "verdict": row["verdict"],
                "dem": number("dem"),
                "summit_distance": number("summit_distance"),
                "summit_drop": number("summit_drop"),
                "drop600": number("drop600"),
                "ridge_distance": number("ridge_distance"),
                "divide_distance": number("divide_distance"),
                "sharp_distance": number("sharp_distance"),
                "ring_higher": number("ring_higher"),
                "ridge_strict_distance": number("ridge_strict_distance"),
                "divide_strict_distance": number("divide_strict_distance"),
                "ele_diff": number("ele_diff"),
                "ring_700_higher": number("ring_700_higher"),
                "ring_150_lower": number("ring_150_lower"),
                "flags": row.get("flags", ""),
                "timestamp": row.get("timestamp", "")[:10],
                "file": row.get("file", ""),
                "region": region_name(row.get("file", ""), csv_path),
            })
    return peaks


def build_html(peaks, out_dir):
    markers = []
    for peak in peaks:
        markers.append({
            "id": peak["id"],
            "cat": peak["category"],
            "lat": peak["lat"],
            "lon": peak["lon"],
            "v": peak["verdict"],
            "dem": round(peak["dem"]) if peak["dem"] == peak["dem"] else None,
            "sd": round(peak["summit_distance"]) if peak["summit_distance"] == peak["summit_distance"] else None,
            "sp": round(peak["summit_drop"]) if peak["summit_drop"] == peak["summit_drop"] else None,
            "d600": round(peak["drop600"]) if peak["drop600"] == peak["drop600"] else None,
            "rd": round(peak["ridge_distance"]) if peak["ridge_distance"] == peak["ridge_distance"] else None,
            "dd": round(peak["divide_distance"]) if peak["divide_distance"] == peak["divide_distance"] else None,
            "sh": round(peak["sharp_distance"]) if peak["sharp_distance"] == peak["sharp_distance"] else None,
            "rh": round(peak["ring_higher"] * 100) if peak["ring_higher"] == peak["ring_higher"] else None,
            "rs": round(peak["ridge_strict_distance"]) if peak["ridge_strict_distance"] == peak["ridge_strict_distance"] else None,
            "ds": round(peak["divide_strict_distance"]) if peak["divide_strict_distance"] == peak["divide_strict_distance"] else None,
            "ed": round(peak["ele_diff"], 1) if peak["ele_diff"] == peak["ele_diff"] else None,
            "r7": round(peak["ring_700_higher"] * 100) if peak["ring_700_higher"] == peak["ring_700_higher"] else None,
            "l1": round(peak["ring_150_lower"]) if peak["ring_150_lower"] == peak["ring_150_lower"] else None,
            "flags": peak["flags"],
            "date": peak["timestamp"],
            "file": peak["file"],
            "region": peak["region"],
        })
    counts = {}
    for peak in peaks:
        counts[peak["verdict"]] = counts.get(peak["verdict"], 0) + 1
    legend = "".join(
        f"<span class='legend'><i style='background:{VERDICT_COLOURS[name]}'></i>"
        f"{VERDICT_LABELS[name]} — {counts.get(name, 0)}</span>"
        for name in ("error", "suspect", "ok")
    )
    data = json.dumps(markers, ensure_ascii=False)
    html = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Пики OSM — проверка положения</title>
<link rel="stylesheet" href="leaflet.css">
<style>
  html, body {{ margin: 0; padding: 0; height: 100%; font-family: sans-serif; }}
  #map {{ position: absolute; top: 0; left: 340px; right: 0; bottom: 0; }}
  #sidebar {{ position: absolute; top: 0; left: 0; width: 340px; bottom: 0;
             overflow-y: auto; background: #fafafa; border-right: 1px solid #ccc;
             padding: 10px; box-sizing: border-box; font-size: 13px; }}
  #sidebar h1 {{ font-size: 15px; margin: 0 0 8px; }}
  .legend {{ display: inline-block; margin-right: 10px; white-space: nowrap; }}
  .legend i {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%;
               margin-right: 4px; vertical-align: middle; }}
  .item {{ padding: 4px 6px; border-bottom: 1px solid #e4e4e4; cursor: pointer; }}
  .item:hover {{ background: #eef4ff; }}
  .item .id {{ font-weight: bold; }}
  .item .meta {{ color: #666; font-size: 12px; }}
  .dot {{ display: inline-block; width: 9px; height: 9px; border-radius: 50%;
          margin-right: 5px; }}
  #regions {{ margin-bottom: 8px; }}
  .chip {{ display: inline-block; margin: 0 4px 4px 0; padding: 2px 7px;
           border: 1px solid #bbb; border-radius: 10px; cursor: pointer;
           background: #fff; font-size: 12px; }}
  .chip:hover {{ background: #eef4ff; }}
  .popup h3 {{ margin: 0 0 6px; font-size: 14px; }}
  .popup table {{ border-collapse: collapse; font-size: 12px; }}
  .popup td {{ padding: 1px 6px 1px 0; vertical-align: top; }}
  .popup td.k {{ color: #666; white-space: nowrap; }}
  .popup a {{ font-size: 12px; }}
</style>
</head>
<body>
<div id="sidebar">
  <h1>Пики OSM — проверка положения</h1>
  <div style="margin-bottom:8px">{legend}</div>
  <div style="color:#666;margin-bottom:10px;font-size:12px">
    Проверяются пики без названия: без ele и с ele. Клик по строке или
    точке центрирует карту; в подсказке — id, координаты и метрики.
  </div>
  <div id="regions"></div>
  <div id="list"></div>
</div>
<div id="map"></div>
<script src="leaflet.js"></script>
<script>
const peaks = {data};
const colours = {json.dumps(VERDICT_COLOURS)};
const labels = {json.dumps(VERDICT_LABELS, ensure_ascii=False)};

const map = L.map('map', {{minZoom: 7, maxZoom: 18}});
L.tileLayer('tiles/{{z}}/{{x}}/{{y}}.png', {{
  minZoom: 7, maxZoom: 18,
  errorTileUrl: '{TRANSPARENT_GIF}',
  attribution: '&copy; <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA), &copy; OpenStreetMap'
}}).addTo(map);

function popup(peak) {{
  const rows = [
    ['id', 'node/' + peak.id],
    ['координаты', peak.lat.toFixed(5) + ', ' + peak.lon.toFixed(5)],
    ['категория', peak.cat === 'anonymous' ? 'без названия и ele' : 'без названия, с ele'],
    ['высота DEM', peak.dem === null ? '—' : peak.dem + ' м'],
    ['ближайшая вершина', peak.sd === null ? '—' : peak.sd + ' м, +' + peak.sp + ' м'],
    ['до гребня', peak.rd === null ? '—' : peak.rd + ' м'],
    ['до водораздела', peak.dd === null ? '—' : peak.dd + ' м'],
    ['до острого гребня', peak.sh === null ? '—' : peak.sh + ' м'],
    ['кольцо 200 м выше', peak.rh === null ? '—' : peak.rh + ' %'],
    ['строгий гребень', peak.rs === null ? '—' : peak.rs + ' м'],
    ['строгий водораздел', peak.ds === null ? '—' : peak.ds + ' м'],
    ['расхождение OSM ele и DEM', peak.ed === null ? '—' : peak.ed + ' %'],
    ['кольцо 700 м выше', peak.r7 === null ? '—' : peak.r7 + ' %'],
    ['низ кольца 150 м', peak.l1 === null ? '—' : peak.l1 + ' м'],
    ['правка', peak.date + (peak.file ? ' · ' + peak.file : '')],
  ];
  return '<div class="popup"><h3 style="color:' + colours[peak.v] + '">' +
    labels[peak.v] + '</h3><table>' +
    rows.map(([k, v]) => '<tr><td class="k">' + k + '</td><td>' + v + '</td></tr>').join('') +
    '</table><div style="margin-top:6px;font-size:12px">' + (peak.flags || '') + '</div>' +
    '<a target="_blank" href="https://www.openstreetmap.org/node/' + peak.id + '">открыть в OSM</a></div>';
}}

// Точки на карте — все проверенные пики.
const markers = {{}};
peaks.forEach(peak => {{
  const marker = L.circleMarker([peak.lat, peak.lon], {{
    radius: peak.v === 'error' ? 6 : 4,
    color: '#222', weight: 1,
    fillColor: colours[peak.v],
    fillOpacity: peak.v === 'error' ? 0.9 : 0.55
  }}).addTo(map);
  marker.bindPopup(popup(peak));
  markers[peak.id] = marker;
}});

// Список — только ошибки, самые далёкие от гребня сверху.
const list = document.getElementById('list');
const regions = [...new Set(peaks.map(p => p.region))];
const counts = {{}};
peaks.forEach(p => {{
  counts[p.region] = counts[p.region] || {{all: 0, error: 0}};
  counts[p.region].all += 1;
  if (p.v === 'error') counts[p.region].error += 1;
}});

// Кнопки регионов: показывать только один регион или все сразу.
let active = null;
function renderList() {{
  list.innerHTML = '';
  const shown = peaks
    .filter(p => p.v === 'error' && (active === null || p.region === active))
    .sort((a, b) => (b.rd || 0) - (a.rd || 0));
  list.insertAdjacentHTML('beforebegin',
    '<div style="color:#666;margin-bottom:6px;font-size:12px">На карте ' +
    peaks.length + ' проверенных пиков; в списке ошибки: ' + shown.length + '</div>');
  shown.forEach(peak => {{
    const item = document.createElement('div');
    item.className = 'item';
    item.innerHTML = '<span class="dot" style="background:' + colours[peak.v] + '"></span>' +
      '<span class="id">node/' + peak.id + '</span> ' +
      '<span class="meta">' + peak.region + '</span><br>' +
      '<span class="meta">' + peak.lat.toFixed(4) + ', ' + peak.lon.toFixed(4) +
      (peak.dem === null ? '' : ' · ' + peak.dem + ' м') +
      (peak.rd === null ? '' : ' · гребень в ' + peak.rd + ' м') + '</span>';
    item.addEventListener('click', () => {{
      map.flyTo([peak.lat, peak.lon], 14, {{duration: 0.8}});
      setTimeout(() => markers[peak.id].openPopup(), 900);
    }});
    list.appendChild(item);
  }});
}}

const chips = document.getElementById('regions');
chips.innerHTML = '<span class="chip" data-region="">все (' +
  peaks.length + ')</span>' + regions.map(name =>
    '<span class="chip" data-region="' + name + '">' + name + ' (' +
    counts[name].all + ')</span>').join('');
chips.querySelectorAll('.chip').forEach(chip => {{
  chip.addEventListener('click', () => {{
    active = chip.dataset.region || null;
    chips.querySelectorAll('.chip').forEach(c => {{
      c.style.background = (c.dataset.region || '') === (active || '') ? '#dbe8ff' : '';
    }});
    renderList();
    const part = active === null ? peaks : peaks.filter(p => p.region === active);
    if (part.length) {{
      const bounds = L.latLngBounds(part.map(p => [p.lat, p.lon]));
      map.fitBounds(bounds, {{maxZoom: 12}});
    }}
  }});
}});
renderList();

// Общий вид: сразу видно все регионы.
if (peaks.length) {{
  map.fitBounds(L.latLngBounds(peaks.map(p => [p.lat, p.lon])), {{maxZoom: 10}});
}}
</script>
</body>
</html>
"""
    path = Path(out_dir) / "index.html"
    path.write_text(html, encoding="utf-8")
    return path


class TileHandler(http.server.SimpleHTTPRequestHandler):
    """Отдаёт файлы каталога и докачивает тайлы по запросу браузера."""

    proxy = None
    root = None
    lock = threading.Lock()
    last_request = 0.0
    min_interval = 0.25

    def do_GET(self):  # noqa: N802 - имя задано базовым классом
        path = self.path.split("?", 1)[0]
        if path.startswith("/tiles/"):
            self.serve_tile(path)
            return
        super().do_GET()

    def serve_tile(self, path):
        parts = path.strip("/").split("/")
        if len(parts) != 4:
            self.send_error(404)
            return
        zoom, x, y = parts[1], parts[2], parts[3].removesuffix(".png")
        if not (zoom.isdigit() and x.isdigit() and y.isdigit()):
            self.send_error(404)
            return
        target = Path(self.root) / "tiles" / zoom / x / f"{y}.png"
        if not target.exists():
            with TileHandler.lock:
                if not target.exists():
                    # Не частим с запросами к OpenTopoMap.
                    wait = TileHandler.min_interval - (time.monotonic() - TileHandler.last_request)
                    if wait > 0:
                        time.sleep(wait)
                    TileHandler.last_request = time.monotonic()
                    if not fetch(
                        TILE_URL.format(z=zoom, x=x, y=y), target, self.proxy
                    ):
                        self.send_error(404)
                        return
        try:
            data = target.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv", action="append", required=True,
        help="CSV из check_peak_placement.py (можно несколько — регионы "
             "сводятся на одной карте)",
    )
    parser.add_argument("--out", required=True, help="каталог для карты")
    parser.add_argument(
        "--proxy", default="",
        help="SOCKS5-прокси для загрузки тайлов, например socks5://host:port "
             "(по умолчанию — без прокси)",
    )
    parser.add_argument("--port", type=int, default=8765, help="порт локального сервера")
    parser.add_argument(
        "--serve", action="store_true",
        help="запустить локальный сервер (без него тайлы не загрузятся)",
    )
    arguments = parser.parse_args(argv)

    out_dir = Path(arguments.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    peaks = []
    for csv_path in arguments.csv:
        peaks.extend(load_peaks(csv_path))
    print(f"Пиков для карты: {len(peaks)}")

    for name, url in LEAFLET_URLS.items():
        if not fetch(url, out_dir / name, arguments.proxy):
            raise SystemExit(f"не удалось скачать {name}")

    path = build_html(peaks, out_dir)
    print(f"Карта: {path}")

    if not arguments.serve:
        print("Тайлы подгружаются локальным сервером: запустите с --serve")
        return 0

    TileHandler.proxy = arguments.proxy
    TileHandler.root = str(out_dir)
    handler = lambda *args, **kwargs: TileHandler(*args, directory=str(out_dir), **kwargs)
    server = Server(("127.0.0.1", arguments.port), handler)
    print(f"Откройте http://127.0.0.1:{arguments.port}/  (Ctrl+C — остановить)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено")
    return 0


if __name__ == "__main__":
    sys.exit(main())
