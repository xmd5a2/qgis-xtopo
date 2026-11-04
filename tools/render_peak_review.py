#!/usr/bin/env python3
"""Рисует фрагменты карты с изолиниями вокруг подозрительных пиков.

Фрагмент помогает глазами отличить настоящую вершину от ошибочного
узла: на подложке рельеф с изолиниями, водные объекты из OSM и соседние
пики.  Красным отмечен проверяемый пик, зелёным — ближайшая более
высокая точка рельефа.

Вход — CSV из ``tools/check_peak_placement.py`` и список PBF (из них
берутся водные объекты).

Пример:

    python3 tools/render_peak_review.py \\
        --csv /tmp/peaks/peaks.csv \\
        --pbf ~/qgis_projects/qgisxtopo-cache/geofabrik/georgia.osm.pbf \\
        --out /tmp/peaks/maps --limit 60
"""

import argparse
import csv
import html
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy import ndimage

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.colors import LightSource

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_peak_placement import (  # noqa: E402
    DemTiles, fill_nan, find_osmium, half_pixels, read_waterways,
)

METRES_PER_DEGREE = 111320.0


def load_rows(csv_path):
    """Читает CSV с метриками пиков."""
    rows = []
    with Path(csv_path).open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            for key in ("lon", "lat", "dem", "max3", "min3"):
                row[key] = float(row[key]) if row[key] not in ("", None) else float("nan")
            row["lon"] = float(row["lon"])
            row["lat"] = float(row["lat"])
            for key in list(row):
                if key.startswith("drop"):
                    row[key] = float(row[key]) if row[key] not in ("", None) else float("nan")
            rows.append(row)
    return rows


def select_rows(rows, limit, verdicts, radius_column):
    """Отбирает пики для просмотра: сначала самые явные, но по разным кластерам."""
    candidates = [row for row in rows if row["verdict"] in verdicts]
    for row in candidates:
        value = row.get(radius_column, float("nan"))
        row["_severity"] = value if value == value else 0.0
    candidates.sort(key=lambda row: row["_severity"], reverse=True)

    cells = defaultdict(list)
    for row in candidates:
        key = (int(row["lon"] / 0.02), int(row["lat"] / 0.02))
        cells[key].append(row)
    groups = []
    visited = set()
    for key in cells:
        if key in visited:
            continue
        stack = [key]
        visited.add(key)
        group = []
        while stack:
            current = stack.pop()
            group.extend(cells[current])
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    neighbour = (current[0] + dx, current[1] + dy)
                    if neighbour in cells and neighbour not in visited:
                        visited.add(neighbour)
                        stack.append(neighbour)
        groups.append(group)
    groups.sort(key=len, reverse=True)

    # По одному самому явному пику из каждого кластера, затем вторые.
    chosen = []
    depth = 0
    while len(chosen) < limit and any(len(group) > depth for group in groups):
        for group in groups:
            if len(group) <= depth:
                continue
            chosen.append(group[depth])
            if len(chosen) >= limit:
                break
        depth += 1
    return chosen


def find_summit(smooth, lon, lat, half_x, half_y, search_x, search_y, dem_value, zoom):
    """Самая высокая точка рельефа в радиусе поиска.

    Берётся максимум по сглаженной поверхности: на сыром DEM максимум
    попадает на случайный шумовой выброс, и метка уезжает с вершины.
    Сглаживание ставит метку на гребень/вершину устойчиво.

    Координаты считаются по увеличенной сетке, поэтому шаг ячейки
    делится на zoom.
    """
    rows, columns = smooth.shape
    row_center = rows // 2
    column_center = columns // 2
    row_from = max(0, row_center - search_y * zoom)
    row_to = min(rows, row_center + search_y * zoom + 1)
    column_from = max(0, column_center - search_x * zoom)
    column_to = min(columns, column_center + search_x * zoom + 1)
    values = smooth[row_from:row_to, column_from:column_to]
    if values.size == 0:
        return None
    flat = int(np.argmax(values))
    row_index, column = divmod(flat, values.shape[1])
    height = float(values[row_index, column])
    if height <= dem_value + 5:
        return None
    lon_min = lon - half_x / 3600.0
    lat_min = lat - half_y / 3600.0
    return (
        lon_min + (column_from + column + 0.5) / 3600.0 / zoom,
        lat_min + (row_from + row_index + 0.5) / 3600.0 / zoom,
        height,
    )


def render_peak(row, peaks_index, waterlines, dem, radius, out_path):
    """Рисует фрагмент карты вокруг пика."""
    lon = row["lon"]
    lat = row["lat"]
    half_x, half_y = half_pixels(radius, lat)
    window = dem.window(lon, lat, half_x, half_y)
    if window is None or np.isnan(window).all():
        return None
    window = fill_nan(window)

    from scipy import ndimage

    zoom = 3
    surface = ndimage.zoom(window, zoom, order=3)
    lon_span = 2 * half_x / 3600.0
    lat_span = 2 * half_y / 3600.0
    extent = (lon - lon_span, lon + lon_span, lat - lat_span, lat + lat_span)

    # SRTM в горах шумный, поэтому подложку и изолинии считаем по
    # слегка сглаженной поверхности.
    smooth = ndimage.gaussian_filter(surface, sigma=2.0)
    shade = LightSource(azdeg=315, altdeg=50).hillshade(
        smooth, vert_exag=1.5, dx=1, dy=1
    )
    shade = np.clip(shade, 0, 1)

    figure, axes = plt.subplots(figsize=(7.2, 7.2), dpi=120)
    axes.imshow(shade, cmap="gray", extent=extent, origin="lower", vmin=0.15, vmax=0.95)

    span = float(np.nanmax(smooth) - np.nanmin(smooth))
    if span > 500:
        step = 50
    elif span > 250:
        step = 25
    elif span > 120:
        step = 20
    else:
        step = 10
    low = math.floor(np.nanmin(smooth) / step) * step
    high = math.ceil(np.nanmax(smooth) / step) * step
    levels = np.arange(low, high + step, step)
    if len(levels) > 3:
        contours = axes.contour(
            smooth, levels=levels, colors="0.25", linewidths=0.7, extent=extent,
        )
        axes.clabel(contours, levels=levels[::2], inline=True, fontsize=7, fmt="%d")

    for line in waterlines:
        xs = [point[0] for point in line]
        ys = [point[1] for point in line]
        if max(xs) < extent[0] or min(xs) > extent[1]:
            continue
        if max(ys) < extent[2] or min(ys) > extent[3]:
            continue
        axes.plot(xs, ys, color="#1b6fb3", linewidth=1.3, zorder=3)

    for other in peaks_index:
        if other is row:
            continue
        if not (extent[0] <= other["lon"] <= extent[1]):
            continue
        if not (extent[2] <= other["lat"] <= extent[3]):
            continue
        axes.plot(
            other["lon"], other["lat"], marker="^", markersize=6,
            markerfacecolor="0.55", markeredgecolor="0.25", linestyle="none", zorder=4,
        )

    # Ближайший названный пик OSM — для масштаба: где стоят настоящие вершины.
    nearest = None
    for other in peaks_index:
        if other is row or not other["name"].strip():
            continue
        distance = math.hypot(
            (other["lon"] - lon) * METRES_PER_DEGREE * math.cos(math.radians(lat)),
            (other["lat"] - lat) * METRES_PER_DEGREE,
        )
        if distance > 3000:
            continue
        if nearest is None or distance < nearest[0]:
            nearest = (distance, other)
    if nearest is not None:
        distance, other = nearest
        axes.plot(
            other["lon"], other["lat"], marker="^", markersize=10,
            markerfacecolor="#1a5fb4", markeredgecolor="black",
            linestyle="none", zorder=6,
        )
        axes.text(
            other["lon"], other["lat"] + lat_span * 0.02,
            f"{other['name']} ({distance / 1000:.1f} км)", color="#1a5fb4",
            fontsize=8, ha="center", zorder=7,
        )

    # Ближайшая настоящая вершина (локальный максимум рельефа).
    search_x, search_y = half_pixels(600, lat)
    summit = find_summit(
        smooth, lon, lat, half_x, half_y, search_x, search_y, row["dem"], zoom
    )

    axes.plot(lon, lat, marker="^", markersize=13, markerfacecolor="#e02020",
              markeredgecolor="black", linestyle="none", zorder=6)
    if summit:
        axes.plot(summit[0], summit[1], marker="^", markersize=11,
                  markerfacecolor="#28a745", markeredgecolor="black",
                  linestyle="none", zorder=6)
        axes.plot([lon, summit[0]], [lat, summit[1]], color="#e02020",
                  linestyle="--", linewidth=1.0, zorder=5)
        axes.text(
            summit[0], summit[1] + lat_span * 0.02, f"+{summit[2] - row['dem']:.0f} м",
            color="#1a6b2a", fontsize=9, ha="center", zorder=7,
        )

    verdict = row["verdict"]
    colour = {"error": "#b00020", "suspect": "#b26a00", "mismatch": "#1a5fb4"}.get(
        verdict, "black"
    )
    title = {
        "error": "ОШИБКА: пик не на вершине",
        "suspect": "ПОДОЗРИТЕЛЬНО",
        "mismatch": "РАСХОЖДЕНИЕ ele",
    }.get(verdict, verdict)
    name = row["name"] or "(без названия)"
    axes.set_title(
        f"node/{row['osm_id']}  ·  {title}\n"
        f"{name}  ·  OSM ele={row['ele'] or '—'}  ·  DEM={row['dem']:.0f} м",
        fontsize=10, color=colour,
    )
    axes.text(
        0.015, 0.985,
        "\n".join([
            f"ниже вершины на {row.get('drop600') or 0:.0f} м (радиус 600 м)",
            f"локальный макс. в 90 м: {row['max3']:.0f} м",
            f"локальный мин. в 90 м: {row['min3']:.0f} м",
            f"правка: {row['timestamp'][:10]}  {row['user'] or '(без пользователя)'}",
        ]),
        transform=axes.transAxes, fontsize=8, va="top", ha="left",
        bbox=dict(facecolor="white", alpha=0.8, edgecolor="0.6"),
    )

    # Масштабная линейка на 1 км.
    bar = 1000.0 / METRES_PER_DEGREE
    x0 = extent[0] + lon_span * 0.06
    y0 = extent[2] + lat_span * 0.06
    axes.plot([x0, x0 + bar], [y0, y0], color="black", linewidth=2.5, zorder=7)
    axes.text(x0 + bar / 2, y0 + lat_span * 0.012, "1 км", fontsize=8,
              ha="center", zorder=7)

    axes.set_xlim(extent[0], extent[1])
    axes.set_ylim(extent[2], extent[3])
    axes.set_xticks([])
    axes.set_yticks([])
    figure.tight_layout()
    figure.savefig(out_path, bbox_inches="tight")
    plt.close(figure)
    return out_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, help="CSV из check_peak_placement.py")
    parser.add_argument("--pbf", action="append", default=[], help="файлы OSM PBF")
    parser.add_argument("--out", required=True, help="каталог для картинок")
    parser.add_argument("--limit", type=int, default=60, help="сколько пиков показать")
    parser.add_argument(
        "--verdicts", default="error,suspect",
        help="вердикты для просмотра через запятую",
    )
    parser.add_argument(
        "--radius", type=float, default=1200.0,
        help="радиус фрагмента карты, м (по умолчанию 1200)",
    )
    parser.add_argument(
        "--dem-cache", default=str(Path.home() / "qgis_projects" / "qgisxtopo-cache" / "srtm"),
    )
    parser.add_argument("--osmium", default=None)
    arguments = parser.parse_args(argv)

    out_dir = Path(arguments.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = load_rows(arguments.csv)
    verdicts = [item.strip() for item in arguments.verdicts.split(",") if item.strip()]
    chosen = select_rows(rows, arguments.limit, verdicts, "drop600")
    print(f"Кандидатов: {len(chosen)}")

    dem = DemTiles(arguments.dem_cache, download=True)
    waterlines = []
    if arguments.pbf:
        osmium_bin = find_osmium(arguments.osmium)
        print("Чтение водных объектов...")
        waterlines = read_waterways(arguments.pbf, osmium_bin)
        print(f"Линий водных объектов: {len(waterlines)}")

    index = []
    for number, row in enumerate(chosen, 1):
        name = f"{number:03d}_{row['osm_id']}_{row['verdict']}.png"
        path = out_dir / name
        try:
            render_peak(row, rows, waterlines, dem, arguments.radius, path)
        except Exception as exc:  # noqa: BLE001 - один кадр не должен ронять всё
            print(f"WARNING: не удалось нарисовать {row['osm_id']}: {exc}",
                  file=sys.stderr)
            continue
        index.append((name, row))
        print(f"{number}/{len(chosen)} {name}")

    html_path = out_dir / "index.html"
    parts = [
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>",
        "<title>Проверка пиков</title>",
        "<style>body{font-family:sans-serif;margin:20px;background:#fafafa}"
        ".item{display:inline-block;margin:10px;vertical-align:top;width:420px}"
        "img{width:420px;border:1px solid #bbb}"
        ".cap{font-size:12px;padding:4px;background:#fff;border:1px solid #ddd}"
        ".error{color:#b00020;font-weight:bold}.suspect{color:#b26a00;font-weight:bold}"
        "</style></head><body>",
        f"<h1>Проверка положения пиков ({len(index)} шт.)</h1>",
    ]
    for name, row in index:
        caption = (
            f"<b class='{row['verdict']}'>{row['verdict']}</b> "
            f"node/{row['osm_id']} — {html.escape(row['name'] or 'без названия')}<br>"
            f"DEM={row['dem']:.0f} м, OSM ele={row['ele'] or '—'}, "
            f"ниже вершины на {row.get('drop600') or 0:.0f} м<br>"
            f"{row['timestamp'][:10]} · {html.escape(row['user'] or 'без пользователя')}"
            f" · {html.escape(row['file'])}"
        )
        parts.append(
            f"<div class='item'><img src='{name}' loading='lazy'>"
            f"<div class='cap'>{caption}</div></div>"
        )
    parts.append("</body></html>")
    html_path.write_text("\n".join(parts), encoding="utf-8")
    print(f"Каталог: {out_dir}, индекс: {html_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
