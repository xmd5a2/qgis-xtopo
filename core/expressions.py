import importlib.util
import sys
from pathlib import Path

from .resources import resource_path


_LOADED = False


def load_expressions(progress=None):
    global _LOADED
    if _LOADED:
        return []
    errors = []
    directory = Path(resource_path("expressions"))
    for path in sorted(directory.glob("*.py")):
        if path.name == "__init__.py":
            continue
        module_name = f"qgis_xtopo_expr_{path.stem}"
        if module_name in sys.modules:
            continue
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
            if progress:
                progress(f"WARNING: expression {path.name} failed: {exc}")
    _LOADED = True
    return errors
