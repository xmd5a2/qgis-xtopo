"""One-off patch: curve admin boundary labels along the border.

Changes line-based label placement (Parallel -> Curved) for the admin level 2
country names and admin level 4 region names in the project template.
Delete this script after applying; the result stays in automap.qgz.
"""

import re
import sys
import zipfile
from pathlib import Path

ADMIN_LAYERS = {
    "admin level 2 (country names)": 2,
    "admin level 4 (region names)": 6,
}
PLACEMENT = re.compile(r"<placement\b[^>]*/>")


def patch_layer(block):
    changed = []
    already = 0

    def replace(match):
        nonlocal already
        tag = match.group(0)
        line = (
            'geometryGeneratorEnabled="1"' in tag
            and 'layerType="LineGeometry"' in tag
        )
        if line and 'placement="3"' in tag:
            already += 1
        if line and 'placement="2"' in tag:
            changed.append(tag)
            return tag.replace('placement="2"', 'placement="3"')
        return tag

    return PLACEMENT.sub(replace, block), len(changed), already


def patch_text(text):
    counts = {}
    for name, expected in ADMIN_LAYERS.items():
        marker = f"<layername>{name}</layername>"
        start = text.find(marker)
        if start < 0:
            raise SystemExit(f"Layer not found: {name}")
        start = text.rfind("<maplayer", 0, start)
        end = text.find("</maplayer>", start) + len("</maplayer>")
        block = text[start:end]
        patched, count, already = patch_layer(block)
        if count + already != expected:
            raise SystemExit(
                f"{name}: expected {expected} line rules, "
                f"changed {count}, already curved {already}"
            )
        text = text[:start] + patched + text[end:]
        counts[name] = (count, already)
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
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=9) as target:
        for info, data in members:
            if info.filename.endswith(".qgs"):
                data = text.encode("utf-8")
            target.writestr(info, data, compress_type=info.compress_type,
                            compresslevel=9)
    for name, (count, already) in counts.items():
        print(
            f"{name}: {count} line rules switched to Curved, "
            f"{already} already curved"
        )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "automap.qgz")
