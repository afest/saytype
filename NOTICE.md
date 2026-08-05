# Third-party components

SayType itself is MIT-licensed (see [LICENSE](LICENSE)). This file describes the
components it is built on and the obligations that come with redistributing them.

Two situations are different and are kept apart below:

* **Installing from source.** pip fetches every package from its own origin; this
  repository redistributes nothing, and only attribution applies.
* **A built distribution** (the installer, the frozen application). It carries
  those libraries as files, so their terms apply to what the user receives. The
  full, generated inventory ships with the build as
  `licenses/THIRD-PARTY-LICENSES.txt` — regenerate it with
  `python tools/collect_licenses.py`.

## LGPL-3.0 — obligations when bundled

| Component | Version | Why it is here |
|---|---|---|
| [PySide6](https://doc.qt.io/qtforpython/licenses.html) (with `PySide6-Essentials`, `PySide6-Addons`, `shiboken6`) and the Qt libraries behind it | 6.11.1 | the entire user interface |

The Qt for Python wheels are offered under `LGPL-3.0-only OR GPL-2.0-only OR
GPL-3.0-only`; SayType takes them under **LGPL-3.0**. What that requires of a
build, and how this project satisfies it:

1. **Ship the libraries as separate, replaceable files.** The build is one-folder
   (PyInstaller `--onedir`), so `PySide6/`, `shiboken6/` and the Qt DLLs sit next
   to the executable rather than being linked into it. A user can drop in their
   own build of the same version.
2. **Include the license texts.** `licenses/LICENSE.LGPLv3` is in the
   distribution. `licenses/LICENSE.GPLv3` is there too — not because any GPL code
   is bundled, but because LGPL-3.0 is written as a set of additional permissions
   on top of GPL-3.0 and is incomplete without it.
3. **Say so, and point at the sources of the exact version.** The "About" dialog
   names the component, its version, and links to the source release for that
   version. "Whatever is newest upstream" would not satisfy the requirement — the
   point is being able to rebuild a replacement for the library that shipped.

## LGPL-2.1 — FFmpeg inside Qt Multimedia

| Component | Version | Why it is here |
|---|---|---|
| FFmpeg (`avcodec`, `avformat`, `avutil`, `swresample`, `swscale`) | n7.1.3 | shipped inside PySide6; Qt Multimedia's media backend, used by the built-in player |

Not a dependency of this project — it arrives with the PySide6 wheels. Qt builds
it **without** `--enable-gpl`, so `libx264` and `libx265` are absent and the
libraries are LGPL-2.1-or-later; every one of them reports
`lib*** license: LGPL version 2.1 or later` in its own binary, and the
configuration string embedded in `avutil` carries no GPL switch. `licenses/
LICENSE.LGPLv2.1` ships with the build, and the About dialog links to the FFmpeg
source release for that exact version.

If a future Qt release starts shipping a GPL-configured FFmpeg, this stops being
true. The check is mechanical: look for `libx264` / `libx265` and for
`--enable-gpl` in the configuration string inside
`_internal/PySide6/avutil-*.dll` of a fresh build.

## Weak copyleft, file level

No obligation beyond keeping the notice and offering the source of the component
itself if it is modified. Neither is modified here.

| Component | License |
|---|---|
| [certifi](https://github.com/certifi/python-certifi) | MPL-2.0 |
| [tqdm](https://github.com/tqdm/tqdm) | MPL-2.0 AND MIT |

## Permissive — attribution only

The remaining dependencies are MIT / BSD / Apache-2.0 / ISC / PSF and require only
that their notices travel with the distribution, which `THIRD-PARTY-LICENSES.txt`
does. The main ones:

| Component | License |
|---|---|
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT |
| [CTranslate2](https://github.com/OpenNMT/CTranslate2) | MIT |
| [huggingface_hub](https://github.com/huggingface/huggingface_hub) | Apache-2.0 |
| [tokenizers](https://github.com/huggingface/tokenizers) | Apache-2.0 |
| [onnxruntime](https://github.com/microsoft/onnxruntime) | MIT |
| [NumPy](https://numpy.org) | BSD-3-Clause and others |
| [SciPy](https://scipy.org) | BSD-3-Clause |
| [sounddevice](https://github.com/spatialaudio/python-sounddevice) | MIT |
| [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) | Apache-2.0 |
| [Pillow](https://python-pillow.org) | MIT-CMU |
| [pyperclip](https://github.com/asweigart/pyperclip) | BSD |

## NVIDIA CUDA runtime — proprietary, downloaded on demand

The GPU path needs NVIDIA's cuBLAS and the cuDNN loader. They are **not** in the
installer: the app offers to download a separate archive
(`saytype-cuda-<version>.zip`, ~553 MB) from this repository's releases and keeps
it under the user's profile.

*This software contains source code provided by NVIDIA Corporation.*

| File | Product | Terms |
|---|---|---|
| `cublas64_12.dll`, `cublasLt64_12.dll` | CUDA Toolkit — cuBLAS 12.9 | [CUDA EULA](https://docs.nvidia.com/cuda/eula/index.html), Attachment A lists `cublas.dll` / `cublasLt.dll` as redistributable |
| `cudnn64_9.dll` | cuDNN 9.21 | [cuDNN SLA](https://docs.nvidia.com/deeplearning/cudnn/sla/index.html), which permits distributing "the runtime files .so and .dll" |

The EULA texts travel **inside the archive** and are unpacked next to the
libraries, so a user always has them at hand. Conditions this project has to
meet, and how it meets them:

- **the application must add material functionality beyond the SDK** — SayType is
  a dictation application; the libraries are one accelerated code path in it;
- **the files must be used only by this application** — they are unpacked into
  SayType's own profile directory and added to its DLL search path, not to a
  system-wide location;
- **no standalone redistribution** — the archive is a component of the
  application, downloadable only because shipping 553 MB to people without an
  NVIDIA card would be absurd. It is not offered as an NVIDIA runtime
  distribution, and the release notes say so.

Without the layer the application still works: CTranslate2 falls back to CPU int8.

## Not bundled, deliberately

**PyAV / FFmpeg.** PyAV wheels carry their own FFmpeg build, and that build
includes `libx264` and `libx265` — encoders under **GPLv2+**. One such file in a
distribution puts the whole distribution under the GPL, whatever this project's
own license says. SayType therefore excludes PyAV from the build. Nothing is lost:
the application never decodes media files. Dictation and call recording hand the
model a NumPy array, and WAV files are read with the standard library's `wave`.
Because `faster_whisper` imports `av` at module level, a stub module takes its
place in the frozen build (`packaging/av_stub_rthook.py`); touching it raises a
clear error instead of silently doing nothing.

**ffmpeg (the executable).** Prebuilt ffmpeg binaries are GPL-licensed, so none is
shipped. It is used only to convert a recorded call to MP3, is looked up in `PATH`
at runtime, and its absence is not an error — the WAV file is kept instead.

**pynput.** Was a dependency until the hotkey parser was rewritten. It is
LGPL-3.0, and in a frozen build its bytecode ends up inside the executable's PYZ
archive, where the user cannot replace it — so the replaceability requirement
could not be met for it. It was used for one thing: validating a hotkey string.
That parser is now this project's own code (`src/saytype/hotkeys.py`), and the
dependency is gone rather than worked around.

**pystray.** Never a dependency. The tray icon is `QSystemTrayIcon`; the global
hotkey is `RegisterHotKey` through a Qt native event filter.

**winotify.** Declared as an optional extra, but the application does not import
it and the build excludes it. Without it there are simply no toast notifications.

**Model weights.** Whisper checkpoints are downloaded from Hugging Face at first
run and stay under the user's profile directory. They are covered by their own
model licenses (the original OpenAI Whisper weights are MIT), not by this
repository's license.
