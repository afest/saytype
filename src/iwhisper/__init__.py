"""iwhisper — GPU-accelerated system-wide dictation and call recording for Windows.

Public entry points:

    python -m iwhisper                  launch the tray application
    python -m iwhisper.transcribe_call  record a call from the command line

``iwhisper.engine`` is the only place that loads a Whisper model and can be used
on its own for batch transcription::

    from iwhisper import engine
    model = engine.load_model("small")
"""

__version__ = "0.1.1"

__all__ = ["__version__"]
