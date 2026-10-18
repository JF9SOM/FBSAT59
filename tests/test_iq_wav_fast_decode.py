"""Tests for reading .iq.wav files and the HADES FSK "Fast" decode of a recording file."""

from __future__ import annotations

import datetime
import sqlite3
import struct
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

import ui.telemetry_tab as telemetry_tab_mod
from comms.telemetry import iq_wav
from comms.telemetry.hades_fsk_sdr import HadesFileDecoder
from tests.test_hades_fsk import REAL_PAYLOADS, _stream
from ui.telemetry_tab import TelemetryTab

UNNE1B = 98205
RATE = 250_000


def _write_wav(path: Path, iq: np.ndarray, rate: int = RATE, fact: bool = True) -> None:
    """A float32 stereo WAV laid out like sdr.recorder's (a ``fact`` chunk before ``data``)."""
    pairs = np.empty((len(iq), 2), dtype="<f4")
    pairs[:, 0], pairs[:, 1] = iq.real, iq.imag
    data = pairs.tobytes()
    fmt = struct.pack("<HHIIHH", 3, 2, rate, rate * 8, 8, 32)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    if fact:
        chunks += b"fact" + struct.pack("<II", 4, len(iq))
    chunks += b"data" + struct.pack("<I", len(data))
    path.write_bytes(
        b"RIFF" + struct.pack("<I", 4 + len(chunks) + len(data)) + b"WAVE" + chunks + data
    )


def test_header_with_fact_chunk_starts_data_at_56(tmp_path: Path) -> None:
    iq = (np.arange(100) + 1j * np.arange(100)[::-1]).astype(np.complex64)
    path = tmp_path / "a.iq.wav"
    _write_wav(path, iq)
    info = iq_wav.read_wav_info(path)
    assert (info.sample_rate, info.data_offset, info.frames) == (RATE, 56, 100)
    assert info.float_samples
    blocks = list(iq_wav.iter_iq_blocks(path, info, block=30))
    assert [len(b) for b in blocks] == [30, 30, 30, 10]
    assert np.array_equal(np.concatenate(blocks), iq)  # I and Q are not swapped


def test_header_without_fact_chunk_starts_data_at_44(tmp_path: Path) -> None:
    path = tmp_path / "b.iq.wav"
    _write_wav(path, np.ones(10, dtype=np.complex64), fact=False)
    assert iq_wav.read_wav_info(path).data_offset == 44


def test_int16_stereo_is_read_as_scaled_floats(tmp_path: Path) -> None:
    pairs = np.array([[16384, -16384], [32767, 0]], dtype="<i2")
    data = pairs.tobytes()
    fmt = struct.pack("<HHIIHH", 1, 2, RATE, RATE * 4, 4, 16)
    body = b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(data)) + data
    path = tmp_path / "c.wav"
    path.write_bytes(b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body)
    info = iq_wav.read_wav_info(path)
    assert not info.float_samples and info.frames == 2
    out = np.concatenate(list(iq_wav.iter_iq_blocks(path, info)))
    assert np.allclose(out, [0.5 - 0.5j, 32767 / 32768 + 0j])


def test_truncated_file_is_read_as_far_as_it_goes(tmp_path: Path) -> None:
    path = tmp_path / "d.iq.wav"
    _write_wav(path, np.ones(100, dtype=np.complex64))
    path.write_bytes(path.read_bytes()[: 56 + 8 * 40 + 3])  # cut inside the 41st sample
    assert iq_wav.read_wav_info(path).frames == 40


@pytest.mark.parametrize("content", [b"", b"not a wav at all", b"RIFF\x00\x00\x00\x00WAVE"])
def test_bad_files_raise_wav_error(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "bad.wav"
    path.write_bytes(content)
    with pytest.raises(iq_wav.WavError):
        iq_wav.read_wav_info(path)


def test_mono_file_is_refused(tmp_path: Path) -> None:
    fmt = struct.pack("<HHIIHH", 3, 1, RATE, RATE * 4, 4, 32)
    body = b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", 4) + b"\0\0\0\0"
    path = tmp_path / "mono.wav"
    path.write_bytes(b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body)
    with pytest.raises(iq_wav.WavError, match="two"):
        iq_wav.read_wav_info(path)


def _recording(tmp_path: Path, name: str = "98205_UNNE-1B_20261018T012021Z.iq.wav") -> Path:
    path = tmp_path / name
    _write_wav(path, _stream(REAL_PAYLOADS, [3_100.0, -8_900.0, 11_500.0]))
    return path


def test_file_decoder_finds_every_frame_with_its_time(qtbot: QtBot, tmp_path: Path) -> None:
    job = HadesFileDecoder(_recording(tmp_path))
    found: list[tuple[bytes, float]] = []
    job.frame_found.connect(lambda f, t: found.append((f.payload, t)))
    with qtbot.waitSignal(job.decoded, timeout=60_000) as done:
        job.start()
    assert [p for p, _t in found] == REAL_PAYLOADS
    assert [round(t) for _p, t in found] == [2, 32, 62]  # seconds from the file start
    duration, elapsed, frames = done.args
    assert frames == 3 and duration > 90 and elapsed > 0
    job.wait(3000)


def test_file_decoder_reports_a_bad_file(qtbot: QtBot, tmp_path: Path) -> None:
    bad = tmp_path / "x.wav"
    bad.write_bytes(b"junk")
    job = HadesFileDecoder(bad)
    with qtbot.waitSignal(job.failed, timeout=10_000) as failed:
        job.start()
    assert "WAV" in failed.args[0]
    job.wait(3000)


def test_file_decoder_can_be_cancelled(qtbot: QtBot, tmp_path: Path) -> None:
    job = HadesFileDecoder(_recording(tmp_path))
    seen: list[Any] = []
    job.decoded.connect(lambda *a: seen.append(a))
    job.cancel()  # set before start: the first block sees it and stops
    job.start()
    job.wait(10_000)
    assert seen == []


# ---- the Telemetry tab ------------------------------------------------------------------ #


class _FakeRadioControl(QWidget):
    rig_connected = Signal()
    rig_disconnected = Signal()
    rig2_connected = Signal()
    rig2_disconnected = Signal()
    transmitter_changed = Signal(object)

    def current_transmitter(self) -> None:
        return None


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    c.execute(
        "CREATE TABLE satellites (norad_cat_id INTEGER PRIMARY KEY, name TEXT, is_hidden INTEGER)"
    )
    c.execute(
        """CREATE TABLE transmitters (
            uuid TEXT PRIMARY KEY, norad_cat_id INTEGER, description TEXT,
            mode TEXT, baud INTEGER, alive INTEGER
        )"""
    )
    c.execute("INSERT INTO satellites VALUES (?, 'UNNE-1B', 0)", (UNNE1B,))
    c.execute(
        "INSERT INTO transmitters VALUES ('u1', ?, 'Mode U - 200bps FSK TLM', 'FSK', 200, 1)",
        (UNNE1B,),
    )
    return c


def _tab(qtbot: QtBot, conn: sqlite3.Connection) -> TelemetryTab:
    tab = TelemetryTab(conn, _FakeRadioControl())
    qtbot.addWidget(tab)
    tab._combo_mode.setCurrentIndex(tab._combo_mode.findText(telemetry_tab_mod._MODE_HADES))
    return tab


class _RecordingUploader:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, int, Any]] = []

    def submit(self, conn, raw, norad, received_at, force=False, on_result=None) -> bool:  # type: ignore[no-untyped-def]  # noqa: ANN001
        self.calls.append((raw, norad, received_at))
        return True


def _run_fast(qtbot: QtBot, tab: TelemetryTab, path: Path) -> None:
    assert tab._start_fast_decode(path)
    assert tab._fast_job is not None
    assert not tab._btn_start.isEnabled() and tab._btn_stop.isEnabled()
    qtbot.waitUntil(lambda: tab._fast_job is None, timeout=60_000)


def test_speed_combo_is_only_shown_in_hades_mode_and_remembered(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    tab = _tab(qtbot, conn)
    tab.show()
    assert tab._combo_decode_speed.isVisible()
    assert [tab._combo_decode_speed.itemData(i) for i in range(2)] == ["normal", "fast"]
    tab._combo_decode_speed.setCurrentIndex(1)
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'telemetry_decode_speed'")
    assert row.fetchone()["value"] == "fast"
    tab._combo_mode.setCurrentIndex(0)
    assert not tab._combo_decode_speed.isVisible()
    assert not tab._lbl_decode_speed.isVisible()
    again = _tab(qtbot, conn)
    assert again._combo_decode_speed.currentData() == "fast"


def test_fast_decode_shows_logs_and_uploads_with_file_time(
    qtbot: QtBot, conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _RecordingUploader()
    monkeypatch.setattr(telemetry_tab_mod, "get_satnogs_uploader", lambda: rec)
    tab = _tab(qtbot, conn)
    _run_fast(qtbot, tab, _recording(tmp_path))
    assert tab._table.rowCount() == 3
    assert [r[0] for r in rec.calls] == REAL_PAYLOADS
    start = datetime.datetime(2026, 10, 18, 1, 20, 21, tzinfo=datetime.UTC)
    offsets = [(r[2] - start).total_seconds() for r in rec.calls]
    assert [round(o) for o in offsets] == [2, 32, 62]  # the file's start time + burst position
    assert all(abs(o - round(o)) < 0.5 for o in offsets)
    assert "3 frame(s)" in tab._lbl_status.text()
    assert tab._btn_start.isEnabled() and tab._btn_decode_iq.isEnabled()
    assert conn.execute("SELECT COUNT(*) FROM telemetry_log WHERE kind='hades'").fetchone()[0] == 3


def test_fast_decode_twice_does_not_log_or_send_a_frame_again(
    qtbot: QtBot, conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _RecordingUploader()
    monkeypatch.setattr(telemetry_tab_mod, "get_satnogs_uploader", lambda: rec)
    tab = _tab(qtbot, conn)
    path = _recording(tmp_path)
    _run_fast(qtbot, tab, path)
    _run_fast(qtbot, tab, path)
    assert conn.execute("SELECT COUNT(*) FROM telemetry_log WHERE kind='hades'").fetchone()[0] == 3


def test_fast_decode_without_a_time_in_the_name_is_not_uploaded(
    qtbot: QtBot, conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _RecordingUploader()
    monkeypatch.setattr(telemetry_tab_mod, "get_satnogs_uploader", lambda: rec)
    tab = _tab(qtbot, conn)
    _run_fast(qtbot, tab, _recording(tmp_path, "my recording.wav"))
    assert tab._table.rowCount() == 3  # still shown and logged
    assert rec.calls == []
    assert "not sent" in tab._lbl_status.text()
    reliable = conn.execute("SELECT time_reliable FROM telemetry_log").fetchall()
    assert [r[0] for r in reliable] == [0, 0, 0]


def test_fast_decode_needs_a_satellite_and_a_good_file(
    qtbot: QtBot, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    conn.execute("DELETE FROM transmitters")
    tab = _tab(qtbot, conn)
    assert not tab._start_fast_decode(_recording(tmp_path))
    assert tab._fast_job is None and "satellite" in tab._lbl_status.text().lower()
    conn2 = sqlite3.connect(":memory:")  # a fresh DB with the satellite, then a bad file
    conn2.row_factory = sqlite3.Row
    for line in conn.iterdump():
        conn2.execute(line)
    conn2.execute("INSERT INTO transmitters VALUES ('u2', ?, 'x', 'FSK', 200, 1)", (UNNE1B,))
    tab2 = _tab(qtbot, conn2)
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"junk")
    assert tab2._start_fast_decode(bad)
    qtbot.waitUntil(lambda: tab2._fast_job is None, timeout=10_000)
    assert "Could not decode" in tab2._lbl_status.text()
    assert tab2._btn_start.isEnabled()


def test_stop_cancels_a_fast_decode(qtbot: QtBot, conn: sqlite3.Connection, tmp_path: Path) -> None:
    tab = _tab(qtbot, conn)
    assert tab._start_fast_decode(_recording(tmp_path))
    tab._on_stop()
    assert tab._fast_job is None
    assert tab._btn_start.isEnabled() and not tab._btn_stop.isEnabled()
    assert tab._btn_decode_iq.isEnabled()


def test_decode_button_in_fast_mode_asks_for_a_file(
    qtbot: QtBot, conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tab = _tab(qtbot, conn)
    tab._combo_decode_speed.setCurrentIndex(1)
    chosen = _recording(tmp_path)
    monkeypatch.setattr(
        telemetry_tab_mod.QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *a, **k: (str(chosen), "")),
    )
    tab._on_decode_iq()
    assert tab._fast_job is not None
    qtbot.waitUntil(lambda: tab._fast_job is None, timeout=60_000)
    assert tab._table.rowCount() == 3
