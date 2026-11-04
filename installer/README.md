# Инсталлятор QGIS-xtopo для Windows

Инсталлятор (`qgis-xtopo-setup-<версия>.exe`) сделан на Inno Setup 6.
Он:

* проверяет, установлен ли QGIS (реестр, `Program Files`, `OSGeo4W`);
* если QGIS не найден — предлагает скачать и установить официальный
  установщик QGIS (standalone MSI) без дополнительных вопросов;
* кладёт плагин в профиль QGIS
  (`%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\qgis_xtopo`);
* включает плагин в `QGIS3.ini` (секция `[PythonPlugins]`, `qgis_xtopo=true`);
* создаёт ярлык «QGIS-xtopo» на рабочем столе и в меню «Пуск» — он
  запускает QGIS сразу с открытой панелью плагина;
* при удалении убирает плагин, ярлыки и запись в `QGIS3.ini`.

Установка не требует прав администратора: всё ставится в профиль
пользователя.

## Состав

| Файл | Назначение |
|------|------------|
| `qgis-xtopo.iss` | скрипт Inno Setup: установка, ярлыки, проверка QGIS |
| `scripts/install-qgis.ps1` | поиск QGIS, скачивание и установка официального MSI |
| `build-installer.sh` | сборка на Linux/macOS (через wine) и на Windows |
| `build-installer.ps1` | сборка на Windows (используется в CI) |
| `icon/logo.ico` | значок плагина для ярлыка |

`payload/` появляется при сборке: туда распаковывается архив плагина.
Папка в git не хранится.

## Сборка

Нужен Inno Setup 6 (Windows: `choco install innosetup`).

Windows:

```powershell
./installer/build-installer.ps1
```

Linux/macOS (компилятор берётся из префикса wine):

```sh
WINEPREFIX=~/.wine ./installer/build-installer.sh
```

Путь к компилятору можно задать явно: `ISCC=/путь/к/ISCC.exe`.
Скрипт ищет `ISCC.exe` в префиксе wine в каталогах
`Program Files (x86)/Inno Setup 6`, `Program Files/Inno Setup 6` и `inno`
(последний — если Inno Setup установлен с ключом `/DIR=C:\inno`).

Результат: `dist/qgis-xtopo-setup-<версия>.exe`.

Сборка в CI: `.github/workflows/build-installer.yml` (windows-2022,
Inno Setup из Chocolatey), артефакт `qgis-xtopo-setup`.

## Установка QGIS из инсталлятора

Скрипт `scripts/install-qgis.ps1`:

1. ищет QGIS в реестре (`HKLM\SOFTWARE\QGIS\QGIS3`), в `Program Files`,
   в `OSGeo4W` и в локальных программах;
2. если QGIS нет и передан ключ `-Install` — берёт страницу
   <https://qgis.org/download/>, выбирает последний
   `QGIS-OSGeo4W-<версия>.msi`, скачивает его
   (`download.qgis.org`, запасной адрес — `dl1.qgis.org`) и ставит
   через `msiexec /i … /qn /norestart`;
3. печатает путь к `qgis-bin.exe`.

Журнал установки пишется в файл, переданный ключом `-LogFile`.
