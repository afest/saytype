"""saytype — GPU-accelerated system-wide dictation and call recording for Windows.

Public entry points:

    python -m saytype                  launch the tray application
    python -m saytype.transcribe_call  record a call from the command line

``saytype.engine`` is the only place that loads a Whisper model and can be used
on its own for batch transcription::

    from saytype import engine
    model = engine.load_model("small")
"""

__version__ = "0.1.2"

__all__ = ["__version__"]
