# SayType UI (V5) — где что менять

Пакет `saytype.ui` — интерфейс приложения на PySide6 / Qt Widgets. Внешний вид
задаётся токенами, страницы собираются из общих компонентов. Прежнее окно
(`saytype/transcribe_ui_window.py`) остаётся в репозитории и включается
`SAYTYPE_UI=legacy` — для сравнения и парного бенчмарка.

## Быстрые ответы

| Хочу поменять | Где |
|---|---|
| Цвет акцента, линий, текста, цифр, штриховки | `tokens.py`, класс `Color` (hex) и `Hatch.RGBA` |
| Заголовочный шрифт (сейчас Unbounded 450) | файл в `assets/fonts/` + `Font.HEAD_FILE`, `Font.HEAD_WEIGHT`; текстовый шрифт — `Font.BODY_FAMILIES` |
| Кегли и межбуквенные | `tokens.py`, класс `Font`; заголовок h1 — `Grid.h1_size()` (clamp по ширине окна) |
| Знак (логотип) | `assets/mark.svg` (справочно) и путь в `icons.py` (`MARK_PATH`, `MARK_CUT`): рисуется QPainter'ом, цвет `Color.MARK` |
| Иконка приложения и трея | `icons.app_tile_pixmap()` — белый знак на плитке; цвета `Color.APP_TILE*` (трей: покой / запись / распознавание), пропорции `Grid.APP_TILE_*`. Окно и трей берут её на лету; `assets/saytype.ico` (exe, установщик, splash, ярлык) — пересобрать `python tools/make_app_icon.py` |
| Ритм сетки: высота шапки, паддинг ячеек, высота часов, ширина sidebar | `tokens.py`, класс `Grid` (`HEAD`, `PAD`, `clock_height()`, `sidebar_width()`); направляющие — `Grid.GUIDES` |
| Длительности и кривые анимаций | `tokens.py`, класс `Motion` (цифры 780 мс, сброс 740 + 20·i, переход страниц 650 мс, hover меню 420 мс) |
| Направление перехода страниц | `Motion.PAGE_DIRECTION`: одно для всех переходов, `1` — слева направо, `-1` — справа налево |
| Включить/выключить анимации | настройка «Анимации интерфейса» (ключ `ui_animations`) → `Motion.set_user_enabled()`; `Motion.reduced()` учитывает её и системную настройку Windows |
| Меню развёрнутое или только иконки | `Grid.SIDEBAR_ALWAYS_COMPACT` (сейчас `True`: иконки; название выдвигается вкладкой `shell._NavFlyout`, скорость — `Motion.NAV_HATCH_MS`) |
| Межстрочный текста записи | `history.py`, `LINE_HEIGHT` (1.5; в прототипе было 1.8) |
| Точка «микрофон слышит» | `widgets.py`: `LevelDot`, `level_icon` (кнопка «Завершить»), шкала — `LEVEL_FLOOR_DB` / `LEVEL_CEIL_DB` |
| Иконки (контурные, viewBox 24) | `icons.py`: словари `NAV_ICONS` (меню) и `ICONS` (общие), path-данные SVG |
| Глифы цифр | `digits.py`, `GLYPHS` (viewBox 100×140, обводка 15) |
| Состояния контролов (hover/disabled/focus) | `widgets.py`: `_btn_qss()`, `FIELD_QSS`, `Switch.paintEvent` |
| Пункты меню, порядок страниц | `shell.py`: `Sidebar.ITEMS` / `BOTTOM` |
| Плавающие индикаторы записи | `overlays.py` (`FloatingStatus`) — единственные; внутри окна плашки записи нет |
| Строка истории | `history.py` (`RecordRow`); полный текст созвона — листом `widgets.TextViewer` |
| Полоса перемотки и ползунок | `history.py`, `_SeekBar` (зона наведения, толщина полосы, размер квадратного ползунка); слой лежит в контейнере списка на границе строк — полоса растёт от своей линии в обе стороны |
| Счётчик метрик статистики | `pages/stats.py`, `_Readout` (кривая и задержка разрядов — `Motion.RESET_*`) |
| Лист (чтение, правка длинного текста) и диалоги | `widgets.py`: `OverlayDialog` — слой внутри окна с затемнением, не отдельное модальное окно (окно приложения не блокируется); `Sheet`, `TextViewer`, `TextEditorSheet` (словарь), `Modal`. Размер листа — `Grid.SHEET_*` (ширина под чтение), затемнение — `Color.SCRIM*`, появление — `Motion.OVERLAY_*`. В тестах искать через `window.findChildren(...)`, а не среди top-level окон |

Проверить, что изменение разошлось по всем экранам: `python -m saytype.ui.gallery` —
галерея показывает компоненты во всех состояниях и живой таймер с тестом границ
59→60 и 3599→3600. Страницы приложения — `python -m saytype` (из репозитория:
`PYTHONPATH=src`).

## Устройство

```
tokens.py      токены: Color, Font, Grid, Hatch, Motion; load_fonts(), body_font(), head_font()
svgpath.py     разбор SVG `d` → QPainterPath (без QtSvgWidgets — он исключён из сборки)
icons.py       icon_pixmap()/qicon() с кэшем по DPR, IconButton, mark_pixmap(), app_tile_pixmap()/app_qicon()
trayguard.py   видит ли Проводник значок трея (после его перезапуска значок теряется — окно ставит заново)
digits.py      DigitStrip (статично), DigitReel (разряд с ghost-кадрами), ClockGrid (ЧЧ:ММ:СС), MiniClock
widgets.py     Button, Switch, Label, Heading, LineEdit/ComboBox/SpinBox/TextEdit, Cell, PageHead,
               SectionHead, Field, ActionField, Spinner, Toast, LevelDot/LevelFollower/level_icon,
               Modal, Sheet/TextViewer/TextEditorSheet, confirm(), info_modal()
shell.py       NavButton, Brand, Sidebar, GuideCanvas (фон + направляющие), PageScroll,
               PageHost + _Transition (переход двумя снимками), AppShell
history.py     AudioPlayer, RecordRow
overlays.py    FloatingStatus (диктовка — низ-право, созвон — верх-центр экрана)
pages/         capture (Диктовки/Созвоны), notes, models, stats, settings, about; placeholders — запасные
main_window.py MainWindow с прежним контрактом (конструктор, notify_*, сигналы)
gallery.py     галерея компонентов
```

## Правила

* **Контракт окна не меняется.** `transcribe_ui.py` зовёт только `notify_*`,
  `show_window`, `winId` и три сигнала (`settings_changed`, `quit_requested`,
  `stop_sound_requested`); всё остальное — внутри `ui`. Из worker-потоков к Qt —
  только через `notify_*` (внутри — сигналы с queued-доставкой).
* **Никаких постоянных таймеров в покое.** Таймеры живут только пока идёт запись
  (200 мс, шаг по целым секундам), скачивание, спиннер видим. Анимации —
  `QVariantAnimation` на конкретном виджете; перерисовывается только он.
* **Время — от монотонных часов.** Страница показывает `int(monotonic - t0)`;
  пропущенные тики не проигрываются, стартовая анимация не задерживает захват.
* **Цвета и размеры — только из `tokens`.** Хардкод hex в страницах — ошибка.
* **Стиль без селектора — только на листовых виджетах.** `setStyleSheet("background:…")`
  на контейнере наследуют все вложенные виджеты: так колонки строки истории стали
  белыми и закрыли линии и полосу перемотки, а подсказки — белым по белому. Контейнеру
  — селектор по `objectName`; листу с таким стилем — `TOOLTIP_QSS` в конец.
* **Что рисует родитель, дети закрывают.** Линия ячейки (`Cell(right=True)`) и
  отрисовка в `paintEvent` строки видны, только пока дочерние виджеты их не
  заливают: отступ раскладки на 1 px от линии. Наведение и клики над детьми
  родитель не получает — интерактивную зону поверх детей делать отдельным
  виджетом (`_SeekBar`).
* **Reduced motion.** `Motion.reduced()` — настройка «Анимации интерфейса» или
  системная настройка Windows; все анимации обязаны уважать её (мгновенная подстановка).
* **Переход страниц — снимками.** Кадр перехода рисует слой из двух картинок;
  живую страницу на каждом кадре не двигать: перерисовка страницы со строками
  истории стоила кадров, и переход на «Созвоны» дёргался.
* **DPI.** Иконки и штриховка рисуются с `devicePixelRatioF()`; размеры в
  логических пикселях, Qt масштабирует сам.

## Шрифт Unbounded

`assets/fonts/Unbounded.ttf` (variable, 200–900) под OFL — лицензия рядом
(`Unbounded-OFL.txt`). Загружается `QFontDatabase.addApplicationFont` при
создании окна, из сети ничего не берётся. В сборку кладётся через `datas`
в `saytype.spec` и `package-data` в `pyproject.toml`.
