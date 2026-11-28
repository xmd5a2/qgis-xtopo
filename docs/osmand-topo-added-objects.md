# Добавленные объекты OsmAnd Topo

Текстовая таблица: до какого масштаба виден каждый объект и его подпись.

Всего добавлен 21 слой:

- 17 точечных: `bus_station`, `tourism=viewpoint`, `highway=trailhead`,
  `tourism=information` (отдельно `information=guidepost`), `artwork`,
  `museum`, `zoo`, `aquarium`, `theme_park`, `hotel`, `hostel`,
  `guest_house`, `motel`, `apartment`, `caravan_site`, `camp_pitch`;
- 4 линейных: `ladder=yes`, `ford=stepping_stones`, `highway=raceway`,
  `highway=escape`.

`tourism=cabin` отдельного слоя не получил: он добавлен в запрос
`wilderness_hut.txt` и рисуется значком укрытия с теми же диапазонами.

| Объект | Тег OSM | Значок / линия | Подпись |
|---|---|---|---|
| Автобусная станция | `amenity=bus_station` | до 1:30 000 | до 1:20 000 |
| Смотровая площадка | `tourism=viewpoint` | до 1:20 000 | до 1:15 000 |
| Начало маршрута | `highway=trailhead` | до 1:50 000 | до 1:30 000 |
| Информация | `tourism=information` | до 1:30 000 | до 1:15 000 |
| Указатель | `tourism=information` + `information=guidepost` | до 1:30 000 | до 1:20 000 |
| Арт-объект | `tourism=artwork` | до 1:15 000 | до 1:15 000 |
| Музей | `tourism=museum` | до 1:30 000 | до 1:30 000 |
| Зоопарк | `tourism=zoo` | до 1:30 000 | до 1:30 000 |
| Аквариум | `tourism=aquarium` | до 1:30 000 | до 1:30 000 |
| Тематический парк | `tourism=theme_park` | до 1:30 000 | до 1:30 000 |
| Гостиница | `tourism=hotel` | до 1:50 000 | до 1:30 000 |
| Хостел | `tourism=hostel` | до 1:50 000 | до 1:30 000 |
| Гостевой дом | `tourism=guest_house` | до 1:50 000 | до 1:30 000 |
| Мотель | `tourism=motel` | до 1:50 000 | до 1:30 000 |
| Апартаменты | `tourism=apartment` | до 1:50 000 | до 1:30 000 |
| Кемпинг для автодомов | `tourism=caravan_site` | до 1:50 000 | до 1:30 000 |
| Палаточное место | `tourism=camp_pitch` | до 1:50 000 | до 1:50 000 |
| Домик | `tourism=cabin`, в слое `wilderness hut` | до 1:130 000 | до 1:20 000 |
| Лестница | `ladder=yes` | до 1:30 000 | до 1:20 000 |
| Камешки через брод | `ford=stepping_stones` | до 1:20 000 | до 1:20 000 |
| Гоночная трасса | `highway=raceway` | до 1:70 000 | до 1:20 000 |
| Аварийный съезд | `highway=escape` | до 1:130 000 | до 1:20 000 |

«До 1:N» — объект виден на 1:N и крупнее. Подписи включаются не позже
значка. У линейных слоёв общее правило подписи — до 1:20 000.

Значки взяты из Topo-стиля OsmAnd (`osmandapp/OsmAnd-resources`,
`rendering_styles/topo.render.xml`), приведены к чёрному силуэту. Правила
вытеснения значков, маски и подписи работают так же, как у остальных POI
проекта; линейные слои в вытеснении не участвуют.
