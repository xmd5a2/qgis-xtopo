"""Плотность изолиний и автоподбор порогов отображения.

Базовые масштабные пороги взяты из шаблона ``automap.qgz``: это режим
«Обычно».  Остальные режимы умножают верхний порог каждого уровня, не
меняя ни данные, ни набор кратностей.  Поэтому плотность можно менять в
открытом проекте без переподготовки.

«Редко» — вдвое реже текущей схемы.  «Плотно» и «Очень плотно» нужны
равнинам: при малом перепаде высот без них на обзорных масштабах остаётся
слишком мало горизонталей.  «Максимум» — ручной режим для совсем плоских
участков.
"""

from . import slope_style

# Идентификаторы режимов хранятся в переменной проекта.
SPARSE = "sparse"
NORMAL = "normal"
DENSE = "dense"
VERY_DENSE = "very_dense"
MAXIMUM = "maximum"

# Порядок совпадает с пунктами списка в панели «Проект».
PRESETS = (
    (SPARSE, 0.5),
    (NORMAL, 1.0),
    (DENSE, 2.0),
    (VERY_DENSE, 4.0),
    (MAXIMUM, 8.0),
)
DENSITIES = tuple(name for name, _factor in PRESETS)
DENSITY_FACTORS = dict(PRESETS)
LABELS = {
    SPARSE: "Редко",
    NORMAL: "Обычно",
    DENSE: "Плотно",
    VERY_DENSE: "Очень плотно",
    MAXIMUM: "Максимум",
}
DEFAULT = NORMAL

# Базовые верхние пороги масштаба (scalemaxdenom) текущей схемы шаблона.
RENDER_SCALE = {
    5: 25_000,
    10: 50_000,
    25: 100_000,
    50: 300_000,
    100: 600_000,
}
LABEL_SCALE = {
    10: 25_000,
    50: 100_000,
    100: 300_000,
}

# Автовыбор по тому же детектору, что и вариант раскраски уклонов.
VARIANT_DENSITY = {
    slope_style.MOUNTAINS: SPARSE,
    slope_style.FOOTHILLS: NORMAL,
    slope_style.PLAINS: VERY_DENSE,
}

VARIABLE = "isolines_density"
MIN_SCALE = 1_000
MAX_SCALE = 10_000_000


def normalize(value):
    """Допустимый идентификатор режима или None."""
    text = str(value or "").strip().lower()
    return text if text in DENSITY_FACTORS else None


def factor(value):
    """Множитель режима; неизвестное значение считается «Обычно»."""
    return DENSITY_FACTORS.get(normalize(value) or DEFAULT, 1.0)


def scaled(base, value):
    """Порог масштаба для режима.

    Ноль означает «без ограничения» и остаётся нулём.
    """
    if not base:
        return 0
    result = int(round(float(base) * factor(value)))
    if result <= 0:
        return 0
    return max(MIN_SCALE, min(MAX_SCALE, result))


def choose_density(variant):
    """Режим для варианта рельефа из ``slope_style.choose_variant``."""
    return VARIANT_DENSITY.get(variant)


def project_density(project=None):
    """Режим, сохранённый в переменной проекта, или None."""
    from qgis.core import QgsProject

    project = project or QgsProject.instance()
    try:
        value = project.customVariables().get(VARIABLE)
    except Exception:
        return None
    return normalize(value)


def set_project_density(value, project=None):
    """Записывает режим в переменную проекта."""
    from qgis.core import QgsExpressionContextUtils, QgsProject

    project = project or QgsProject.instance()
    name = normalize(value) or DEFAULT
    if str(project.customVariables().get(VARIABLE)) != name:
        QgsExpressionContextUtils.setProjectVariable(
            project, VARIABLE, name
        )
    return name


def density_from_scales(scales):
    """Режим, которому соответствуют текущие верхние пороги правил.

    ``scales`` — словарь ``{уровень: верхний_порог_масштаба}``.
    Возвращает None, если пороги не совпадают ни с одним режимом.
    """
    if not scales or not any(
        level in scales for level in RENDER_SCALE
    ):
        return None
    for name, _factor in PRESETS:
        if all(
            int(round(float(current))) == scaled(base, name)
            for level, base in RENDER_SCALE.items()
            for current in [scales.get(level)]
            if current is not None
        ):
            return name
    return None
