"""One-off patch: halve the repetition rate of admin boundary labels.

Doubles every nonzero repeatDistance in the admin level 2/4 name layers, so
country and region labels repeat half as often.  Delete this script after
applying; the result stays in automap.qgz.
"""

import os
import re
import sys
import zipfile
from pathlib import Path

LAYERS = {
    "admin level 2 (country names)": {"200": "400", "80": "160"},
    "admin level 4 (region names)": {"400": "800", "160": "320"},
}
PLACEMENT = re.compile(r"<placement\b[^>]*/>")
REPEAT = re.compile(r'(repeatDistance=")(\d+)(")')


def patch_layer(block, mapping):
    changed = 0

    def replace(match):
        nonlocal changed
        tag = match.group(0)

        def value(found):
            nonlocal changed
            old = found.group(2)
            if old not in mapping:
                return found.group(0)
            changed += 1
            return found.group(1) + mapping[old] + found.group(3)

        return REPEAT.sub(value, tag)

    return PLACEMENT.sub(replace, block), changed


def patch_text(text):
    counts = {}
    for name, mapping in LAYERS.items():
        marker = f"<layername>{name}</layername>"
        start = text.find(marker)
        if start < 0:
            raise SystemExit(f"Layer not found: {name}")
        start = text.rfind("<maplayer", 0, start)
        end = text.find("</maplayer>", start) + len("</maplayer>")
        block = text[start:end]
        patched, count = patch_layer(block, mapping)
        if count and count != sum(
            block.count(f'repeatDistance="{old}"') for old in mapping
        ):
            raise SystemExit(f"{name}: patched {count} repeat values")
        text = text[:start] + patched + text[end:]
        counts[name] = count
    return text, counts


def main(path):
    path = Path(path)
    with zipfile.ZipFile(path) as source:
        members = [
            (info, source.read(info.filename)) for info in source.infolist()
        ]
    qgs = next(
        data for info, data in members if info.filename.endswith(".qgs")
    )
    text, counts = patch_text(qgs.decode("utf-8"))
    if text.encode("utf-8") == qgs:
        print("No changes")
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=9) as target:
        for info, data in members:
            if info.filename.endswith(".qgs"):
                data = text.encode("utf-8")
            target.writestr(info, data, compress_type=info.compress_type,
                            compresslevel=9)
    os.replace(temporary, path)
    for name, count in counts.items():
        print(f"{name}: {count} repeat values doubled")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "automap.qgz")
