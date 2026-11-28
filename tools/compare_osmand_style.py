"""Сравнение объектов стиля OsmAnd Topo и qgis-xtopo.

Скачивает `rendering_styles/topo.render.xml` из OsmAnd-resources, разбирает
запросы `resources/queries/*.txt` и поля слоёв шаблона, сопоставляет объекты
(тег=значение) и дополнительные свойства (`additional=`), дополняет числами
taginfo и пишет отчёт в `docs/osmand-topo-vs-xtopo.md`.

Запуск: python3 tools/compare_osmand_style.py [--no-taginfo]
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lxml import etree  # noqa: E402

from core import field_subset  # noqa: E402

STYLE_URL = (
    "https://raw.githubusercontent.com/osmandapp/OsmAnd-resources/master/"
    "rendering_styles/topo.render.xml"
)
COMMIT_API = (
    "https://api.github.com/repos/osmandapp/OsmAnd-resources/commits"
    "?path=rendering_styles/topo.render.xml&per_page=1"
)
TAGINFO_API = "https://taginfo.openstreetmap.org/api/4/tag/stats?"
QUERY_DIR = ROOT / "resources" / "queries"
CACHE_DIR = Path.home() / ".cache" / "qgis-xtopo" / "osmand-compare"

SECTIONS = ("order", "text", "point", "polygon", "line")
SECTION_RU = {
    "point": "точка", "polygon": "полигон", "line": "линия",
    "text": "подпись", "order": "порядок",
}

STMT = re.compile(r"(nwr|node|way|relation)\s*((?:\[[^\]]*\]\s*)+)")
FILT = re.compile(r"\[\s*\"([^\"]+)\"\s*(?:(=~|!~|=)!?\"([^\"]*)\")?\s*\]")

# служебные теги OsmAnd, которых нет в OSM
INTERNAL = re.compile(
    r"^(osmand|route_road_#SEQ|routePoints|ele_limit|population_rounded|"
    r"area_rounded|lit_additional|lit_pedestrian_layer|fixme_tag|note_tag|"
    r"osm_note|osmand_legend|osmand_change)"
)


def fetch(url, timeout=60, retries=3, binary=False):
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.read() if binary else resp.read().decode("utf-8")
        except Exception:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def style_path(cache_dir, refresh=False):
    path = cache_dir / "topo.render.xml"
    if refresh or not path.exists():
        path.write_bytes(fetch(STYLE_URL, binary=True))
    return path


# --------------------------------------------------------------- OsmAnd
def osmand_objects(path):
    root = etree.parse(str(path)).getroot()
    objects = {}
    for sec in SECTIONS:
        el = root.find(sec)
        if el is None:
            continue
        for case in el.iter("case"):
            tag = case.get("tag")
            value = case.get("value") or ""
            if not tag:
                continue
            rec = objects.setdefault(
                (tag, value),
                {"sections": set(), "additional": set(), "icons": set()},
            )
            rec["sections"].add(sec)
            add = case.get("additional")
            if add:
                rec["additional"].add(add)
            for sub in case.iter():
                if sub is case:
                    continue
                a = sub.get("additional")
                if a:
                    rec["additional"].add(a)
                icon = sub.get("icon") or sub.get("textShield")
                if icon:
                    rec["icons"].add(icon)
    return objects


# --------------------------------------------------------------- xtopo
def xtopo_objects(query_dir=QUERY_DIR):
    """(тег, значение) → {слои, геометрия, соседние условия запроса}."""
    objects = {}
    for fn in sorted(query_dir.glob("*.txt")):
        layer = fn.stem
        text = fn.read_text(encoding="utf-8")
        for m in STMT.finditer(text):
            elems = m.group(1)
            positive, negative = [], []
            for fm in FILT.finditer(m.group(2)):
                key, op, val = fm.group(1), fm.group(2), fm.group(3)
                if op == "=":
                    positive.append((key, val))
                elif op is None:
                    positive.append((key, ""))
                else:
                    negative.append((key, op, val))
            for key, val in positive:
                rec = objects.setdefault(
                    (key, val),
                    {"layers": set(), "geom": set(), "props": defaultdict(set)},
                )
                rec["layers"].add(layer)
                rec["geom"].add(elems)
                for k2, v2 in positive:
                    if (k2, v2) != (key, val):
                        rec["props"][k2].add(v2 or "*")
                for k2, op, v2 in negative:
                    rec["props"][k2].add("!%s%s" % (op, v2))
    return objects


def layer_fields():
    """Поля слоёв: ссылки шаблона плюс наборы `field_subset.PRESETS`."""
    fields = defaultdict(set)
    for stem, names in field_subset.template_references().items():
        fields[stem] |= names
    for name, preset in field_subset.PRESETS.items():
        fields[name] |= set(preset)
    for stem in fields:
        fields[stem] |= field_subset.ESSENTIAL_FIELDS
    return fields


def fields_for_layer(layer, fields):
    out = set()
    for stem, names in fields.items():
        if stem == layer or stem.startswith(layer + "_"):
            out |= names
    return out


# --------------------------------------------------------------- taginfo
def taginfo_counts(idents, cache_dir):
    cache_path = cache_dir / "taginfo.json"
    try:
        cache = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        cache = {}
    todo = [i for i in idents if i not in cache]
    for n, ident in enumerate(todo, 1):
        key, _, value = ident.partition("=")
        url = TAGINFO_API + urllib.parse.urlencode(
            {"key": key, "value": value or "-"}
        )
        try:
            data = json.loads(fetch(url, timeout=30))
            rows = data.get("data") or []
            cache[ident] = next(
                (r["count"] for r in rows if r.get("type") == "all"), 0
            )
        except Exception:  # noqa: BLE001
            cache[ident] = None
        if n % 50 == 0:
            cache_path.write_text(json.dumps(cache))
    cache_path.write_text(json.dumps(cache))
    return cache


def fmt_count(count):
    if not count:
        return "—"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f} млн".replace(".0 ", " ")
    if count >= 1000:
        return f"{count // 1000} тыс."
    return str(count)


# --------------------------------------------------------------- отчёт
def build_report(style_file, counts, commit):
    osmal = osmand_objects(style_file)
    xto = xtopo_objects()
    fields = layer_fields()

    xt_keys = defaultdict(set)
    for key, value in xto:
        xt_keys[key].add(value)
    xt_any = {key for (key, value) in xto if value == ""}
    baked = {key for (key, value) in xto if value}

    rows = []
    for (tag, value), rec in sorted(osmal.items()):
        if (tag, value) in xto or tag in xt_any:
            state = "ok"
        elif tag in xt_keys:
            state = "value"
        else:
            state = "key"
        ident = f"{tag}={value}" if value else tag
        rows.append({
            "tag": tag,
            "value": value,
            "state": state,
            "sections": sorted(rec["sections"]),
            "additional": sorted(rec["additional"]),
            "additional_osm": [
                a for a in sorted(rec["additional"])
                if not INTERNAL.match(a.split("=")[0])
            ],
            "icons": sorted(rec["icons"]),
            "xtopo_layers": sorted(xto.get((tag, value), {}).get("layers", [])),
            "xtopo_values": sorted(xt_keys.get(tag, ())),
            "count": counts.get(ident),
        })

    by_key = defaultdict(list)
    for row in rows:
        by_key[row["tag"]].append(row)

    all_fields = set()
    for names in fields.values():
        all_fields |= names

    prop_gaps = []
    for row in rows:
        if row["state"] != "ok" or not row["additional_osm"]:
            continue
        available = set()
        for layer in row["xtopo_layers"]:
            available |= fields_for_layer(layer, fields)
        missing = [a for a in row["additional_osm"]
                   if a.split("=")[0] not in available]
        if missing:
            prop_gaps.append((row, missing))

    stats = defaultdict(int)
    for row in rows:
        stats[row["state"]] += 1
    internal = [r for r in rows if INTERNAL.match(r["tag"])]

    out = []
    w = out.append
    w("# OsmAnd Topo vs qgis-xtopo: поддерживаемые объекты и их свойства\n")
    w(f"Дата: {time.strftime('%Y-%m-%d')}. "
      f"OsmAnd-resources, коммит `{commit}` (стиль `Topo`).\n")
    w("- OsmAnd: `rendering_styles/topo.render.xml`, ветка `master`.\n")
    w(f"- qgis-xtopo: `resources/queries/*.txt` "
      f"({len(list(QUERY_DIR.glob('*.txt')))} запросов) + шаблон "
      "`automap.qgz`.\n")
    w("- Числа объектов OSM — taginfo (глобальные данные).\n")
    w("Повторить: `python3 tools/compare_osmand_style.py` "
      "(`--no-taginfo` — без сетевых счётчиков).\n")

    w(f"Всего объектов в стиле OsmAnd Topo: **{len(rows)}** "
      f"(служебных тегов OsmAnd: {len(internal)}). "
      f"Поддерживает xtopo **{stats['ok']}**, ключ без нужного значения — "
      f"**{stats['value']}**, ключ не используется — **{stats['key']}**.\n")

    w("## 1. Сводка по ключам тегов\n")
    w("| тег | в OsmAnd | поддержано | не поддержано | значения в xtopo |")
    w("|---|---|---|---|---|")
    for tag in sorted(by_key):
        rs = by_key[tag]
        ok = sum(1 for r in rs if r["state"] == "ok")
        values = sorted({r["value"] for r in rs
                         if r["state"] == "ok" and r["value"]})
        if tag in xt_any:
            have = "любые"
        else:
            have = ", ".join(f"`{v}`" for v in values) if values else "—"
        w(f"| `{tag}` | {len(rs)} | {ok} | {len(rs) - ok} | {have} |")
    w("")

    w("## 2. Приоритет: самые массовые неподдерживаемые объекты\n")
    w("| объект | объектов в OSM | где рисует OsmAnd | значок |")
    w("|---|---|---|---|")
    missing = [r for r in rows if r["state"] != "ok"
               and not INTERNAL.match(r["tag"]) and r["count"]]
    for row in sorted(missing, key=lambda r: -(r["count"] or 0))[:45]:
        secs = ", ".join(SECTION_RU[s] for s in row["sections"]
                         if s != "order") or "порядок"
        icons = ", ".join(row["icons"][:2]) if row["icons"] else "—"
        w(f"| `{row['tag']}`=`{row['value']}` | {fmt_count(row['count'])} | "
          f"{secs} | {icons} |")
    w("")

    w("## 3. Полный список неподдерживаемых объектов\n")
    w("### 3.1. Ключ не используется xtopo вовсе\n")
    real = [r for r in rows if r["state"] == "key"
            and not INTERNAL.match(r["tag"])]
    w(f"Всего: {len(real)}.\n")
    w("| тег | значение | объектов в OSM | где рисует OsmAnd | значок |")
    w("|---|---|---|---|---|")
    for row in sorted(real, key=lambda r: (r["tag"], r["value"])):
        secs = ", ".join(SECTION_RU[s] for s in row["sections"]
                         if s != "order") or "порядок"
        icons = ", ".join(row["icons"][:2]) if row["icons"] else "—"
        w(f"| `{row['tag']}` | `{row['value'] or '*'}` | "
          f"{fmt_count(row['count'])} | {secs} | {icons} |")
    w("")
    w("Служебные теги OsmAnd (в OSM их нет): " +
      ", ".join(f"`{r['tag']}`" for r in internal) + ".\n")

    w("### 3.2. Ключ поддержан, но конкретного значения нет\n")
    valued = [r for r in rows if r["state"] == "value"]
    w(f"Всего: {len(valued)}.\n")
    w("| тег | значение | объектов в OSM | где рисует OsmAnd | значок | "
      "значения в xtopo |")
    w("|---|---|---|---|---|---|")
    for row in sorted(valued, key=lambda r: (r["tag"], r["value"])):
        secs = ", ".join(SECTION_RU[s] for s in row["sections"]
                         if s != "order") or "порядок"
        icons = ", ".join(row["icons"][:2]) if row["icons"] else "—"
        have = ", ".join(f"`{v}`" for v in row["xtopo_values"]) or "—"
        w(f"| `{row['tag']}` | `{row['value'] or '*'}` | "
          f"{fmt_count(row['count'])} | {secs} | {icons} | {have} |")
    w("")

    w("## 4. Дополнительные свойства объектов\n")
    w("OsmAnd различает объекты дополнительными тегами (`additional=`): "
      "`access`, `tunnel`, `bridge`, `oneway`, `surface`, `smoothness`, "
      "`tracktype`, `winter_road`, `ice_road`, `horse_scale` и др. Ниже — "
      "свойства, которые OsmAnd использует, а в слое xtopo поля нет.\n")
    agg = defaultdict(lambda: defaultdict(set))
    for row, missing_props in prop_gaps:
        for prop in missing_props:
            agg[row["tag"]][row["value"] or "*"].add(prop)
    w("| тег | значение | недостающие свойства |")
    w("|---|---|---|")
    for tag in sorted(agg):
        for value in sorted(agg[tag]):
            w(f"| `{tag}` | `{value}` | " +
              ", ".join(f"`{a}`" for a in sorted(agg[tag][value])) + " |")
    w("")

    w("### 4.1. Свойства, которых нет ни в одном слое xtopo\n")
    prop_all = defaultdict(set)
    for row in rows:
        for prop in row["additional_osm"]:
            prop_all[prop.split("=")[0]].add(prop)
    w("| свойство | значения в OsmAnd Topo | как учтено в xtopo |")
    w("|---|---|---|")
    for prop in sorted(prop_all):
        if prop in all_fields:
            continue
        how = ("зашито в запрос (значение отбирается при выгрузке)"
               if prop in baked else "**не хранится**")
        w(f"| `{prop}` | " +
          ", ".join(f"`{a}`" for a in sorted(prop_all[prop])) + f" | {how} |")
    w("")

    w("## 5. Обратная разница: объекты xtopo, которых нет в OsmAnd Topo\n")
    os_supported = defaultdict(set)
    for (tag, value) in osmal:
        os_supported[tag].add(value)
    for row in rows:
        for prop in row["additional"]:
            key, _, value = prop.partition("=")
            os_supported[key].add(value)
    extra = []
    for (key, value), rec in sorted(xto.items()):
        if key in os_supported and (value == "" or value in os_supported[key]):
            continue
        extra.append((key, value, sorted(rec["layers"])))
    w(f"Всего: {len(extra)} — данные готовим, но этот стиль их не рисует.\n")
    w("| тег | значение | слои xtopo |")
    w("|---|---|---|")
    for key, value, layers in extra:
        w(f"| `{key}` | `{value or '*'}` | " +
          ", ".join(f"`{layer}`" for layer in layers) + " |")
    w("")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", default=str(ROOT / "docs" / "osmand-topo-vs-xtopo.md"))
    parser.add_argument("--no-taginfo", action="store_true",
                        help="не запрашивать счётчики taginfo")
    parser.add_argument("--refresh", action="store_true",
                        help="перекачать topo.render.xml из master")
    args = parser.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    style_file = style_path(CACHE_DIR, refresh=args.refresh)
    try:
        commit = json.loads(fetch(COMMIT_API))[0]["sha"][:10]
    except Exception:  # noqa: BLE001
        commit = "master"

    counts = {}
    if not args.no_taginfo:
        idents = set()
        for (tag, value) in osmand_objects(style_file):
            idents.add(f"{tag}={value}" if value else tag)
        counts = taginfo_counts(sorted(idents), CACHE_DIR)

    text = build_report(style_file, counts, commit)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(text + "\n", encoding="utf-8")
    print("отчёт:", args.output)


if __name__ == "__main__":
    main()
