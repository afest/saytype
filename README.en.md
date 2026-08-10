> 🇷🇺 [Русская версия](README.md) — the primary one. This English text is updated
> when it is needed; if the two disagree, the Russian file is right.

# SayType

**Dictation for Windows that types into any window.** Press a hotkey, talk, press
it again — the text lands where your cursor was. Speech recognition runs on your
own machine, on your GPU if you have one.

### [⬇ Download SayType for Windows](https://github.com/afest/saytype/releases/latest)

1. On the release page open **Assets** and grab `saytype-app-win-Setup.exe`.
   Run the installer: it installs for the current user and does not ask for
   administrator rights. Windows will warn you about an unsigned app —
   [what to click](#windows-says-the-app-is-not-signed).
2. A first-run wizard asks for your microphone, a hotkey and a model, then
   downloads the model (0.5–2 GB, once).
3. Press the hotkey anywhere, say something, press it again. The text is pasted
   into the window you were in.

Prefer to run it from source? See [For developers](#for-developers) below.

It also records calls: your microphone and the system output are captured as two
separate channels, so the transcript comes out with speakers already separated.

> **Status:** works, used daily by its author, but rough around the edges. The
> interface is currently in Russian only.

## What leaves your machine

Speech never does — recognition is local, there is no account and no telemetry.
Three things do reach the network, all of them either optional or one-time:

| When | Where to | What for |
|---|---|---|
| First use of a model | `huggingface.co` | downloading the model weights |
| You click "GPU acceleration" | `github.com` (this repository's releases) | downloading the NVIDIA CUDA runtime |
| On start and from Settings | `github.com` (this repository's releases) | checking for a new version |

Recordings, transcripts, your dictionary and your settings stay in
`%LOCALAPPDATA%\saytype` and are never uploaded anywhere.

## Why

CPU-only dictation tools are fast enough to be usable and slow enough to be
annoying. If you already own an NVIDIA card, the same audio goes through
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) several times faster
at comparable accuracy. Measured against a CPU Parakeet-based tool on the same
recordings: **2.2–2.8× faster, error rate within noise of each other**. On a
60-second dictation the full pass runs around 9–10× realtime.

Numbers are from one machine (GTX 1060, 6 GB) and one speaker, in Russian. Treat
them as an order of magnitude, not a benchmark.

## Requirements

- Windows 10 or 11.
- NVIDIA GPU with CUDA 12.x for the fast path. **Without one it still works** —
  CTranslate2 falls back to CPU int8 automatically, just slower; pick a smaller
  model in that case.
- ~0.5–2 GB of disk for model weights, downloaded on first run.
- Optional: `ffmpeg` in `PATH`, only if you want an MP3 archive of recorded
  calls. Without it the WAV is kept and nothing fails.

The installer brings its own Python — you do not need one installed.

## Windows says the app is not signed

On first run Windows shows a blue **"Windows protected your PC"** screen. Click
**More info**, then **Run anyway**.

This happens because the installer is not signed with a code-signing
certificate. Such a certificate costs a few hundred dollars a year, and this is a
free tool with no company behind it, so there is none. The warning is about the
absence of a signature, not about the contents of the file.

What you can check instead of trusting the sentence above:

- the [VirusTotal report](#virustotal) for this exact release;
- the source in this repository, and the build recipe in
  [`docs/build.md`](docs/build.md) — the release is built from it with one
  command, so you can produce your own binary and compare behaviour.

## VirusTotal

A VirusTotal report belongs to one exact file, so the link is built from that
file's hash. For the current release:

| File | SHA-256 | Report |
|---|---|---|
| `saytype-app-win-Setup.exe` | `9613385a8eeff672714b180b5f77f9daa56a67507ae66349713010654c552380` | [VirusTotal](https://www.virustotal.com/gui/file/9613385a8eeff672714b180b5f77f9daa56a67507ae66349713010654c552380) |
| `saytype-app-win-Portable.zip` | `ec5c690454ba72a4ebb2d4b38c9cfba11223b5dc91cc698c74cf04819489661e` | [VirusTotal](https://www.virustotal.com/gui/file/ec5c690454ba72a4ebb2d4b38c9cfba11223b5dc91cc698c74cf04819489661e) |

Check the hash of what you downloaded before trusting the report — that is what
ties the two together:

```powershell
Get-FileHash .\saytype-app-win-Setup.exe -Algorithm SHA256
```

A handful of engines out of seventy flagging a generic heuristic is the normal
result for an unsigned PyInstaller build — see below for why.

## Antivirus warnings

Some antivirus engines flag the app, usually as a generic "may be a keylogger"
heuristic. That reaction is expected and comes from what a dictation tool has to
do to work at all:

- it registers a **global hotkey**, so it must be reachable while you are in
  another application;
- it **sends a synthetic Ctrl+V** to paste the result into the window you were
  in;
- it **records the microphone**, and for call recording also the system output.

Any of the three, on its own, is enough for a heuristic. What the app does *not*
do: it does not log keystrokes (a hotkey registration is not a keyboard hook),
does not run in the background without the tray icon, does not send audio or text
anywhere, and does not phone home. The three network destinations are listed
above in [What leaves your machine](#what-leaves-your-machine) — you can verify
them with a firewall or a proxy.

If your antivirus quarantines the app, the honest options are to check the
VirusTotal report, to add an exclusion, or to build it yourself from source.

## For developers

```powershell
git clone https://github.com/afest/saytype
cd saytype
pip install --user -r requirements.txt
pip install --user -e .
```

If CTranslate2 cannot find `cublas` / `cudnn`, install the CUDA runtime from pip
instead of a full toolkit:

```powershell
pip install --user "saytype[cuda]"
```

Then run it:

```powershell
pythonw.exe -m saytype      # no console window
python -m saytype           # same, but with a console for logs
```

Building the distribution is described in [`docs/build.md`](docs/build.md).

## Using it

The app lives in the tray. Double-click the tray icon for the history window;
right-click for settings, "About" and exit.

| Action | Default |
|---|---|
| Dictate (hold-free toggle) | `Ctrl+Shift+Q` |
| Record a call | `Ctrl+Shift+E` |

The dictation hotkey is configurable in settings and applies immediately, without
a restart.

## Where your data lives

Everything is under `%LOCALAPPDATA%\saytype`:

```
settings.ini         application settings
dictionary.txt       your vocabulary hint (see below)
replacements.json    text replacement rules
models/              downloaded model weights
history/             recordings and transcripts (configurable)
history/Calls/       call transcripts, audio buffer, recovery data
runtime/             crash log and live-session marker
icons/               tray icons, generated on first run
```

No telemetry. The only outgoing connections are the three listed in
[What leaves your machine](#what-leaves-your-machine).

## Vocabulary and replacements

Whisper mangles names, product names and jargon it has never seen. Two knobs,
both empty out of the box, both in **Settings**:

**Dictionary** — words, names and terms the model gets wrong. The text is passed
to Whisper as `initial_prompt`: a hint, not a rule. Keep it dense — short forms
and inflections, no prose. There is a hard limit of **223 tokens**, and anything
over it is silently cut **from the beginning of the string**, which means your
first entries quietly stop working. The counter under the field shows where you
are.

**Replacements in text** — `pattern → replacement` rules (Python regular
expressions, group references work) applied to the finished text. Use these when
the hint is not enough. A rule can be marked *end of text only*: useful against
the endings Whisper hallucinates over trailing silence ("thanks for watching"),
dangerous for call transcripts where a short closing line may be real.

A filled-in example is in
[`examples/dictionary-ru-example.json`](examples/dictionary-ru-example.json).

## Recording calls

`Ctrl+Shift+E` starts and stops. The microphone and the WASAPI loopback of your
default playback device are recorded separately, then transcribed channel by
channel and merged into one Markdown file with timestamps and speaker labels
(the names are yours to set in Settings).

**Get consent before you record.** In many countries and US states recording a
conversation without telling the other party is illegal. The app cannot know
where you are and does not ask.

Two things that will bite you:

- The transcript only separates speakers if the other party's audio actually goes
  through your **default** Windows playback device. If the call plays on a headset
  while the default output is a monitor, the second channel records digital
  silence. The app detects this and puts a warning banner in the transcript, but
  the recording is already lost — check the output device first.
- Audio is streamed to disk while recording, so a crash or a power loss costs the
  last second or two, not the whole call. On the next start saytype offers to
  finish an interrupted recording.

## Batch transcription

`saytype.engine` is standalone — no Qt, no UI:

```python
from saytype import engine

model = engine.load_model("large-v3-turbo")
segments, info = model.transcribe("call.wav", language="ru", beam_size=5)
print(" ".join(s.text.strip() for s in segments))
```

It owns CUDA DLL setup on Windows, model download with progress, the
`compute_type` fallback chain (`float16 → int8_float16 → int8_float32 → float32
→ cpu/int8`), and unloading from VRAM.

Passing a **file path**, as above, makes faster-whisper decode it through PyAV,
which comes with the library in a normal Python environment. The packaged
application does not ship PyAV (see [NOTICE.md](NOTICE.md)) and works with audio
as NumPy arrays — that path is unaffected.

To rebuild a transcript from an already recorded call:

```powershell
python -m saytype.retranscribe_call            # newest WAV in the calls folder
python -m saytype.retranscribe_call "path.wav"
```

## Models

Six presets from `tiny` to `large-v3`, plus "custom": any Hugging Face repo id or
a local folder holding a CTranslate2 model. Weights are downloaded to
`%LOCALAPPDATA%\saytype\models` with a progress dialog. Switching models at
runtime unloads the old one from VRAM first; it is blocked while a recording or a
transcription is in flight.

Not every checkpoint works: the model must be in CTranslate2 format. A
`.safetensors` Transformers model is rejected with the `ct2-transformers-converter`
command you need to run.

## Known limitations

- **Windows only.** WASAPI loopback, `RegisterHotKey` and the paste path are all
  Win32. The transcription core is portable, the app is not.
- **Russian is the tuned path.** The language is passed explicitly as `ru` in the
  dictation flow; other languages work but are not what the defaults were shaped
  around.
- **The installer is not signed.** Windows and some antivirus engines will say so;
  see the two sections above.
- **Older GPUs** (Pascal and similar) work, but through compute types that are
  not the fastest available. The fallback chain sorts it out automatically.
- **The first run is slow** — 5–10 seconds of imports before the window appears,
  plus the model download.

## License

MIT — see [LICENSE](LICENSE). Third-party components and what changes when you
bundle them are in [NOTICE.md](NOTICE.md).

**LGPL components.** The user interface is built on **PySide6 / Qt 6.11.1**, used
under the **LGPL-3.0**. Qt Multimedia additionally brings its own **FFmpeg
n7.1.3** libraries, under the **LGPL-2.1-or-later** (Qt builds them without
`--enable-gpl`, so no `libx264` / `libx265`). The build ships all of these as
separate files next to the executable, not linked into it, so you can replace
them with your own build of the same version. Sources for exactly those versions:
<https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.1-src/>
and <https://github.com/FFmpeg/FFmpeg/releases/tag/n7.1.3>. The full license texts
travel with the distribution in `licenses/`, and the same information is in the
application under **Tray → About**.

Nothing under the GPL is redistributed. In particular PyAV is deliberately left
out of builds: its wheels bundle an FFmpeg with `libx264` / `libx265`, which are
GPLv2+, and a single such file would place the whole distribution under the GPL.

**NVIDIA runtime.** The optional GPU layer contains NVIDIA's cuBLAS and cuDNN,
redistributed under the CUDA EULA and the cuDNN SLA as a component of this
application. *This software contains source code provided by NVIDIA Corporation.*
The license texts are inside the archive and are unpacked next to the libraries.
Details are in [NOTICE.md](NOTICE.md).
