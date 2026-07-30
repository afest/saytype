# Third-party components

iwhisper itself is MIT-licensed (see [LICENSE](LICENSE)). It depends on the
components below. Nothing here is redistributed by this repository — pip installs
each package from its own source — but a bundled build (installer, frozen
executable) does redistribute them, and then the terms in this file apply.

## LGPL-3.0 — special handling when bundling

| Component | Why it is here | Obligation when bundled |
|---|---|---|
| [PySide6](https://doc.qt.io/qtforpython/licenses.html) (+ `shiboken6`) | the entire user interface | LGPL-3.0. Ship the Qt/PySide6 libraries as **separate files** (not statically linked into one executable), include the full license text, and let the user replace them with a compatible version. |

Practical consequence for packaging: use a one-folder build (PyInstaller
`--onedir`, or an equivalent) rather than a single-file executable, and copy the
upstream `LICENSE.LGPLv3` / `LICENSE.GPLv3` texts next to the binaries.

`pystray` and `pynput` are **not** dependencies of this project — the tray icon
and the global hotkey are implemented with Qt and the Win32 API directly. Only
the pynput *string format* for hotkeys (`<ctrl>+<shift>+q`) is reused, and a
format is not code.

## MIT / BSD / Apache-2.0 — attribution only

| Component | License |
|---|---|
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT |
| [CTranslate2](https://github.com/OpenNMT/CTranslate2) | MIT |
| [huggingface_hub](https://github.com/huggingface/huggingface_hub) | Apache-2.0 |
| [tokenizers](https://github.com/huggingface/tokenizers) | Apache-2.0 |
| [onnxruntime](https://github.com/microsoft/onnxruntime) | MIT |
| [NumPy](https://numpy.org) | BSD-3-Clause |
| [SciPy](https://scipy.org) | BSD-3-Clause |
| [sounddevice](https://github.com/spatialaudio/python-sounddevice) | MIT |
| [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) | MIT |
| [Pillow](https://python-pillow.org) | MIT-CMU |
| [pyperclip](https://github.com/asweigart/pyperclip) | BSD-3-Clause |
| [winotify](https://github.com/versa-syahptr/winotify) | MIT |
| [tqdm](https://github.com/tqdm/tqdm) | MPL-2.0 / MIT |
| [PyAV](https://github.com/PyAV-Org/PyAV) | BSD-3-Clause (links against FFmpeg libraries — see below) |

## Not bundled, deliberately

**ffmpeg.** Prebuilt ffmpeg binaries are GPL-licensed; bundling one would place
the whole project under the GPL. iwhisper therefore never ships ffmpeg. It is
used only to convert a recorded call to MP3, is looked up in `PATH` at runtime,
and its absence is not an error — the WAV file is kept instead.

**Model weights.** Whisper checkpoints are downloaded from Hugging Face at first
run and stay under the user's profile directory. They are covered by their own
model licenses (the original OpenAI Whisper weights are MIT), not by this
repository's license.
