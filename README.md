# iwhisper

System-wide dictation for Windows that runs Whisper on your GPU. Press a hotkey,
talk, release — the text is pasted into whatever window you were in. Nothing
leaves your machine.

It also records calls: your microphone and the system output are captured as two
separate channels, so the transcript comes out with speakers already separated.

> **Status:** works, used daily by its author, but rough around the edges. There
> is no installer yet — you run it from a Python environment.

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
- Python 3.10+ (3.12 recommended).
- NVIDIA GPU with CUDA 12.x for the fast path. **Without one it still works** —
  CTranslate2 falls back to CPU int8 automatically, just slower; pick a smaller
  model in that case.
- ~0.5–2 GB of disk for model weights, downloaded on first run.
- Optional: `ffmpeg` in `PATH`, only if you want an MP3 archive of recorded
  calls. Without it the WAV is kept and nothing fails.

## Install

```powershell
git clone https://github.com/afest/iwhisper
cd iwhisper
pip install --user -r requirements.txt
pip install --user -e .
```

If CTranslate2 cannot find `cublas` / `cudnn`, install the CUDA runtime from pip
instead of a full toolkit:

```powershell
pip install --user "iwhisper[cuda]"
```

## Run

```powershell
pythonw.exe -m iwhisper      # no console window
python -m iwhisper           # same, but with a console for logs
```

The app lives in the tray. Double-click the tray icon for the history window;
right-click for settings and exit.

| Action | Default |
|---|---|
| Dictate (hold-free toggle) | `Ctrl+Shift+Q` |
| Record a call | `Ctrl+Shift+E` |

The dictation hotkey is configurable in settings and applies immediately, without
a restart.

## Where your data lives

Everything is under `%LOCALAPPDATA%\iwhisper`:

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

No telemetry, no network calls except downloading model weights from Hugging Face.

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
  last second or two, not the whole call. On the next start iwhisper offers to
  finish an interrupted recording.

## Batch transcription

`iwhisper.engine` is standalone — no Qt, no UI:

```python
from iwhisper import engine

model = engine.load_model("large-v3-turbo")
segments, info = model.transcribe("call.wav", language="ru", beam_size=5)
print(" ".join(s.text.strip() for s in segments))
```

It owns CUDA DLL setup on Windows, model download with progress, the
`compute_type` fallback chain (`float16 → int8_float16 → int8_float32 → float32
→ cpu/int8`), and unloading from VRAM.

To rebuild a transcript from an already recorded call:

```powershell
python -m iwhisper.retranscribe_call            # newest WAV in the calls folder
python -m iwhisper.retranscribe_call "path.wav"
```

## Models

Six presets from `tiny` to `large-v3`, plus "custom": any Hugging Face repo id or
a local folder holding a CTranslate2 model. Weights are downloaded to
`%LOCALAPPDATA%\iwhisper\models` with a progress dialog. Switching models at
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
- **No installer.** Packaging and auto-update are the next step.
- **Older GPUs** (Pascal and similar) work, but through compute types that are
  not the fastest available. The fallback chain sorts it out automatically.
- **The first run is slow** — 5–10 seconds of imports before the window appears,
  plus the model download.

## License

MIT — see [LICENSE](LICENSE). Third-party components and what changes when you
bundle them are in [NOTICE.md](NOTICE.md).
