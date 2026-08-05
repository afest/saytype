"""WASAPI loopback capture module for saytype call-recording skeleton.

Pinned: PyAudioWPatch 0.2.12.8 (MIT, fork of pyaudio with WASAPI loopback patch).
Pattern adapted from s0d3s/PyAudioWPatch/examples/pawp_record_wasapi_loopback.py.

Walking skeleton (T-171). Production version with mitigations — T-172.
"""

from __future__ import annotations

import sys
from typing import Callable, Optional

import pyaudiowpatch as pyaudio


def find_loopback_device(pa: pyaudio.PyAudio, device_name: Optional[str] = None) -> dict:
    """Find a WASAPI loopback device.

    If device_name is None — use default playback (Windows "Звук" → Воспроизведение).
    Pattern: get default output, then walk loopback generator looking for name match.

    Returns the device info dict from pyaudiowpatch.
    Raises RuntimeError on failure (WASAPI unavailable, no loopback for endpoint, etc.).
    """
    try:
        wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    except OSError as exc:
        raise RuntimeError("WASAPI host API not available") from exc

    if device_name is None:
        target = pa.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
    else:
        target = None
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info["hostApi"] == wasapi_info["index"] and device_name.lower() in info["name"].lower():
                target = info
                break
        if target is None:
            raise RuntimeError(f"WASAPI device matching {device_name!r} not found")

    if target.get("isLoopbackDevice"):
        return target

    for loopback in pa.get_loopback_device_info_generator():
        if target["name"] in loopback["name"]:
            return loopback

    raise RuntimeError(f"No loopback companion for endpoint {target['name']!r}")


def open_loopback_stream(
    pa: pyaudio.PyAudio,
    device: dict,
    callback: Callable,
    chunk_size: int = 2048,
) -> pyaudio.Stream:
    """Open a WASAPI loopback input stream on the given device.

    callback signature (PyAudioWPatch / PortAudio):
        (in_data: bytes, frame_count: int, time_info: dict, status: int)
            -> (out_data: bytes | None, flag: int)
    Return (None, pyaudio.paContinue) for capture-only streams.
    """
    channels = int(device["maxInputChannels"])
    sample_rate = int(device["defaultSampleRate"])

    stream = pa.open(
        format=pyaudio.paInt16,
        channels=channels,
        rate=sample_rate,
        frames_per_buffer=chunk_size,
        input=True,
        input_device_index=device["index"],
        stream_callback=callback,
    )
    return stream


if __name__ == "__main__":
    with pyaudio.PyAudio() as p:
        dev = find_loopback_device(p)
        print(
            f"loopback device: #{dev['index']} {dev['name']!r}",
            f"rate={int(dev['defaultSampleRate'])} channels={int(dev['maxInputChannels'])}",
            sep="\n  ",
            file=sys.stderr,
        )
