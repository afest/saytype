"""Консольная самопроверка собранного приложения.

Собирается в тот же дистрибутив вторым exe и делит с ним `_internal`. Нужна
потому, что основное приложение — оконное: если в сборке не хватает DLL или
Qt-плагина, оно просто не появляется, без единой строки на экране. Здесь всё
то же самое импортируется по очереди и печатается человеческим языком.

    saytype-selftest.exe            проверить окружение и импорты
    saytype-selftest.exe --audio    плюс список аудиоустройств
    saytype-selftest.exe --model    плюс загрузка модели и транскрипция тишины

Код возврата: 0 — всё поднялось, 1 — что-то не импортировалось.
"""

import sys
import time
import traceback

CHECKS = [
    ("numpy", "numpy"),
    ("scipy.signal (ресэмпл loopback)", "scipy.signal"),
    ("PIL (иконки трея)", "PIL.Image"),
    ("sounddevice (микрофон)", "sounddevice"),
    ("pyaudiowpatch (WASAPI loopback)", "pyaudiowpatch"),
    ("saytype.hotkeys (разбор hotkey)", "saytype.hotkeys"),
    ("pyperclip (автопаст)", "pyperclip"),
    ("shiboken6 (рантайм привязок Qt)", "shiboken6"),
    ("PySide6.QtWidgets", "PySide6.QtWidgets"),
    ("PySide6.QtMultimedia (плеер)", "PySide6.QtMultimedia"),
    ("PySide6.QtCharts (статистика)", "PySide6.QtCharts"),
    # `av` в сборке подменён заглушкой (T-318): проверяем, что импорт проходит —
    # именно он нужен faster_whisper. Настоящего PyAV с GPL-кодеками тут нет.
    ("av (заглушка вместо PyAV)", "av"),
    ("onnxruntime (VAD)", "onnxruntime"),
    ("ctranslate2", "ctranslate2"),
    ("faster_whisper", "faster_whisper"),
    ("huggingface_hub", "huggingface_hub"),
    ("saytype.engine", "saytype.engine"),
    ("saytype.cuda_layer", "saytype.cuda_layer"),
    ("saytype.transcribe_call", "saytype.transcribe_call"),
    ("saytype.ui.main_window (интерфейс V5)", "saytype.ui.main_window"),
]


def main() -> int:
    print("=== saytype: самопроверка сборки ===")
    print(f"frozen: {getattr(sys, 'frozen', False)}")
    print(f"exe:    {sys.executable}")
    print(f"python: {sys.version.split()[0]}")
    print()

    failed = []
    for title, module in CHECKS:
        started = time.monotonic()
        try:
            __import__(module)
        except Exception as exc:
            failed.append((title, exc))
            print(f"  ПРОВАЛ  {title}: {exc.__class__.__name__}: {exc}")
            continue
        print(f"  ок      {title}  ({time.monotonic() - started:.1f}s)")

    print()
    from saytype import cuda_layer, engine, profile

    print(f"профиль:        {profile.profile_dir()}")
    print(f"CUDA DLL-пути:  {[str(p) for p in engine.CUDA_DLL_DIRS] or 'нет'}")
    print(f"CUDA-рантайм:   {engine.cuda_runtime_available()}")
    print(f"видеокарта:     {engine.detect_gpu() or 'NVIDIA не найдена'}")
    print(f"CUDA-слой:      {cuda_layer.installed_version() or 'не установлен'}")

    if "--audio" in sys.argv:
        print("\n=== аудиоустройства ===")
        try:
            import sounddevice as sd

            for i, dev in enumerate(sd.query_devices()):
                io = f"in:{dev['max_input_channels']} out:{dev['max_output_channels']}"
                print(f"  [{i}] {dev['name']}  ({io})")
        except Exception:
            traceback.print_exc()

    if "--model" in sys.argv:
        print("\n=== загрузка модели ===")
        try:
            import numpy as np

            spec = "tiny"  # самая лёгкая: проверяем тракт, а не качество
            print(f"качаю/гружу {spec}…")
            model = engine.load_model(spec, progress_cb=lambda d, t: None)
            print(f"загружена: {engine.current_device()}/{engine.current_compute_type()}")
            silence = np.zeros(16000 * 3, dtype=np.float32)
            segments, _info = model.transcribe(silence, language="ru")
            print(f"транскрипция тишины прошла, сегментов: {len(list(segments))}")
        except Exception:
            traceback.print_exc()
            failed.append(("загрузка модели", "см. traceback выше"))

    print()
    if failed:
        print(f"ИТОГ: провалов {len(failed)}")
        return 1
    print("ИТОГ: всё поднялось")
    return 0


if __name__ == "__main__":
    code = main()
    if sys.stdout.isatty():
        input("\nEnter — закрыть…")
    raise SystemExit(code)
