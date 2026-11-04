"""Параллельный прогон тестов плагина.

Каждый тестовый модуль выполняется в отдельном процессе: QGIS не
поддерживает двух приложений в одном процессе, зато несколько процессов
работают вполне параллельно. Модули используют временные каталоги и не
мешают друг другу.

    python3 tools/run_tests.py            # число процессов по числу ядер
    python3 tools/run_tests.py -j 2       # ограничить число процессов
    python3 tools/run_tests.py tests.test_config   # только выбранные модули
"""

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"


def modules():
    return sorted(
        f"tests.{path.stem}" for path in TESTS.glob("test_*.py")
    )


def run_module(name):
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    started = time.monotonic()
    process = subprocess.run(
        [sys.executable, "-m", "unittest", name],
        cwd=str(ROOT),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return name, process.returncode, process.stdout, time.monotonic() - started


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-j", "--jobs", type=int, default=0)
    parser.add_argument("modules", nargs="*")
    options = parser.parse_args()

    selected = options.modules or modules()
    jobs = options.jobs or max(1, min(6, (os.cpu_count() or 2) - 1))
    print(f"Тестовых модулей: {len(selected)}, параллельно: {jobs}")

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(run_module, selected))
    total = time.monotonic() - started

    failed = []
    for name, code, output, elapsed in sorted(results, key=lambda item: -item[3]):
        status = "OK " if code == 0 else "ОШИБКА"
        print(f"  {status} {name:<45} {elapsed:6.1f} с")
        if code != 0:
            failed.append((name, output))

    for name, output in failed:
        print("=" * 72)
        print(name)
        print(output)

    print(
        f"Готово за {total:.1f} с: успешно "
        f"{len(selected) - len(failed)} из {len(selected)} модулей"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
