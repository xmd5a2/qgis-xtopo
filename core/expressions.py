import importlib.util
import sys
from pathlib import Path

from .resources import resource_path


_LOADED = False


def load_expressions(progress=None, force=False):
    """Загружает пользовательские выражения QGIS.

    Модули выполняются заново при каждой загрузке: функции выражений
    регистрируются в QGIS по имени, поэтому после обновления плагина
    старые копии модулей из памяти Python не должны оставаться — иначе
    выражения проекта ссылались бы на несуществующие функции.
    """
    global _LOADED
    if _LOADED and not force:
        return []
    errors = []
    directory = Path(resource_path("expressions"))
    for path in sorted(directory.glob("*.py")):
        if path.name == "__init__.py":
            continue
        module_name = f"qgis_xtopo_expr_{path.stem}"
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


def registered_functions():
    """Имена зарегистрированных в QGIS пользовательских функций."""
    try:
        from qgis.core import QgsExpression
    except ImportError:
        return set()
    try:
        getter = getattr(QgsExpression, "Functions", None)
        if getter is None:
            getter = getattr(QgsExpression, "functions", None)
        if getter is None:
            return set()
        return {
            str(function.name()).lower() for function in getter()
        }
    except Exception:
        return set()
