"""Tests for AudioFilePlayer and Radio Control's recording-playback buttons."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pytestqt.qtbot import QtBot

sf = pytest.importorskip("soundfile")
sd = pytest.importorskip("sounddevice")

from comms.audio_device_manager import DeviceFeedSink  # noqa: E402
from comms.audio_player import AudioFilePlayer  # noqa: E402

RATE = 8000


class _FakeStream:
    """Stand-in for sounddevice.OutputStream that never touches an audio device."""

    instances: list[_FakeStream] = []

    def __init__(self, **kwargs: Any) -> None:
        self.callback = kwargs["callback"]
        self.started = False
        self.closed = False
        _FakeStream.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def wav_path(tmp_path: Path) -> str:
    """A 2-second mono WAV file."""
    path = tmp_path / "rec.wav"
    sf.write(str(path), np.linspace(-0.5, 0.5, RATE * 2, dtype="float32"), RATE)
    return str(path)


@pytest.fixture(autouse=True)
def fake_output_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeStream.instances.clear()
    monkeypatch.setattr(sd, "OutputStream", _FakeStream)


def _pull(stream: _FakeStream, frames: int) -> tuple[np.ndarray, bool]:
    """Run the audio callback once; returns (buffer, stopped-at-end)."""
    out = np.ones((frames, 1), dtype="float32")
    try:
        stream.callback(out, frames, None, None)
    except sd.CallbackStop:
        return out, True
    return out, False


class TestAudioFilePlayer:
    def test_load_reports_duration(self, wav_path: str) -> None:
        p = AudioFilePlayer()
        assert not p.is_loaded
        p.load(wav_path)
        assert p.is_loaded
        assert p.duration_s == pytest.approx(2.0)
        assert p.position_s == 0.0
        p.close()

    def test_play_streams_samples_and_advances_position(self, wav_path: str) -> None:
        p = AudioFilePlayer()
        p.load(wav_path)
        p.play()
        assert p.is_playing
        _pull(_FakeStream.instances[-1], RATE // 2)
        assert p.position_s == pytest.approx(0.5)
        p.close()

    def test_pause_keeps_position_and_resume_continues(self, wav_path: str) -> None:
        p = AudioFilePlayer()
        p.load(wav_path)
        p.play()
        _pull(_FakeStream.instances[-1], RATE)
        p.pause()
        assert not p.is_playing
        assert p.position_s == pytest.approx(1.0)
        p.play()
        assert p.is_playing
        assert p.position_s == pytest.approx(1.0)
        p.close()

    def test_end_of_file_pads_silence_and_stops(self, wav_path: str) -> None:
        p = AudioFilePlayer()
        p.load(wav_path)
        p.play()
        out, stopped = _pull(_FakeStream.instances[-1], RATE * 3)
        assert stopped
        assert not p.is_playing
        assert np.all(out[RATE * 2 :] == 0.0)
        p.close()

    def test_play_after_end_restarts_from_beginning(self, wav_path: str) -> None:
        p = AudioFilePlayer()
        p.load(wav_path)
        p.play()
        _pull(_FakeStream.instances[-1], RATE * 3)
        p.play()
        assert p.is_playing
        assert p.position_s == 0.0
        p.close()

    def test_seek_relative_is_clamped(self, wav_path: str) -> None:
        p = AudioFilePlayer()
        p.load(wav_path)
        p.seek_relative(-5.0)
        assert p.position_s == 0.0
        p.seek_relative(1.5)
        assert p.position_s == pytest.approx(1.5)
        p.seek_relative(99.0)
        assert p.position_s == pytest.approx(2.0)
        p.close()

    def test_load_replaces_previous_file_and_close_releases(self, wav_path: str) -> None:
        p = AudioFilePlayer()
        p.load(wav_path)
        p.play()
        p.load(wav_path)
        assert not p.is_playing
        p.close()
        assert not p.is_loaded
        assert p.duration_s == 0.0

    def test_load_bad_file_raises_and_keeps_state(self, tmp_path: Path) -> None:
        bad = tmp_path / "nope.mp3"
        bad.write_bytes(b"not audio")
        p = AudioFilePlayer()
        with pytest.raises(Exception):  # noqa: B017 - soundfile's own error type
            p.load(str(bad))
        assert not p.is_loaded


class TestRadioControlPlaybackButtons:
    def _make(self, qtbot: QtBot) -> Any:
        from ui.radio_control_widget import RadioControlWidget

        w = RadioControlWidget()
        qtbot.addWidget(w)
        return w

    def test_buttons_are_icon_only_and_idle_state(self, qtbot: QtBot) -> None:
        w = self._make(qtbot)
        assert w._audio_stop_rec_btn.text() == "■"
        assert "STOP" not in w._audio_rec_btn.text()
        assert w._audio_rec_btn.text().endswith("REC")
        assert w._play_pause_btn.text() == "▶️"
        assert not w._play_pause_btn.isEnabled()
        assert not w._rew_btn.isEnabled()
        assert not w._ff_btn.isEnabled()
        assert w._play_open_btn.isEnabled()

    def test_open_loads_file_and_starts_playing(
        self, qtbot: QtBot, wav_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from PySide6.QtWidgets import QFileDialog

        w = self._make(qtbot)
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (wav_path, ""))
        w._on_play_open_clicked()

        assert w._audio_player.is_playing
        assert w._play_pause_btn.isEnabled()
        assert w._play_pause_btn.text() == "⏸️"
        assert w._rew_btn.isEnabled()
        assert w._ff_btn.isEnabled()
        assert w._audio_rec_status_label.text() == "00:00"

        w._play_pause_btn.click()
        assert not w._audio_player.is_playing
        assert w._play_pause_btn.text() == "▶️"

        w._play_pause_btn.click()
        assert w._audio_player.is_playing
        assert w._play_pause_btn.text() == "⏸️"
        w._play_pause_btn.click()

        w._on_seek_clicked(1.0)
        assert w._audio_player.position_s == pytest.approx(1.0)
        assert w._audio_rec_status_label.text() == "00:01"
        w.close()

    def test_button_returns_to_play_when_file_ends(
        self, qtbot: QtBot, wav_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from PySide6.QtWidgets import QFileDialog

        w = self._make(qtbot)
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (wav_path, ""))
        w._on_play_open_clicked()
        assert w._play_pause_btn.text() == "⏸️"

        _pull(_FakeStream.instances[-1], RATE * 3)  # runs off the end of the file
        w._update_play_state()  # what the 250 ms poll does

        assert w._play_pause_btn.text() == "▶️"
        assert w._play_pause_btn.isEnabled()
        w.close()

    def test_cancelled_dialog_changes_nothing(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from PySide6.QtWidgets import QFileDialog

        w = self._make(qtbot)
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: ("", ""))
        w._on_play_open_clicked()
        assert not w._audio_player.is_loaded
        assert not w._play_pause_btn.isEnabled()


def _wait_for(cond: Any, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return bool(cond())


def _pull_last(frames: int) -> bool:
    """Run the audio callback of the most recently opened fake stream."""
    return _pull(_FakeStream.instances[-1], frames)[1]


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.samples = 0
        self.rate = 0

    def begin(self) -> None:
        self.events.append("begin")

    def push(self, chunk: np.ndarray, samplerate: int) -> None:
        assert chunk.ndim == 1
        self.samples += len(chunk)
        self.rate = samplerate

    def end(self) -> None:
        self.events.append("end")


class TestPlayerFeed:
    def test_played_blocks_reach_the_sink_in_order_with_begin_and_end(self, wav_path: str) -> None:
        p = AudioFilePlayer()
        p.load(wav_path)
        sink = _RecordingSink()
        p.set_feed_sink(sink)
        p.play()
        _pull_last(RATE // 2)
        _pull_last(RATE // 2)
        p.pause()
        assert _wait_for(lambda: sink.events == ["begin", "end"])
        assert sink.samples == RATE
        assert sink.rate == RATE
        p.close()

    def test_end_of_file_ends_the_sink(self, wav_path: str) -> None:
        p = AudioFilePlayer()
        p.load(wav_path)
        sink = _RecordingSink()
        p.set_feed_sink(sink)
        p.play()
        assert _pull_last(RATE * 3) is True
        assert _wait_for(lambda: sink.events == ["begin", "end"])
        assert sink.samples == RATE * 2
        p.close()

    def test_no_sink_means_nothing_is_fed(self, wav_path: str) -> None:
        p = AudioFilePlayer()
        p.load(wav_path)
        p.play()
        _pull_last(RATE)
        p.close()

    def test_enabling_the_sink_mid_playback_starts_feeding(self, wav_path: str) -> None:
        p = AudioFilePlayer()
        p.load(wav_path)
        p.play()
        _pull_last(RATE // 2)
        sink = _RecordingSink()
        p.set_feed_sink(sink)
        _pull_last(RATE // 2)
        p.set_feed_sink(None)
        assert _wait_for(lambda: sink.events == ["begin", "end"])
        assert sink.samples == RATE // 2
        p.close()

    def test_resume_starts_a_new_session(self, wav_path: str) -> None:
        p = AudioFilePlayer()
        p.load(wav_path)
        sink = _RecordingSink()
        p.set_feed_sink(sink)
        p.play()
        p.pause()
        p.play()
        p.pause()
        assert _wait_for(
            lambda: sink.events == ["begin", "begin", "end", "end"] or sink.events.count("end") == 2
        )
        assert sink.events.count("begin") == 2
        p.close()


class TestRadioControlFeedCheckbox:
    def _make(self, qtbot: QtBot) -> Any:
        from ui.radio_control_widget import RadioControlWidget

        w = RadioControlWidget()
        qtbot.addWidget(w)
        return w

    def test_default_off_and_no_sink(self, qtbot: QtBot) -> None:
        w = self._make(qtbot)
        assert not w._feed_cb.isChecked()
        assert w._feed_cb.text() == "Feed to tabs"
        assert w._make_feed_sink() is None

    def test_checked_with_a_sound_card_gives_a_sink_excluding_the_recorder(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        w = self._make(qtbot)
        monkeypatch.setattr(w, "_load_sound_card_input_device", lambda: 3)
        w._feed_cb.setChecked(True)
        sink = w._make_feed_sink()
        assert isinstance(sink, DeviceFeedSink)
        assert sink._device == 3
        assert sink._exclude_owner == w._AUDIO_OWNER

    def test_checked_without_a_sound_card_gives_no_sink(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        w = self._make(qtbot)
        monkeypatch.setattr(w, "_load_sound_card_input_device", lambda: None)
        w._feed_cb.setChecked(True)
        assert w._make_feed_sink() is None


class TestPlayerToSubscribers:
    def test_played_audio_reaches_a_subscriber_and_live_input_is_gated(
        self, wav_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from comms.audio_device_manager import AudioDeviceManager, _SharedInputStream

        mgr = AudioDeviceManager()
        monkeypatch.setattr("comms.audio_device_manager.get_audio_device_manager", lambda: mgr)
        received: list[int] = []
        stream = _SharedInputStream(3, lambda: mgr._feed_state(3))
        stream._subscribers["aprs"] = (RATE, lambda chunk: received.append(len(chunk)))
        mgr._inputs[3] = stream

        p = AudioFilePlayer()
        p.load(wav_path)
        p.set_feed_sink(DeviceFeedSink(3, exclude_owner="rec"))
        p.play()
        _pull_last(RATE // 2)
        assert _wait_for(lambda: sum(received) == RATE // 2)

        received.clear()
        stream._on_audio(np.zeros((100, 1), dtype="float32"), 100, None, None)
        assert received == []  # live audio withheld while the file is fed

        p.pause()
        assert _wait_for(lambda: mgr._feed_state(3)[0] is False)
        stream._on_audio(np.zeros((100, 1), dtype="float32"), 100, None, None)
        assert received  # live audio is back
        p.close()
