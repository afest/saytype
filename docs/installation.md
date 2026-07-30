# Installation (Windows)

## 1. Python

Python 3.10 or newer, 3.12 recommended. Install it **for the current user**, not
system-wide, unless you have a reason — the rest of these instructions use
`pip install --user`.

Check that the interpreter you are about to use is the one you think it is:

```powershell
python -c "import sys; print(sys.executable, sys.version)"
```

If several Pythons are installed, call the intended one by its full path
everywhere below. A smoke test that "passes" on the wrong interpreter is worse
than no smoke test.

## 2. The package

```powershell
git clone https://github.com/afest/iwhisper
cd iwhisper
pip install --user -r requirements.txt
pip install --user -e .
```

`-e` (editable) means the repository *is* the installed code: edit a file,
restart the app, and the change is live. Drop the `-e` for a snapshot install.

## 3. GPU

Nothing to do if you already have a working CUDA 12.x runtime. If CTranslate2
cannot find `cublas` / `cudnn`, install them from pip:

```powershell
pip install --user nvidia-cublas-cu12 nvidia-cudnn-cu12
```

These land in `site-packages\nvidia\*\bin`, where the Windows DLL search does not
look — `iwhisper.engine` adds those folders to the search path at import, before
anything touches `ctranslate2`. That is why importing `engine` first matters.

Verify:

```powershell
python -c "from iwhisper import engine; m = engine.load_model('tiny'); print(engine.current_device(), engine.current_compute_type())"
```

`cuda float16` (or another `cuda` compute type) means the GPU path works. `cpu
int8` means it fell back — check `nvidia-smi` and the driver version. It still
works, just slower.

## 4. First run

```powershell
pythonw.exe -m iwhisper
```

The first start downloads model weights (0.5–2 GB depending on the preset) with a
progress dialog, and generates the tray icons. Both go to
`%LOCALAPPDATA%\iwhisper`.

If nothing appears: run `python -m iwhisper` (without the `w`) to get the log in
the console.

## 5. Optional

**ffmpeg** — only for the MP3 archive of recorded calls:

```powershell
winget install Gyan.FFmpeg
```

The app looks for `ffmpeg` in `PATH` and in the winget package folder. Without it
the call WAV is kept and nothing fails.

**Autostart** — a checkbox in Settings. It creates a shortcut to
`pythonw.exe -m iwhisper` in `shell:startup`.

## Upgrading from an unpackaged copy

If you ran the modules as loose files before, settings used to live in
`%APPDATA%\faster-whisper-ui\settings.ini`. On first start the app copies that
file into `%LOCALAPPDATA%\iwhisper\settings.ini` — a copy, not a move, so the old
one stays as a fallback. The history folder is taken from the settings, so
recordings stay where they were.

## Uninstall

```powershell
pip uninstall iwhisper
```

Then delete `%LOCALAPPDATA%\iwhisper` (settings, dictionary, model weights,
history) and the autostart shortcut from `shell:startup` if you created one.
