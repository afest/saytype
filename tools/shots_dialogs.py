"""T-487: снимки окон с прежней логикой в теме V5 и галереи (offscreen, без микрофона).

    python tools/shots_dialogs.py [out_dir]

* мастер первого запуска — все шаги; индикатор уровня микрофона заглушён
  (иначе мастер открыл бы микрофон);
* окно импорта файла — до старта декодирования (exec не вызывается);
* окно ошибки и прогресс модели — QMessageBox / QProgressDialog под общей темой;
* галерея компонентов — с токенами по умолчанию и с подменой акцента и знака
  (`SAYTYPE_TOKENS`), чтобы показать: меняется в одном месте — меняется везде.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "_dev" / "shots"
OUT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")
os.environ.setdefault("SAYTYPE_PROFILE_DIR", os.path.join(os.environ["LOCALAPPDATA"], "saytype-dev"))
sys.path.insert(0, str(ROOT / "src"))

if os.environ.get("SHOTS_GALLERY_ONLY"):
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    from saytype.ui.gallery import Gallery
    g = Gallery()
    g.resize(1180, 900)
    g.show()
    app.processEvents()
    g.centralWidget().widget().grab().save(str(OUT / os.environ["SHOTS_GALLERY_ONLY"]))
    g.ov.hide_overlay()
    g.ov_call.hide_overlay()
    print("gallery", os.environ["SHOTS_GALLERY_ONLY"])
    sys.exit(0)

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog  # noqa: E402

app = QApplication(sys.argv)
from saytype.ui.widgets import APP_QSS  # noqa: E402
app.setStyleSheet(APP_QSS)

from saytype import wizard  # noqa: E402
wizard._LevelMeter.start = lambda self, *a, **k: None  # микрофон не открываем
wizard._LevelMeter.stop = lambda self, *a, **k: None
from saytype.transcribe_ui_window import load_settings_dict  # noqa: E402

wz = wizard.FirstRunWizard(load_settings_dict())
wz.resize(620, 520)
wz.show()
for i in range(wz.stack.count()):
    wz.stack.setCurrentIndex(i)
    app.processEvents()
    wz.grab().save(str(OUT / f"wizard-step{i + 1}.png"))
    print("wizard step", i + 1)
wz.hide()

from saytype import audio_import  # noqa: E402
from saytype.transcribe_ui_window import FileImportDialog  # noqa: E402
from saytype.ui.legacy_theme import apply_v5_dialog_theme  # noqa: E402
api = audio_import.FileImportApi(begin=lambda: True, run=lambda *a, **k: {}, end=lambda: None)
dlg = FileImportDialog(None, Path.home() / "Music" / "интервью-с-заказчиком.m4a", api)
apply_v5_dialog_theme(dlg)
dlg.resize(520, 180)
dlg.show()
app.processEvents()
dlg.grab().save(str(OUT / "dialog-import.png"))
dlg.hide()
print("import dialog")

box = QMessageBox(QMessageBox.Warning, "Не удалось распознать запись",
                  "Модель не загрузилась: файл весов повреждён.\n\nОткрой раздел «Модели» и нажми «Скачать».",
                  QMessageBox.Ok)
box.show()
app.processEvents()
box.grab().save(str(OUT / "dialog-error.png"))
box.hide()
prog = QProgressDialog("Скачиваю модель Whisper Large v3 Turbo…\n3,1 МБ/с · осталось 4 мин", "Отменить", 0, 100)
prog.setWindowTitle("SayType — модель")
prog.setValue(37)
prog.setMinimumWidth(460)
prog.show()
app.processEvents()
prog.grab().save(str(OUT / "dialog-progress.png"))
prog.hide()
print("message box, progress")

# галерея: по умолчанию и с подменой токенов (отдельные процессы — подмена до импорта)
env = dict(os.environ)
env["SHOTS_GALLERY_ONLY"] = "gallery-default.png"
subprocess.run([sys.executable, __file__, str(OUT)], env=env, check=True)
env["SHOTS_GALLERY_ONLY"] = "gallery-accent-override.png"
env["SAYTYPE_TOKENS"] = json.dumps({"Color": {"ACCENT": "#E0452A", "ACCENT_HOVER": "#C23A22", "DIGIT_ACTIVE": "#E0452A",
                                              "SIGNAL": "#E0452A", "STRIP_BG": "#E0452A", "FULL": "#E0452A",
                                              "MARK": "#E0452A"}})
subprocess.run([sys.executable, __file__, str(OUT)], env=env, check=True)
print("done")
