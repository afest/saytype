# Architecture decisions

Why the code looks the way it does. Every entry here cost something to learn —
usually a crash, a lost recording, or a week of a wrong assumption.

## 1. Two UI layers, not one

`transcribe_ui.py` owns recording, transcription, rotation, the global hotkey and
the Qt application object. `transcribe_ui_window.py` owns everything visual. They
talk through callbacks (`history_dir_getter`, `rotation_count_getter`) and Qt
signals (`settings_changed`, `quit_requested`).

The split is not aesthetic: it lets the UI file be imported and exercised in a
smoke test without starting audio capture, and it keeps the single-instance guard
in one place.

A `tkinter` splash still exists on top of both. PySide6 and faster-whisper take
5–10 seconds to import; without it, double-clicking the shortcut looks like
nothing happened. `tkinter` is in the standard library and imports in ~0.5 s.

## 2. Qt for the tray, Win32 for the hotkey

The tray icon is `QSystemTrayIcon`, not `pystray`: two event loops (a `pystray`
Win32 loop and `QApplication`) fight over the main thread.

The global hotkey is `RegisterHotKey` through a `QAbstractNativeEventFilter`, not
`pynput`. A low-level keyboard hook sees synthetic keystrokes with
`LLKHF_INJECTED` set and filters them, which breaks macro keyboards. Pasting is
done with `keybd_event` (VK_CONTROL / VK_V), which is independent of the keyboard
layout.

The foreground window is captured **when the recording stops**, not when it
starts — during a long dictation you often click somewhere else, and the text
should land where you are looking now.

## 3. One place loads a model — `engine.py`

CUDA DLL setup, model presets, downloads, the `compute_type` fallback chain, the
model cache and unloading all live in `engine.py`. It is the only module that
constructs a `WhisperModel`.

Before that, the same logic existed in three copies (the UI, the call recorder,
the batch CLI) and drifted. `engine` imports neither Qt nor anything else from the
package except `profile`, so it can be used on its own for batch work.

Its import has a side effect on purpose: `setup_cuda_dll_paths()` runs at module
level, because the pip packages `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` put
their DLLs in `site-packages`, where the classic Windows DLL search never looks —
and this has to happen **before** anything imports `ctranslate2`.

## 4. Never destroy a CUDA model object

CTranslate2 4.7.1 kills the process with `0xC0000409` (fail-fast, past any
`try/except`) if a `WhisperModel` is destroyed after generation that used
sampling — which is exactly what the default temperature fallback does, so it
happens on every real transcription.

Consequences, both non-obvious:

- Unloading goes through `engine.unload_model()` → `_release()`: VRAM is returned
  by an explicit CTranslate2 call, and the Python wrapper is parked in a list
  instead of being freed.
- Every exit path of a process that loaded a model goes through
  `engine.hard_exit()` instead of `sys.exit`, so the interpreter's finalizer never
  gets to the model. On Windows that is `TerminateProcess`, not `os._exit`: the
  latter tears down the process while background threads are still running, and
  Windows logs a perfectly normal exit as an application crash.

## 5. The temperature fallback stays on

Passing `temperature=0.0` as a single value looks like the deterministic choice.
It also disables faster-whisper's temperature fallback, and decoding gets stuck in
loops — the same phrase repeated until the audio ends. Passing the tuple
`(0.0, 0.2, … 1.0)` keeps the documented anti-loop mechanism: when the compression
ratio or the log probability crosses a threshold, the model retries hotter.

Related: the language is passed as `language="ru"` rather than hinted inside the
prompt. The word "русском" inside `initial_prompt` became a loop trigger of its
own on quiet recordings.

## 6. Vocabulary and replacements are data

The dictionary (`initial_prompt`) and the replacement rules are files in the user
profile, empty out of the box — not constants in the source. Two things follow
from making them data: the app can be handed to someone else, and features like
"you corrected this word three times, add it to the dictionary?" become possible
without touching the code.

The prompt has a hard budget of 223 tokens (`max_length // 2 - 1`) and the excess
is cut **from the front** of the string, silently. So there is a counter in the
settings dialog and a warning in the log at model load. This was found the
expensive way: a prompt grew past the limit and the words it was supposed to fix
stopped being seen by the model, with no error anywhere.

Replacement rules split into ordinary and *end of text only*. The second kind
exists because Whisper hallucinates video-outro phrases over trailing silence.
They are anchored to `$` and are **not** applied to call transcripts, where
segments are short and a closing line is usually real speech.

## 7. Hybrid processing: batch under a threshold, streaming over it

Short recordings are transcribed in one pass when you stop. Long ones run a
background worker during the recording (Local Agreement-2 with tail and head
merge), so most of the work is done by the time you release the hotkey.

Streaming wins by more the longer the recording (≈2× at 15 s, ≈5.7× at 60 s
against a single pass) and loses on short ones, where a full pass is both fast and
more accurate. Hence the threshold instead of a fixed choice; the default is
automatic, and the user can force either mode.

Merging confirmed and tail text deduplicates the overlap by comparing word
suffixes and prefixes. It is not perfect: if the model transcribed the boundary
word differently in the two passes, the overlap is not found and both survive.
That was chosen over the alternative — cutting text that turns out to be real.

## 8. Call recording: two channels, no mixing

The microphone and the WASAPI loopback of the default playback device are
recorded separately and transcribed separately. Speaker separation is therefore
deterministic — it is which cable the audio came from, not a diarization model.

The consequences are what you have to design around:

- **Loopback goes silent legitimately.** With nothing playing, WASAPI simply does
  not fire the callback. A watchdog warns after 3 s and alerts the UI after 5 s
  rather than trying to re-open the stream.
- **Loopback also lies.** If an active-but-silent audio session sits on the
  default endpoint (the call plays on a headset while the default output is a
  monitor), callbacks keep arriving full of zeros. A separate 60-second threshold
  catches that; the pause is long so natural silence does not trip it.
- **Levels differ by ~30 dB.** Loopback captures the digital PCM stream after the
  system mixer; a microphone captures acoustic pressure. That is physics, not a
  bug, and it is why the channels are never mixed into one file.

## 9. Durability while recording, not after

Audio is streamed to disk by a dedicated writer thread while the call is running
(raw sidecar files flushed about once a second). The capture callbacks themselves
never touch the disk — they must stay realtime.

A crash, a BSOD or a power loss therefore costs one or two seconds, not the whole
call. On the next start, orphaned raw files are found, assembled into a WAV, and
the user is offered a transcript. The same assembly function is used by the normal
stop path, so the two channels cannot drift apart in one path and not the other.

After a successful transcript the WAV is **not** deleted: it stays as a rotating
buffer of the last N recordings, so a bad transcript can be redone from the audio.

## 10. Crashes that leave no traceback

`sys.excepthook` and `threading.excepthook` only see Python exceptions. A native
crash, the OOM killer, a BSOD or an external `taskkill` leave nothing at all —
under `pythonw.exe` the window simply disappears.

`crashguard.py` keeps a marker file describing what the session is doing. A clean
exit removes it. If the next start finds one, the previous session died, and the
report is written with the pid, the start time, the last action and whatever
context can be collected. The fact is written and flushed **first**; the context
collector runs after, so a broken collector cannot swallow the evidence.

## 11. Qt objects belong to the GUI thread

A timer created on the GUI thread was stopped and dropped from a worker thread.
`stop()` from another thread is silently ignored by Qt, and dropping the last
Python reference destroys the C++ object — leaving the main thread's event
dispatcher with a dangling pointer and an access violation on the next tick.

The fix is a generation counter: the worker bumps an integer, and a `singleShot`
callback checks whether its generation is still current. An integer is safe from
any thread; a `QTimer` is not.

## 12. Older GPUs are supported by falling back, not by detecting

The `compute_type` chain is `float16 → int8_float16 → int8_float32 → float32 →
cpu/int8`, tried in order. No capability detection, no card whitelist: what
actually loads on a given driver and CTranslate2 build is hard to predict, and
trying is cheap and honest. Pascal-era cards end up two or three steps down the
chain and still work.

## 13. Input quality is measured from the signal, not asked of the driver

A narrowband source — a webcam microphone, or a Bluetooth headset in Hands-Free
mode — reports itself to Windows as an ordinary 48 kHz device.
`sd.InputStream(samplerate=48000)` opens on it without an error and the audio
arrives upsampled from 16 kHz, with nothing above 8 kHz. Settings keep showing
"hi-fi 48 000 Hz" while every recording is quietly ruined; that cost three days
of voice-clone material before it was noticed.

So `audio_quality.py` measures the signal instead: how far the 9.5–14 kHz band
sits below the 0.3–3 kHz speech band, over the loudest 20 % of frames. A live
microphone has room noise and sibilants up there; an upsampled 16 kHz stream has
digital silence. On a labelled set of 420 real recordings the two populations are
separated by more than 20 dB, and the threshold sits between them.

The result is surfaced where the decision is made: the settings row names the
device Windows currently considers default, a "Check" button records three
seconds and says what it heard, and the hi-fi banner names the microphone and
turns red when the last recording came from a narrowband source. Silence, clips
shorter than a second and genuinely 16 kHz sources produce no verdict at all — a
false alarm in the background devalues the real one.

## 14. The interface is a separate layer behind a fixed window contract

`transcribe_ui.py` talks to the window through a narrow contract: the constructor
arguments, a set of `notify_*` methods (each one only emits a queued Qt signal) and
three signals back. The V5 interface (`saytype.ui`) implements exactly that contract,
so recording, hotkeys, paste, tray and transcription are untouched by the redesign.
The previous window stays in the repository and is selected with `SAYTYPE_UI=legacy`.

The design prototype was HTML, but it is ported to Qt Widgets rather than embedded:
QtWebEngine would add a Chromium runtime and a JS bridge next to audio capture.
Digits, icons and the brand mark are the prototype's SVG paths, parsed into
`QPainterPath` by a small parser (`ui/svgpath.py`) because `QtSvgWidgets` is
excluded from the build.

All colours, type, grid rhythm, hatching and motion live in `ui/tokens.py`. Only
visible widgets animate: a hidden window or an inactive page gets values instantly,
and no timer runs while idle. The recording clock reads a monotonic clock and shows
the current second; it never replays missed ticks, and its start animation runs
alongside capture rather than delaying it.

Performance is compared with `tools/bench_matrix.ps1`: both windows alternate in
blocks over the same core code, model, device, mode and audio, with the window
rendered on Qt's offscreen platform.

## Deliberately not done

- **Diarization by model.** Two channels already separate speakers for calls, and
  for dictation there is only one.
- **Cloud fallback.** The point is that audio never leaves the machine.
- **Bundled ffmpeg.** GPL — see [NOTICE.md](../NOTICE.md).
- **Cross-platform support.** WASAPI loopback, `RegisterHotKey` and the paste path
  are all Win32. `engine.py` is portable; the application is not.
