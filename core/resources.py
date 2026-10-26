from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent.parent
RESOURCES_DIR = PACKAGE_DIR / "resources"


def resource_path(*parts):
    return str(RESOURCES_DIR.joinpath(*parts))


def query_names():
    query_dir = RESOURCES_DIR / "queries"
    return sorted(path.stem for path in query_dir.glob("*.txt"))


def read_query(name):
    path = RESOURCES_DIR / "queries" / f"{name}.txt"
    return path.read_text(encoding="utf-8")
