from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent.parent
RESOURCES_DIR = PACKAGE_DIR / "resources"


def resource_path(*parts):
    return str(RESOURCES_DIR.joinpath(*parts))


def template_path():
    """Путь к шаблону проекта.

    В собранном плагине шаблон лежит в resources/, в исходном дереве
    репозитория — в корне: копия в resources не хранится, она появляется
    только в готовом пакете.
    """
    packaged = RESOURCES_DIR / "automap.qgz"
    if packaged.exists():
        return packaged
    return PACKAGE_DIR / "automap.qgz"


def query_names():
    query_dir = RESOURCES_DIR / "queries"
    return sorted(path.stem for path in query_dir.glob("*.txt"))


def read_query(name):
    path = RESOURCES_DIR / "queries" / f"{name}.txt"
    return path.read_text(encoding="utf-8")


def plugin_version():
    """Версия плагина из metadata.txt."""
    try:
        text = (PACKAGE_DIR / "metadata.txt").read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith("version="):
            return line.split("=", 1)[1].strip()
    return ""


def plugin_build_date():
    """Дата сборки пакета.

    Сборщик пишет resources/build.txt в готовый пакет.  В исходном
    дереве файла нет — берём дату изменения metadata.txt: она совпадает
    с датой последнего изменения версии.
    """
    stamp = RESOURCES_DIR / "build.txt"
    try:
        if stamp.exists():
            return stamp.read_text(encoding="utf-8").strip()
        stamp = PACKAGE_DIR / "metadata.txt"
        if stamp.exists():
            import datetime
            mtime = datetime.datetime.fromtimestamp(stamp.stat().st_mtime)
            return mtime.strftime("%Y-%m-%d")
    except OSError:
        pass
    return ""
