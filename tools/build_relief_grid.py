#!/usr/bin/env python3
"""Собирает ``resources/data/relief_grid.npz`` из ETOPO5.

Сетка 1°×1° с минимальной и максимальной высотой нужна авторежиму
рельефа: по доле горных тайлов он решает, что регион горный, и ставит
ALOS раньше Copernicus. В плагин попадает уже готовая сетка, скрипт
нужен только для её пересборки.

ETOPO5 (5′, NOAA) — общественное достояние:
https://www.ngdc.noaa.gov/mgg/global/relief/ETOPO5/
"""

import argparse
import tempfile
import urllib.request
from pathlib import Path

import numpy as np

URL = (
    "https://www.ngdc.noaa.gov/mgg/global/relief/ETOPO5/TOPO/ETOPO5/"
    "ETOPO5.DAT"
)
ROWS, COLS = 2160, 4320
STEP = 12  # 5′ → 1°
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "resources" / "data" / "relief_grid.npz"


def read_etopo5(path):
    """Читает ETOPO5.DAT: 16-битные big-endian, начало — 90°N, 0°E."""
    raw = Path(path).read_bytes()
    expected = ROWS * COLS * 2
    if len(raw) != expected:
        raise SystemExit(
            f"ETOPO5: ожидалось {expected} байт, получено {len(raw)}"
        )
    grid = np.frombuffer(raw, dtype=">i2").reshape(ROWS, COLS)
    # В файле столбец 0 — нулевой меридиан; для сетки удобнее -180°.
    return np.roll(grid, -COLS // 2, axis=1).astype("int16")


def build(grid):
    """Минимум и максимум высоты в каждом тайле 1°×1°."""
    blocks = grid.reshape(ROWS // STEP, STEP, COLS // STEP, STEP)
    high = blocks.max(axis=(1, 3)).astype("int16")
    low = blocks.min(axis=(1, 3)).astype("int16")
    return high, low


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--etopo", type=Path,
        help="готовый ETOPO5.DAT; без него файл скачивается",
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help=f"куда сохранить сетку (по умолчанию {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    if args.etopo is not None:
        source = args.etopo
        grid = read_etopo5(source)
    else:
        with tempfile.TemporaryDirectory(prefix="xtopo_etopo_") as directory:
            source = Path(directory) / "ETOPO5.DAT"
            print(f"Скачивание {URL} …")
            urllib.request.urlretrieve(URL, source)
            grid = read_etopo5(source)

    high, low = build(grid)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(args.output, high=high, low=low)
    print(
        f"Готово: {args.output} "
        f"({args.output.stat().st_size / 1024:.0f} КБ)"
    )


if __name__ == "__main__":
    main()
