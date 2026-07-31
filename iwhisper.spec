# -*- mode: python ; coding: utf-8 -*-
"""Сборка iwhisper в папку с exe, запускаемую без Python (T-261).

    pyinstaller iwhisper.spec

Режим --onedir, не --onefile. Причины две: onefile распаковывает сотни мегабайт
во временную папку при КАЖДОМ запуске (для приложения, живущего в трее, это
секунды ожидания на ровном месте), и установщик Velopack из T-262 с onefile
не работает в принципе.

Чего в сборке НЕТ и почему:
  * CUDA (cuBLAS + cuDNN, ~450 МБ) — докачивается по требованию, см. cuda_layer.py.
    В базовой сборке CTranslate2 работает на CPU (int8).
  * веса модели — качаются при первом использовании (engine.ensure_downloaded).
  * ffmpeg — только для опционального MP3-архива созвона; готовые сборки под GPL,
    бандлинг утянул бы весь проект в GPL. Без него сохраняется WAV.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

# === Что тянем явно ===
# Хуки PyInstaller закрывают большинство пакетов, но три места ловятся плохо:
datas = []
binaries = []

# 1. faster-whisper держит рядом с кодом ассеты (модель VAD Silero + токенизатор).
#    Это данные, а не импорт — анализатор их не видит.
datas += collect_data_files("faster_whisper")

# 2. PortAudio: sounddevice кладёт DLL в отдельный пакет-данные `_sounddevice_data`,
#    PyAudioWPatch (форк PyAudio под WASAPI loopback) — свою, внутри пакета.
#    Штатных хуков под PyAudioWPatch нет, поэтому берём его бинарники руками.
datas += collect_data_files("_sounddevice_data")
binaries += collect_dynamic_libs("pyaudiowpatch")
binaries += collect_dynamic_libs("ctranslate2")

# 3. Иконка приложения: остальные иконки трея генерируются кодом в профиле
#    пользователя, а .ico нужен ещё до этого — для splash и для самого exe.
datas += [("assets/iwhisper.ico", "assets")]

hiddenimports = [
    # pynput выбирает платформенный бэкенд через __import__ по имени ОС
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    # sounddevice грузит PortAudio через cffi-обвязку
    "_cffi_backend",
    # faster-whisper зовёт их лениво, внутри функций
    "faster_whisper.vad",
    "huggingface_hub",
    "tqdm",
]

excludes = [
    # --- Qt: берём только то, что реально импортируется (QtCore/Gui/Widgets/
    # Multimedia/Charts). Остальное — сотни мегабайт, из них один
    # Qt6WebEngineCore.dll весит 195 МБ.
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtWebView",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2", "PySide6.QtQuickTest",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtGraphsWidgets",
    "PySide6.QtDesigner", "PySide6.QtUiTools", "PySide6.QtHelp", "PySide6.QtTest",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtSql", "PySide6.QtBluetooth",
    "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation", "PySide6.QtSerialPort",
    "PySide6.QtSerialBus", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtSensors", "PySide6.QtSpatialAudio", "PySide6.QtStateMachine",
    "PySide6.QtTextToSpeech", "PySide6.QtNetworkAuth", "PySide6.QtHttpServer",
    "PySide6.QtOpcUa", "PySide6.QtWebEngine", "PySide6.QtSvgWidgets",
    "PySide6.QtVirtualKeyboard", "PySide6.QtMultimediaWidgets",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "PySide6.scripts", "PySide6.support",
    # --- cryptography приезжает транзитивно через urllib3 (опциональный
    # pyopenssl-транспорт). HTTPS у нас идёт штатным ssl, так что 19 МБ
    # rust-биндингов не нужны — urllib3 переживает их отсутствие сам.
    "cryptography", "OpenSSL",
    # --- тяжёлое из чужих экосистем: ничего из этого проект не импортирует,
    # но транзитивные хуки любят притащить их «на всякий случай»
    "torch", "tensorflow", "transformers", "matplotlib", "pandas", "sympy",
    "IPython", "jupyter", "notebook", "pytest", "setuptools", "pip",
    "sklearn", "cv2", "gi", "PyQt5", "PyQt6", "PySide2",
    # --- winotify объявлен опциональной зависимостью, но код его не импортирует
    "winotify",
    # --- тесты и данные scipy: мегабайты, которые никогда не выполнятся.
    # Подпакеты numpy отсюда убраны сознательно: и `numpy.testing`, и
    # `numpy.f2py` импортирует `scipy.signal` на уровне модуля, а без него
    # ломается ресэмпл loopback-канала — то есть запись созвона целиком.
    # Выигрыш был бы единицы мегабайт, цена — молча неработающая фича.
    "scipy.io.matlab.tests", "scipy._lib.tests",
    "scipy.optimize.tests", "scipy.signal.tests", "scipy.sparse.tests",
    "scipy.stats.tests", "scipy.linalg.tests", "scipy.spatial.tests",
    "scipy.special.tests", "scipy.ndimage.tests", "scipy.interpolate.tests",
    "scipy.datasets", "scipy.misc", "scipy.weave",
    # --- PIL: нужны только Image/ImageDraw для генерации иконок трея
    "PIL.ImageQt", "PIL.ImageTk", "PIL.ImageShow", "PIL.ImageGrab",
]

a = Analysis(
    ["packaging/iwhisper_launch.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# Вторая точка входа: консольная самопроверка. Кладётся в ту же папку и делит с
# приложением все библиотеки — свой у неё только бутлоадер и архив с байткодом
# (~10 МБ). Нужна затем, что основное приложение оконное: при нехватке DLL или
# Qt-плагина оно не появляется молча, и на чужой машине разбираться не с чем.
#
# MERGE() здесь НЕ используется, хотя напрашивается: он раскладывает общие
# модули по одному владельцу, и второй exe остаётся без pyperclip, PySide6 и
# самого пакета iwhisper — то есть самопроверка начинает «находить» поломки,
# которых в приложении нет. Дублирование байткода дешевле такого вранья.
a_selftest = Analysis(
    ["packaging/iwhisper_selftest.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    # Самопроверка импортирует всё по строкам (`__import__(name)`), а статический
    # анализатор такие импорты не видит: без явного перечисления Qt приезжает без
    # своего runtime-хука, и `QtWidgets` падает с «DLL load failed» там, где у
    # приложения всё в порядке.
    hiddenimports=hiddenimports + [
        "pyaudiowpatch", "pyperclip", "iwhisper.transcribe_call",
        "PySide6.QtWidgets", "PySide6.QtMultimedia", "PySide6.QtCharts",
        "scipy.signal", "PIL.Image",
    ],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)
pyz_selftest = PYZ(a_selftest.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="iwhisper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX выключен сознательно: сжатый exe без подписи — первый кандидат на
    # эвристический вердикт антивируса, а выигрыш в размере тут десятки мегабайт.
    upx=False,
    console=False,  # трей-приложение: консольное окно при старте не нужно
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/iwhisper.ico",
)

exe_selftest = EXE(
    pyz_selftest,
    a_selftest.scripts,
    [],
    exclude_binaries=True,
    name="iwhisper-selftest",
    debug=False,
    strip=False,
    upx=False,
    console=True,  # тут вывод и есть смысл программы
    icon="assets/iwhisper.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    exe_selftest,
    a_selftest.binaries,
    a_selftest.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="iwhisper",
)
