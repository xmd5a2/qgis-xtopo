from dataclasses import dataclass


@dataclass(frozen=True)
class OverpassServer:
    name: str
    url: str


# Порядок важен: первым идёт глобальный инстанс, остальные — запасные.
DEFAULT_OVERPASS_SERVERS = (
    OverpassServer("overpass-api.de", "https://overpass-api.de/api/interpreter"),
    OverpassServer("private.coffee", "https://overpass.private.coffee/api/interpreter"),
    OverpassServer("maps.mail.ru", "https://maps.mail.ru/osm/tools/overpass/api/interpreter"),
)
