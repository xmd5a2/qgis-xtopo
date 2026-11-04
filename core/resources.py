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
