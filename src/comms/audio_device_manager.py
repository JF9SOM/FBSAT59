"""Shared soundcard access for the Communications tabs.

CW Decoder, SSTV/SSDV, FT4, Q65, and APRS (Direwolf) can all be open at the
same time and all read from — or write to — the single soundcard device
configured in Rig Settings > Sound Card. Two tabs each calling
``sounddevice.InputStream()`` directly on the same device index either raise
a "device busy" error (ALSA ``hw:`` devices reject a second open) or silently
starve each other of samples.

This module gives every consumer a single point of contact for that shared
hardware:

  - RX (input) is opened once per device and fanned out to every subscriber
    (pub/sub), since multiple decoders reading the same incoming audio is a
    legitimate use case (e.g. CW Decoder and SSTV open at the same time).
  - TX (output) is exclusively locked to one owner at a time, since two tabs
    transmitting simultaneously would just garble the audio on air.

Linux/PipeWire pinning
-----------------------
On Linux, ``sounddevice``/PortAudio can only target the generic ALSA
``pipewire`` compatibility device (the real hardware nodes are held
exclusively by PipeWire and reject a second open). That generic device
always follows whatever PipeWire currently considers the *default* sink or
source, which silently changes when other USB audio devices are plugged in
or removed — so a rig's soundcard can end up receiving or sending audio
without any error, just not through the device the user picked in Rig
Settings > Sound Card.

When the user has explicitly picked a target sink/source name there (stored
as ``output_sink_name`` / ``input_source_name`` in ``soundcard_settings``),
this module uses ``pactl move-sink-input`` / ``move-source-output`` right
after a stream is opened to force it onto that specific device, regardless
of whatever PipeWire's current default happens to be. This is a no-op on
non-Linux platforms and when no pin target is configured.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, cast

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import QObject, Signal

from i18n import _

try:
    from scipy import signal as sp_signal

    _SCIPY_AVAILABLE: bool = True
except ImportError:
    sp_signal = None
    _SCIPY_AVAILABLE = False

# Every shared input stream is opened at this rate; each subscriber's audio
# is resampled from here to whatever rate it asked for.
_HW_SAMPLE_RATE = 48_000

# Some USB audio interfaces (rig soundcards in particular) and/or PipeWire's
# routing only settle into the real input level/target a moment after a
# stream first opens -- closing and reopening once, a short while later,
# has been observed to fix this reliably. See _SharedInputStream._open().
_REOPEN_SETTLE_DELAY_S = 1.5


def _pactl_available() -> bool:
    """True when running on Linux with a usable ``pactl`` binary."""
    return sys.platform == "linux" and shutil.which("pactl") is not None


def _read_pin_targets() -> tuple[str | None, str | None]:
    """Read (output_sink_name, input_source_name) from soundcard_settings.

    Returns (None, None) when unset, unreadable, or pactl is unavailable.
    """
    if not _pactl_available():
        return None, None
    try:
        import sqlite3

        from data.database import get_db_path

        conn = sqlite3.connect(str(get_db_path()))
        try:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = 'soundcard_settings'"
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None, None
        data = json.loads(row[0])
        return data.get("output_sink_name"), data.get("input_source_name")
    except Exception:
        return None, None


def _snapshot_stream_ids(kind: str) -> set[str]:
    """Return the current set of pactl object ids for `kind`.

    `kind` is "sink-inputs" (playback streams) or "source-outputs"
    (capture streams).
    """
    try:
        result = subprocess.run(
            ["pactl", "list", "short", kind],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return {line.split("\t", 1)[0] for line in result.stdout.splitlines() if line.strip()}
    except Exception:
        return set()


def _pin_new_stream(kind: str, move_verb: str, target: str, before: set[str]) -> None:
    """Move the stream that appeared after `before` was snapshotted to `target`.

    Polls briefly (up to ~1s) since the new stream may not show up in pactl's
    listing the instant the sounddevice stream starts. Does nothing if zero
    or more than one new stream shows up (ambiguous — safer to leave routing
    alone than move the wrong stream).
    """
    for _attempt in range(20):
        new_ids = _snapshot_stream_ids(kind) - before
        if len(new_ids) == 1:
            with contextlib.suppress(Exception):
                subprocess.run(
                    ["pactl", move_verb, next(iter(new_ids)), target],
                    capture_output=True,
                    timeout=2,
                )
            return
        if len(new_ids) > 1:
            return
        time.sleep(0.05)


AudioCallback = Callable[[NDArray[np.float32]], None]


def _resample(chunk: NDArray[np.float32], src_rate: int, dst_rate: int) -> NDArray[np.float32]:
    """Resample a mono float32 chunk from src_rate to dst_rate.

    Always anti-alias filters before downsampling. Naive stride decimation
    (picking every Nth sample with no low-pass filter) folds all energy
    above the new Nyquist frequency straight back into the passband —
    for the common 48000->12000 (FT4) and 48000->3200 (CW) ratios this adds
    several dB of noise across the entire decode band, which is fatal for
    weak-signal digital modes.
    """
    if src_rate == dst_rate or len(chunk) == 0:
        return chunk
    if _SCIPY_AVAILABLE:
        g = np.gcd(src_rate, dst_rate)
        resampled = sp_signal.resample_poly(chunk, dst_rate // g, src_rate // g)
        return cast(NDArray[np.float32], resampled.astype(np.float32))
    n_out = max(1, round(len(chunk) * dst_rate / src_rate))
    x_old = np.linspace(0.0, 1.0, num=len(chunk), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, chunk).astype(np.float32)


def _validate_input_device(device: int | None) -> None:
    """Raise a clear, actionable error if `device` cannot actually record audio.

    PortAudio device indices are not stable across sessions on Linux: they
    shift whenever ALSA/PipeWire's device list changes (e.g. a USB rig
    soundcard being plugged in adds entries ahead of the generic
    "pipewire"/"default" devices). A stale index silently pointing at an
    unrelated, input-less device is what let the FT4 tab report zero audio
    while Rig Settings' own meter — which re-enumerates fresh every time it
    opens — worked fine on the same machine at the same time (2026-07-06).

    No-ops for `device=None` (system default, always valid) and when
    ``sounddevice`` doesn't expose ``query_devices`` (e.g. the fakes used in
    tests) — in both cases the real `sd.InputStream()` call is left to
    surface whatever error actually occurs.
    """
    if device is None:
        return
    import sounddevice as sd

    query_devices = getattr(sd, "query_devices", None)
    if query_devices is None:
        return
    try:
        devices = query_devices()
    except Exception:
        return
    if not (0 <= device < len(devices)):
        raise RuntimeError(
            _(
                "Input device #{device} no longer exists (device list now has "
                "{count} entries) — reopen Rig Settings > Sound Card and "
                "re-select the input device."
            ).format(device=device, count=len(devices))
        )
    if devices[device].get("max_input_channels", 0) <= 0:
        raise RuntimeError(
            _(
                "Input device #{device} ({name}) has no input channels — "
                "reopen Rig Settings > Sound Card and re-select the input "
                "device."
            ).format(device=device, name=devices[device].get("name", "?"))
        )


def validate_output_device(device: int | None, samplerate: int, channels: int = 1) -> None:
    """Raise a clear, actionable error if `device` cannot play back audio at
    the given samplerate/channels.

    Mirrors :func:`_validate_input_device`, but a stale output device index
    is not necessarily *channel-less* — it can still have output channels
    while simply not supporting the specific samplerate a caller needs
    (e.g. a PipeWire/ALSA index drift resolving to a "surroundNN" plugin
    device instead of the generic "pipewire"/"default" node: it has output
    channels, but rejects FT4's 12 kHz mono stream with PortAudio's opaque
    "Invalid sample rate" [-9997]). So this checks the actual
    samplerate/channels combination via ``sd.check_output_settings()``
    rather than just a channel count, and only after that preflight check
    passes does the real ``sd.play()``/``sd.OutputStream()`` get attempted.

    No-ops for `device=None` (system default, always valid) and when
    ``sounddevice`` doesn't expose ``query_devices``/``check_output_settings``
    (e.g. the fakes used in tests) — in both cases the real playback call is
    left to surface whatever error actually occurs.
    """
    if device is None:
        return
    import sounddevice as sd

    query_devices = getattr(sd, "query_devices", None)
    check_output_settings = getattr(sd, "check_output_settings", None)
    if query_devices is None or check_output_settings is None:
        return
    try:
        devices = query_devices()
    except Exception:
        return
    if not (0 <= device < len(devices)):
        raise RuntimeError(
            _(
                "Output device #{device} no longer exists (device list now "
                "has {count} entries) — reopen Rig Settings > Sound Card "
                "and re-select the output device."
            ).format(device=device, count=len(devices))
        )
    try:
        check_output_settings(device=device, samplerate=samplerate, channels=channels)
    except Exception:
        raise RuntimeError(
            _(
                "Output device #{device} ({name}) cannot play audio at "
                "{rate} Hz — reopen Rig Settings > Sound Card and "
                "re-select the output device."
            ).format(
                device=device,
                name=devices[device].get("name", "?"),
                rate=samplerate,
            )
        ) from None


def snapshot_output_streams() -> set[str]:
    """Current pactl sink-input ids — for ad-hoc pinning outside the TX lock
    (e.g. the Rig Settings > Sound Card "Test" button)."""
    return _snapshot_stream_ids("sink-inputs")


def pin_output_stream(target: str, before: set[str]) -> None:
    """Move the sink-input that appeared after `before` was snapshotted to
    `target` — see `snapshot_output_streams()`."""
    _pin_new_stream("sink-inputs", "move-sink-input", target, before)


class _SharedInputStream:
    """A single real sounddevice.InputStream, fanned out to N subscribers."""

    def __init__(self, device: int | None) -> None:
        self._device = device
        self._stream: Any = None
        self._subscribers: dict[str, tuple[int, AudioCallback]] = {}
        self._lock = threading.Lock()

    def add_subscriber(self, owner: str, samplerate: int, callback: AudioCallback) -> None:
        with self._lock:
            self._subscribers[owner] = (samplerate, callback)
            if self._stream is None:
                try:
                    self._open()
                except Exception:
                    # Don't leave a subscriber registered against a stream
                    # that never actually started.
                    self._subscribers.pop(owner, None)
                    raise

    def pause(self) -> bool:
        """Stop the underlying hardware stream without dropping subscribers.

        Used to avoid having this input stream open at the same moment a TX
        worker opens an output stream on the same physical device — some
        single-codec USB audio interfaces (e.g. a rig's built-in sound card)
        have been observed to stall for several seconds when both directions
        are opened concurrently (GitHub Issue #26). Subscribers are left
        registered; call `resume()` afterwards to reopen.

        Returns True if a running stream was actually stopped, False if
        there was nothing to pause (already paused, or never opened).

        Releases the lock before calling `stream.stop()`, same as
        `remove_subscriber()` — that call blocks until the audio callback
        thread returns, and that thread also needs this lock.
        """
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is None:
            return False
        with contextlib.suppress(Exception):
            stream.stop()
            stream.close()
        return True

    def resume(self) -> None:
        """Reopen the hardware stream after `pause()`, if subscribers are
        still registered and it isn't already open (a subscriber may have
        re-added itself in the meantime, opening a fresh stream already)."""
        with self._lock:
            if self._stream is not None or not self._subscribers:
                return
            with contextlib.suppress(Exception):
                self._open(schedule_settle_reopen=False)

    def remove_subscriber(self, owner: str) -> bool:
        """Unsubscribe `owner`. Returns True once no subscribers remain.

        `stream.stop()` blocks until the audio callback thread has returned
        from its current invocation — and that thread also needs `self._lock`
        (see `_on_audio`) to finish. Stopping the stream while still holding
        the lock would deadlock the two threads against each other, so the
        stream handle is claimed under the lock but actually stopped/closed
        after releasing it.
        """
        with self._lock:
            self._subscribers.pop(owner, None)
            if self._subscribers:
                return False
            stream, self._stream = self._stream, None
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.stop()
                stream.close()
        return True

    def _open(self, schedule_settle_reopen: bool = True) -> None:
        import sounddevice as sd

        _validate_input_device(self._device)

        _, in_target = _read_pin_targets()
        before = _snapshot_stream_ids("source-outputs") if in_target else None

        stream = sd.InputStream(
            samplerate=_HW_SAMPLE_RATE,
            channels=1,
            dtype="float32",
            device=self._device,
            callback=self._on_audio,
        )
        stream.start()
        self._stream = stream

        if in_target and before is not None:
            _pin_new_stream("source-outputs", "move-source-output", in_target, before)

        if schedule_settle_reopen:
            timer = threading.Timer(_REOPEN_SETTLE_DELAY_S, self._reopen_once, args=(stream,))
            timer.daemon = True
            timer.start()

    def _reopen_once(self, opened_stream: Any) -> None:
        """Transparently close and reopen the hardware stream once, so a
        quiet/misrouted first open self-heals without subscribers noticing
        (they only ever see the pub/sub interface, never the raw stream).

        No-ops if `opened_stream` is no longer the live stream — it was
        already replaced (subscribers all left and came back, or this is a
        stale timer from a previous generation).
        """
        with self._lock:
            if self._stream is not opened_stream or not self._subscribers:
                return
            old_stream = self._stream
            self._stream = None
        with contextlib.suppress(Exception):
            old_stream.stop()
            old_stream.close()
        with self._lock:
            if self._subscribers and self._stream is None:
                self._open(schedule_settle_reopen=False)

    def _on_audio(
        self, indata: NDArray[np.float32], frames: int, time_info: Any, status: Any
    ) -> None:
        chunk = indata[:, 0].copy()
        with self._lock:
            subs = list(self._subscribers.values())
        for samplerate, callback in subs:
            with contextlib.suppress(Exception):
                callback(_resample(chunk, _HW_SAMPLE_RATE, samplerate))


class AudioDeviceManager(QObject):
    """Process-wide coordinator for shared RX streams and exclusive TX locks."""

    #: Emitted whenever the TX owner of `device` changes — `owner` is the new
    #: owner name, or None once the device becomes free again. Purely
    #: informational (e.g. a status bar indicator); the exclusive-lock
    #: decision itself is made by acquire_output()'s return value, not by
    #: subscribers of this signal. Safe to connect from the UI thread even
    #: though acquire_output()/release_output() are usually called from
    #: background TX worker threads — Qt auto-queues the delivery.
    tx_owner_changed = Signal(object, object)

    _instance: AudioDeviceManager | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        super().__init__()
        self._inputs: dict[int, _SharedInputStream] = {}
        self._inputs_lock = threading.Lock()
        self._tx_owners: dict[int, str] = {}
        self._tx_lock = threading.Lock()
        self._tx_pin_snapshots: dict[str, set[str]] = {}

    @classmethod
    def instance(cls) -> AudioDeviceManager:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @staticmethod
    def _key(device: int | None) -> int:
        return -1 if device is None else device

    # ------------------------------------------------------------------ #
    # RX — shared input (pub/sub)
    # ------------------------------------------------------------------ #

    def acquire_input(
        self, owner: str, device: int | None, samplerate: int, callback: AudioCallback
    ) -> None:
        """Subscribe `owner` to audio from `device`, delivered at `samplerate`.

        Opens the underlying hardware stream on the first subscriber; later
        subscribers on the same device share it (resampled as needed).
        Calling this again for an `owner` that is already subscribed just
        updates its callback/samplerate.
        """
        key = self._key(device)
        with self._inputs_lock:
            stream = self._inputs.get(key)
            if stream is None:
                stream = _SharedInputStream(device)
                self._inputs[key] = stream
        stream.add_subscriber(owner, samplerate, callback)

    def release_input(self, owner: str, device: int | None) -> None:
        """Unsubscribe `owner` from `device`, closing the hardware stream if
        `owner` was the last subscriber."""
        key = self._key(device)
        with self._inputs_lock:
            stream = self._inputs.get(key)
            if stream is None:
                return
            if stream.remove_subscriber(owner):
                del self._inputs[key]

    def pause_input(self, device: int | None) -> bool:
        """Temporarily stop the shared RX stream on `device`, if one is
        active, without dropping its subscribers — see
        `_SharedInputStream.pause()`. Returns True if something was
        actually paused (the caller must call `resume_input()` once done);
        False if there is no RX stream on this device to pause (e.g. RX and
        TX are on different devices, or nothing is subscribed to RX at all)."""
        key = self._key(device)
        with self._inputs_lock:
            stream = self._inputs.get(key)
        if stream is None:
            return False
        return stream.pause()

    def resume_input(self, device: int | None) -> None:
        """Reopen the shared RX stream on `device` after `pause_input()`."""
        key = self._key(device)
        with self._inputs_lock:
            stream = self._inputs.get(key)
        if stream is not None:
            stream.resume()

    # ------------------------------------------------------------------ #
    # TX — exclusive output lock
    # ------------------------------------------------------------------ #

    def acquire_output(self, owner: str, device: int | None) -> bool:
        """Try to claim exclusive use of the output device for `owner`.

        Returns True if claimed (or already held by `owner`), False if a
        different owner currently holds it. When a Linux pin target is
        configured, also snapshots current pactl sink-inputs so a later
        `pin_active_output(owner)` call can find the stream `owner` is about
        to open.
        """
        key = self._key(device)
        with self._tx_lock:
            current = self._tx_owners.get(key)
            if current is not None and current != owner:
                return False
            became_owner = current != owner
            self._tx_owners[key] = owner
            out_target, _ = _read_pin_targets()
            if out_target:
                self._tx_pin_snapshots[owner] = _snapshot_stream_ids("sink-inputs")
        if became_owner:
            self.tx_owner_changed.emit(device, owner)
        return True

    def release_output(self, owner: str, device: int | None) -> None:
        """Release `owner`'s exclusive claim on the output device, if held."""
        key = self._key(device)
        released = False
        with self._tx_lock:
            if self._tx_owners.get(key) == owner:
                del self._tx_owners[key]
                released = True
            self._tx_pin_snapshots.pop(owner, None)
        if released:
            self.tx_owner_changed.emit(device, None)

    def pin_active_output(self, owner: str) -> None:
        """Pin `owner`'s just-started output stream to the configured target.

        Call this right after starting a *non-blocking* sounddevice output
        stream (before waiting for it to finish) — the stream needs to
        already exist in pactl's listing for this to find it. No-op when no
        output pin target is configured or `acquire_output` wasn't called
        first.
        """
        with self._tx_lock:
            before = self._tx_pin_snapshots.pop(owner, None)
        if before is None:
            return
        out_target, _ = _read_pin_targets()
        if out_target:
            _pin_new_stream("sink-inputs", "move-sink-input", out_target, before)

    def output_owner(self, device: int | None) -> str | None:
        """Return the name of the current TX owner of `device`, if any."""
        key = self._key(device)
        with self._tx_lock:
            return self._tx_owners.get(key)


def get_audio_device_manager() -> AudioDeviceManager:
    """Return the process-wide AudioDeviceManager singleton."""
    return AudioDeviceManager.instance()
