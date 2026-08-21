"""
Unit tests for rig/controller.py.

All tests pass even when Hamlib is not installed (CI).
No network connection required (httpx is mocked).
"""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from rig.controller import (
    _FT991_MODE_MAP,
    _FTX1_MODE_CODES,
    _SATNOGS_TO_RIGCTLD_MODE,
    HAMLIB_AVAILABLE,
    FrequencyState,
    HamlibDirectController,
    HamlibNetController,
    HamlibRotatorController,
    HamlibVersionChecker,
    RigControlError,
    RigInfo,
    RigState,
    RotatorState,
    SdrRigAdapter,
    VersionInfo,
    _build_mode_map,
    _check_rig_ok,
    _MockRig,
    _open_rig_with_retry,
    normalize_civ_addr,
)

# ---------------------------------------------------------------------------
# Mode map
# ---------------------------------------------------------------------------


class TestModeMap:
    def test_contains_fm(self) -> None:
        m = _build_mode_map()
        assert "FM" in m

    def test_contains_ssb(self) -> None:
        m = _build_mode_map()
        assert "SSB" in m

    def test_all_values_are_int(self) -> None:
        for v in _build_mode_map().values():
            assert isinstance(v, int)

    def test_known_modes_present(self) -> None:
        m = _build_mode_map()
        for mode in ("FM", "SSB", "USB", "LSB", "CW", "CW-R", "DIGITALVOICE", "BPSK", "AFSK", "AM"):
            assert mode in m


class TestFt4DataModeMapping:
    """FT4 calling-frequency transponders use mode="USB-D"/"LSB-D" (data-mode
    equivalents of USB/LSB, e.g. DATA-USB on Yaesu rigs). Every mode
    lookup table that a transponder mode string can pass through must
    recognize them, or the rig silently falls back to FM (community
    transmitters RS-44/JO-97/MO-122 FT4 calling frequencies)."""

    def test_mode_map_has_data_modes(self) -> None:
        m = _build_mode_map()
        assert "USB-D" in m
        assert "LSB-D" in m
        assert m["USB-D"] != m["LSB-D"]

    def test_ftx1_mode_codes_has_data_modes(self) -> None:
        assert _FTX1_MODE_CODES["USB-D"] == "C"  # DATA-USB
        assert _FTX1_MODE_CODES["LSB-D"] == "8"  # DATA-LSB

    def test_ft991_mode_map_has_data_modes(self) -> None:
        assert _FT991_MODE_MAP["USB-D"] == "C"  # DATA-USB
        assert _FT991_MODE_MAP["LSB-D"] == "8"  # DATA-LSB

    def test_satnogs_to_rigctld_mode_has_data_modes(self) -> None:
        assert _SATNOGS_TO_RIGCTLD_MODE["USB-D"] == "PKTUSB"
        assert _SATNOGS_TO_RIGCTLD_MODE["LSB-D"] == "PKTLSB"


# ---------------------------------------------------------------------------
# CI-V address normalisation
# ---------------------------------------------------------------------------


class TestNormalizeCivAddr:
    """Icom rig CI-V Address menus display e.g. "A2h" (trailing-h hex
    convention); Hamlib's strtol()-based config parser and Python's
    int(x, 16) both expect a leading "0x" instead, so a trailing "h" must
    be stripped before either parser sees it."""

    def test_plain_hex(self) -> None:
        assert normalize_civ_addr("A2") == "0xA2"

    def test_trailing_h(self) -> None:
        assert normalize_civ_addr("A2h") == "0xA2"

    def test_trailing_uppercase_h(self) -> None:
        assert normalize_civ_addr("A2H") == "0xA2"

    def test_already_prefixed(self) -> None:
        assert normalize_civ_addr("0xA2") == "0xA2"

    def test_strips_whitespace(self) -> None:
        assert normalize_civ_addr("  a2  ") == "0xa2"

    def test_empty_stays_empty(self) -> None:
        assert normalize_civ_addr("") == ""

    def test_result_parses_as_hex(self) -> None:
        assert int(normalize_civ_addr("A2h"), 16) == 0xA2
        assert int(normalize_civ_addr("65"), 16) == 0x65


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


class TestDataClasses:
    def test_rig_state_enum(self) -> None:
        assert RigState.DISCONNECTED.value == "disconnected"
        assert RigState.CONNECTED.value == "connected"

    def test_rig_info_fields(self) -> None:
        info = RigInfo(
            model_id=3081,
            model_name="IC-9700",
            port="/dev/ttyUSB0",
            baud_rate=9600,
            state=RigState.CONNECTED,
        )
        assert info.model_id == 3081
        assert info.state == RigState.CONNECTED

    def test_frequency_state_defaults(self) -> None:
        fs = FrequencyState()
        assert fs.freq_hz == 0.0
        assert fs.mode == "FM"
        assert fs.ctcss_tone == 0.0

    def test_rotator_state_defaults(self) -> None:
        rs = RotatorState()
        assert rs.azimuth_deg == 0.0
        assert rs.elevation_deg == 0.0
        assert not rs.is_moving

    def test_version_info_outdated_message(self) -> None:
        vi = VersionInfo(
            installed="4.5.0",
            latest="4.6.0",
            is_outdated=True,
            release_url="https://example.com",
        )
        assert "4.5.0" in vi.warning_message
        assert "4.6.0" in vi.warning_message

    def test_version_info_not_outdated_no_message(self) -> None:
        vi = VersionInfo(installed="4.6.0", latest="4.6.0", is_outdated=False)
        assert vi.warning_message == ""


# ---------------------------------------------------------------------------
# _MockRig
# ---------------------------------------------------------------------------


class TestMockRig:
    def setup_method(self) -> None:
        self.rig = _MockRig(1)

    def test_set_get_freq(self) -> None:
        self.rig.set_freq(0, 145_800_000.0)
        assert self.rig.get_freq(0) == 145_800_000.0

    def test_set_get_mode(self) -> None:
        self.rig.set_mode(0, 2, 3000)  # (vfo, mode, passband)
        mode, pb = self.rig.get_mode(0)
        assert mode == 2
        assert pb == 3000

    def test_set_split_vfo_no_error(self) -> None:
        self.rig.set_split_vfo(0, 1, 0)

    def test_set_split_freq_no_error(self) -> None:
        self.rig.set_split_freq(0, 145_800_000.0)

    def test_func_and_level_no_error(self) -> None:
        self.rig.set_func(0, 0, 1)
        self.rig.set_level(0, 0, 885)

    def test_close_no_error(self) -> None:
        self.rig.close()


# ---------------------------------------------------------------------------
# HamlibDirectController — mock environment
# ---------------------------------------------------------------------------


class TestHamlibDirectController:
    def _make_ctrl(self) -> HamlibDirectController:
        return HamlibDirectController(
            model_id=1,
            port="/dev/null",
            baud_rate=9600,
        )

    def test_initial_state_disconnected(self) -> None:
        ctrl = self._make_ctrl()
        assert ctrl.state == RigState.DISCONNECTED
        assert not ctrl.is_connected

    def test_connect_succeeds_in_mock_mode(self) -> None:
        ctrl = self._make_ctrl()
        # Without Hamlib, falls back to _MockRig so connect() returns True
        if not HAMLIB_AVAILABLE:
            assert ctrl.connect() is True
            assert ctrl.is_connected

    def test_disconnect_from_disconnected_is_safe(self) -> None:
        ctrl = self._make_ctrl()
        ctrl.disconnect()  # should not raise

    def test_set_frequency_when_disconnected_returns_false(self) -> None:
        ctrl = self._make_ctrl()
        assert ctrl.set_frequency(145_800_000.0) is False

    def test_get_frequency_when_disconnected_returns_minus1(self) -> None:
        ctrl = self._make_ctrl()
        assert ctrl.get_frequency() == -1.0

    def test_set_mode_when_disconnected_returns_false(self) -> None:
        ctrl = self._make_ctrl()
        assert ctrl.set_mode("FM") is False

    def test_get_mode_when_disconnected_returns_fm(self) -> None:
        ctrl = self._make_ctrl()
        assert ctrl.get_mode() == "FM"

    def test_get_rig_info_when_disconnected_returns_none(self) -> None:
        ctrl = self._make_ctrl()
        assert ctrl.get_rig_info() is None

    @pytest.mark.skipif(HAMLIB_AVAILABLE, reason="mock-only test")
    def test_full_workflow_in_mock_mode(self) -> None:
        ctrl = self._make_ctrl()
        assert ctrl.connect()
        assert ctrl.is_connected

        assert ctrl.set_frequency(145_800_000.0)
        assert ctrl.get_frequency() == 145_800_000.0

        assert ctrl.set_mode("FM", 15000)
        assert ctrl.get_mode() == "FM"

        assert ctrl.set_ctcss_tone(88.5)
        assert ctrl.set_ctcss_tone(0.0)
        assert ctrl.set_dcs_code(23)
        assert ctrl.set_dcs_code(0)
        assert ctrl.set_vfo("VFOB")

        info = ctrl.get_rig_info()
        assert info is not None
        assert info.state == RigState.CONNECTED

        ctrl.disconnect()
        assert not ctrl.is_connected

    @pytest.mark.skipif(HAMLIB_AVAILABLE, reason="mock-only test")
    def test_connect_twice_is_idempotent(self) -> None:
        ctrl = self._make_ctrl()
        assert ctrl.connect()
        assert ctrl.connect()  # second call also returns True
        ctrl.disconnect()

    def test_mode_to_hamlib_unknown_falls_back_to_fm(self) -> None:
        ctrl = self._make_ctrl()
        fm_val = ctrl._mode_to_hamlib("FM")
        assert ctrl._mode_to_hamlib("UNKNOWN_MODE") == fm_val

    def test_hamlib_to_mode_roundtrip(self) -> None:
        ctrl = self._make_ctrl()
        for mode_str in ("FM", "SSB", "CW"):
            code = ctrl._mode_to_hamlib(mode_str)
            assert ctrl._hamlib_to_mode(code) == mode_str

    # -- satellite duplex (mock mode) --

    @pytest.mark.skipif(HAMLIB_AVAILABLE, reason="mock-only test")
    def test_connect_calls_init_split(self) -> None:
        """connect() calls set_split_vfo on the rig to enable split mode."""
        ctrl = self._make_ctrl()
        assert ctrl.connect()
        assert ctrl.is_connected  # split init failure is non-fatal

    @pytest.mark.skipif(HAMLIB_AVAILABLE, reason="mock-only test")
    def test_last_freqs_none_after_connect(self) -> None:
        """_last_dl_hz and _last_ul_hz are None right after connect()."""
        ctrl = self._make_ctrl()
        ctrl.connect()
        assert ctrl._last_dl_hz is None
        assert ctrl._last_ul_hz is None

    @pytest.mark.skipif(HAMLIB_AVAILABLE, reason="mock-only test")
    def test_last_freqs_reset_on_disconnect(self) -> None:
        """disconnect() resets _last_dl_hz and _last_ul_hz to None."""
        ctrl = self._make_ctrl()
        ctrl.connect()
        ctrl._last_dl_hz = 435_000_000.0
        ctrl._last_ul_hz = 145_000_000.0
        ctrl.disconnect()
        assert ctrl._last_dl_hz is None
        assert ctrl._last_ul_hz is None

    def test_set_vfo_frequencies_disconnected_returns_false(self) -> None:
        """set_vfo_frequencies returns False when not connected."""
        ctrl = self._make_ctrl()
        assert ctrl.set_vfo_frequencies(435_000_000.0, 145_000_000.0) is False

    @pytest.mark.skipif(HAMLIB_AVAILABLE, reason="mock-only test")
    def test_set_vfo_frequencies_sets_dl_and_ul(self) -> None:
        """On first call, sets both DL and UL via set_freq (not set_split_freq).

        set_split_freq is unreliable on generic rigs (e.g. IC-705 — passing
        either the RX vfo or RIG_VFO_CURR overwrites VFOA instead of VFOB,
        confirmed live), so UL now uses set_freq(tx_vfo, ...) directly, same
        as DL.
        """
        ctrl = self._make_ctrl()
        ctrl.connect()
        mock_rig = MagicMock()
        ctrl._rig = mock_rig
        result = ctrl.set_vfo_frequencies(435_000_000.0, 145_000_000.0)
        assert result is True
        assert ctrl._last_dl_hz == 435_000_000.0
        assert ctrl._last_ul_hz == 145_000_000.0
        assert mock_rig.set_freq.call_count == 2
        mock_rig.set_freq.assert_any_call(0, 435_000_000)
        mock_rig.set_freq.assert_any_call(0, 145_000_000)
        mock_rig.set_split_freq.assert_not_called()

    @pytest.mark.skipif(HAMLIB_AVAILABLE, reason="mock-only test")
    def test_set_vfo_frequencies_delta_suppression_below_1hz(self) -> None:
        """Does not send when frequency change is less than 1 Hz."""
        ctrl = self._make_ctrl()
        ctrl.connect()
        ctrl._last_dl_hz = 435_000_000.0
        mock_rig = MagicMock()
        ctrl._rig = mock_rig
        ctrl.set_vfo_frequencies(435_000_000.5, None)
        mock_rig.set_freq.assert_not_called()
        assert ctrl._last_dl_hz == 435_000_000.0  # not updated

    @pytest.mark.skipif(HAMLIB_AVAILABLE, reason="mock-only test")
    def test_set_vfo_frequencies_sends_at_1hz_boundary(self) -> None:
        """Sends when frequency change is exactly 1 Hz."""
        ctrl = self._make_ctrl()
        ctrl.connect()
        ctrl._last_dl_hz = 435_000_000.0
        mock_rig = MagicMock()
        ctrl._rig = mock_rig
        ctrl.set_vfo_frequencies(435_000_001.0, None)
        mock_rig.set_freq.assert_called_once()
        assert ctrl._last_dl_hz == 435_000_001.0

    @pytest.mark.skipif(HAMLIB_AVAILABLE, reason="mock-only test")
    def test_set_vfo_frequencies_first_call_always_sends(self) -> None:
        """When _last_dl_hz is None (just connected), always sends regardless of value.

        Both DL and UL go through set_freq (see test_set_vfo_frequencies_sets_dl_and_ul
        for why set_split_freq is no longer used).
        """
        ctrl = self._make_ctrl()
        ctrl.connect()
        assert ctrl._last_dl_hz is None
        mock_rig = MagicMock()
        ctrl._rig = mock_rig
        ctrl.set_vfo_frequencies(435_000_000.0, 145_000_000.0)
        assert mock_rig.set_freq.call_count == 2
        mock_rig.set_split_freq.assert_not_called()

    def test_send_mode_only_disconnected_does_not_raise(self) -> None:
        """send_mode_only is a no-op when not connected."""
        ctrl = self._make_ctrl()
        ctrl.send_mode_only("FM", "FM")  # must not raise

    @pytest.mark.skipif(HAMLIB_AVAILABLE, reason="mock-only test")
    def test_send_mode_only_calls_set_mode_twice(self) -> None:
        """send_mode_only opens a fresh Rig and calls set_mode for DL and UL."""
        ctrl = self._make_ctrl()
        mock_rig_inst = MagicMock()
        mock_rig_inst.error_status = 0
        mock_hamlib = MagicMock()
        mock_hamlib.Rig.return_value = mock_rig_inst
        mock_hamlib.RIG_MODE_FM = 32
        mock_hamlib.RIG_MODE_USB = 4
        mock_hamlib.RIG_MODE_LSB = 8
        mock_hamlib.RIG_MODE_CW = 2
        mock_hamlib.RIG_MODE_CWR = 128
        mock_hamlib.RIG_MODE_AM = 1
        mock_hamlib.RIG_MODE_PKTFM = 4096
        mock_hamlib.RIG_MODE_PKTUSB = 2048
        mock_hamlib.RIG_VFO_A = 1
        mock_hamlib.RIG_VFO_B = 2
        with (
            patch("rig.controller.HAMLIB_AVAILABLE", True),
            patch.dict("sys.modules", {"Hamlib": mock_hamlib}),
        ):
            ctrl.send_mode_only("USB", "FM")
        assert mock_rig_inst.set_mode.call_count == 2

    @pytest.mark.skipif(HAMLIB_AVAILABLE, reason="mock-only test")
    def test_send_mode_only_correct_mode_constants(self) -> None:
        """set_mode receives the correct Hamlib mode constants for DL and UL.

        Python binding arg order is set_mode(mode, passband, vfo), so mode is args[0].
        """
        ctrl = self._make_ctrl()
        mock_rig_inst = MagicMock()
        mock_rig_inst.error_status = 0
        mock_hamlib = MagicMock()
        mock_hamlib.Rig.return_value = mock_rig_inst
        mock_hamlib.RIG_MODE_FM = 32
        mock_hamlib.RIG_MODE_USB = 4
        mock_hamlib.RIG_MODE_LSB = 8
        mock_hamlib.RIG_MODE_CW = 2
        mock_hamlib.RIG_MODE_CWR = 128
        mock_hamlib.RIG_MODE_AM = 1
        mock_hamlib.RIG_MODE_PKTFM = 4096
        mock_hamlib.RIG_MODE_PKTUSB = 2048
        mock_hamlib.RIG_VFO_A = 1
        mock_hamlib.RIG_VFO_B = 2
        with (
            patch("rig.controller.HAMLIB_AVAILABLE", True),
            patch.dict("sys.modules", {"Hamlib": mock_hamlib}),
        ):
            ctrl.send_mode_only("USB", "FM")
        # mode is args[0] per Python Hamlib binding: set_mode(mode, passband, vfo)
        called_modes = {call.args[0] for call in mock_rig_inst.set_mode.call_args_list}
        assert 4 in called_modes  # RIG_MODE_USB = 4
        assert 32 in called_modes  # RIG_MODE_FM  = 32


# ---------------------------------------------------------------------------
# HamlibDirectController generic (non-satmode, non-FT991) UL write path —
# the set_vfo(VFOA) display-restore step, and why FTX-1F must not receive it
# ---------------------------------------------------------------------------


class TestGenericDirectUlWriteVfoRestore:
    """set_vfo_frequencies()'s generic branch (model not satmode, not
    FT-991) is shared by IC-705 and FTX-1F. The set_vfo(VFOA) restore after
    the UL write was added (commit 6885275, 2026-07-06) specifically for
    Icom CI-V's "CURR stuck on VFO-B" display quirk, confirmed on IC-705 --
    but for FTX-1F, set_vfo(VFOA) sends raw CAT "VS0;", which (confirmed
    live 2026-07-20) resets TX from Sub back to Main, undoing
    _init_split()'s "FT1;" on every single UL update. FTX-1F must be
    excluded from this restore."""

    def _make_connected_ctrl(self, model_id: int) -> HamlibDirectController:
        ctrl = HamlibDirectController(model_id=model_id, port="/dev/null")
        ctrl._rig = MagicMock()
        fake_hamlib = MagicMock()
        fake_hamlib.RIG_VFO_A = 101
        fake_hamlib.RIG_VFO_B = 102
        fake_hamlib.RIG_VFO_MAIN = 103
        fake_hamlib.RIG_VFO_SUB = 104
        ctrl._hamlib = fake_hamlib
        with ctrl._lock:
            ctrl._state = RigState.CONNECTED
        return ctrl

    def test_ic705_still_restores_vfoa_after_ul_write(self) -> None:
        ctrl = self._make_connected_ctrl(model_id=3085)  # IC-705
        assert ctrl.set_vfo_frequencies(145_800_000.0, 435_000_000.0) is True
        ctrl._rig.set_freq.assert_any_call(102, 435_000_000)  # RIG_VFO_B
        ctrl._rig.set_vfo.assert_called_once_with(101)  # RIG_VFO_A

    def test_ftx1_skips_vfoa_restore_after_ul_write(self) -> None:
        ctrl = self._make_connected_ctrl(model_id=1051)  # FTX-1F
        assert ctrl.set_vfo_frequencies(145_800_000.0, 435_000_000.0) is True
        # The UL write itself must still happen -- only the restore is skipped.
        ctrl._rig.set_freq.assert_any_call(102, 435_000_000)  # RIG_VFO_B
        ctrl._rig.set_vfo.assert_not_called()


class TestIc9700ScopeSelectRestore:
    """GitHub Issue #25: IC-9700's front-panel spectrum scope/waterfall
    tracks a separate, persistent CI-V setting (27 12, "Main/Sub scope
    setting") that is entirely independent of VFO-select (07). An earlier
    fix that called Hamlib's set_vfo(MAIN) on every periodic Doppler UL
    write succeeded on every call (confirmed live: 114/114) yet never
    moved the displayed scope, because 07 was simply the wrong command.
    The real fix sends CI-V 27 12 00 once, piggybacked on the existing
    pyserial window _send_sub_mode_civ_pyserial() already opens for the
    Sub DATA-mode-flag fix (GitHub Issue #16) -- since this is a
    persistent rig setting, not a transient display state, one send at
    transponder selection (Stage 1) is enough; no per-cycle repeat is
    needed. Restricted to IC-9700 (_SATMODE_USE_VFO_SUB) only, since this
    command has not been verified on IC-9100/910H/821H."""

    class _FakeSerial:
        """Records every frame written, and ACKs each one immediately
        (this suite only cares about which frames get sent, not their
        reply payloads -- unlike the mode/DATA-flag tests elsewhere,
        which parse the readback)."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.written: list[bytes] = []
            self._last_write = b""

        def write(self, data: bytes) -> None:
            self._last_write = bytes(data)
            self.written.append(self._last_write)

        def flush(self) -> None:
            pass

        def read_until(self, _terminator: bytes = b"\xfd") -> bytes:
            # Every reply is a plausible-looking ACK/readback frame; the
            # generic tail byte (0x06 for mode/data queries) is harmless
            # for frames this suite doesn't inspect the contents of.
            return bytes([0xFE, 0xFE, 0xE0, 0xA2, self._last_write[4], 0x00, 0xFD])

        def close(self) -> None:
            pass

    def _make_ctrl(self, model_id: int) -> HamlibDirectController:
        return HamlibDirectController(model_id=model_id, port="/dev/null", baud_rate=19200)

    def _run_and_capture_frames(self, ctrl: HamlibDirectController) -> list[bytes]:
        fake_serial_module = MagicMock()
        instances: list[TestIc9700ScopeSelectRestore._FakeSerial] = []

        def _make_fake(*args: object, **kwargs: object) -> TestIc9700ScopeSelectRestore._FakeSerial:
            inst = TestIc9700ScopeSelectRestore._FakeSerial(*args, **kwargs)
            instances.append(inst)
            return inst

        fake_serial_module.Serial.side_effect = _make_fake
        with patch.dict("sys.modules", {"serial": fake_serial_module}):
            ctrl._send_sub_mode_civ_pyserial("USB")
        assert len(instances) == 1
        return instances[0].written

    def test_ic9700_sends_scope_select_main_once(self) -> None:
        ctrl = self._make_ctrl(model_id=3081)  # IC-9700
        frames = self._run_and_capture_frames(ctrl)
        scope_frames = [f for f in frames if len(f) >= 6 and f[4] == 0x27 and f[5] == 0x12]
        assert len(scope_frames) == 1
        assert scope_frames[0] == bytes([0xFE, 0xFE, 0xA2, 0xE0, 0x27, 0x12, 0x00, 0xFD])
        # Sent last, after the 07 D0 (Select Main) restore.
        assert frames[-1] == scope_frames[0]

    def test_ic9100_does_not_send_scope_select(self) -> None:
        """IC-9100 is not in _SATMODE_USE_VFO_SUB -- this command has not
        been verified against that rig, so it must not be sent."""
        ctrl = self._make_ctrl(model_id=3068)  # IC-9100
        frames = self._run_and_capture_frames(ctrl)
        scope_frames = [f for f in frames if len(f) >= 6 and f[4] == 0x27 and f[5] == 0x12]
        assert scope_frames == []


class TestPttDopplerFreeze:
    """set_ptt(freeze_doppler=...) decides whether Doppler tracking continues
    through the TX window. Packet bursts (APRS, AX100 Digi) keep the default
    freeze so the carrier cannot jump mid-packet; tone modes (FT4 ~5 s, Q65 up
    to 60 s) pass False, because freezing smears their signal across the
    passband (GitHub Issue #16)."""

    def _make_connected_ctrl(self) -> HamlibDirectController:
        ctrl = HamlibDirectController(model_id=3085, port="/dev/null")  # IC-705
        ctrl._rig = MagicMock()
        fake_hamlib = MagicMock()
        fake_hamlib.RIG_VFO_A = 101
        fake_hamlib.RIG_VFO_B = 102
        ctrl._hamlib = fake_hamlib
        with ctrl._lock:
            ctrl._state = RigState.CONNECTED
        return ctrl

    def test_default_freezes_doppler_during_tx(self) -> None:
        ctrl = self._make_connected_ctrl()
        ctrl.set_ptt(True)
        assert ctrl._ptt_active is True
        assert ctrl._doppler_frozen is True
        ctrl._rig.set_freq.reset_mock()
        # Frozen: the write is swallowed.
        assert ctrl.set_vfo_frequencies(145_800_000.0, 435_000_000.0) is True
        ctrl._rig.set_freq.assert_not_called()

    def test_tone_mode_keeps_tracking_during_tx(self) -> None:
        ctrl = self._make_connected_ctrl()
        ctrl.set_ptt(True, freeze_doppler=False)
        assert ctrl._ptt_active is True
        assert ctrl._doppler_frozen is False
        ctrl._rig.set_freq.reset_mock()
        assert ctrl.set_vfo_frequencies(145_800_000.0, 435_000_000.0) is True
        ctrl._rig.set_freq.assert_any_call(101, 145_800_000)  # DL still tracked
        ctrl._rig.set_freq.assert_any_call(102, 435_000_000)  # UL still tracked

    def test_unkeying_clears_both_flags(self) -> None:
        ctrl = self._make_connected_ctrl()
        ctrl.set_ptt(True, freeze_doppler=False)
        ctrl.set_ptt(False)
        assert ctrl._ptt_active is False
        assert ctrl._doppler_frozen is False

    def test_pending_frequencies_recorded_even_while_frozen(self) -> None:
        """The values handed over while frozen must still be remembered, so
        _flush_pending_frequencies() has something current to replay."""
        ctrl = self._make_connected_ctrl()
        ctrl.set_ptt(True)  # frozen
        ctrl.set_vfo_frequencies(145_800_123.0, 435_000_456.0)
        assert ctrl._pending_dl_hz == 145_800_123.0
        assert ctrl._pending_ul_hz == 435_000_456.0


class TestPreTxUplinkFlush:
    """Satmode throttles UL writes (20 Hz for non-FM) to spare IC-9100's
    display, which leaves Sub up to 20 Hz stale at any instant. Harmless
    while receiving, but not at the moment a tone mode keys up: the first
    in-TX correction is a whole Doppler cycle away, so the transmission
    would start off-frequency and then step. set_ptt(freeze_doppler=False)
    flushes the newest computed pair first (GitHub Issue #16)."""

    def _make_satmode_ctrl(self) -> HamlibDirectController:
        ctrl = HamlibDirectController(model_id=3081, port="/dev/null")  # IC-9700
        ctrl._rig = MagicMock()
        fake_hamlib = MagicMock()
        fake_hamlib.RIG_VFO_MAIN = 4194304
        fake_hamlib.RIG_VFO_SUB = 8388608
        fake_hamlib.RIG_VFO_TX = 16777216
        fake_hamlib.RIG_PTT_ON = 1
        fake_hamlib.RIG_PTT_OFF = 0
        ctrl._hamlib = fake_hamlib
        ctrl._rig.error_status = 0
        ctrl._current_dl_mode = "USB-D"  # non-FM -> 20 Hz UL throttle
        with ctrl._lock:
            ctrl._state = RigState.CONNECTED
        return ctrl

    def test_stale_uplink_is_flushed_before_keying(self) -> None:
        ctrl = self._make_satmode_ctrl()
        # First cycle establishes both VFOs (cross-band satmode).
        ctrl.set_vfo_frequencies(435_612_000.0, 145_993_000.0)
        ctrl._rig.set_freq.reset_mock()
        # Next cycles drift the UL by less than the 20 Hz throttle, so the
        # rig is never told -- Sub is now stale by 8 Hz.
        ctrl.set_vfo_frequencies(435_611_950.0, 145_992_992.0)
        assert ctrl._rig.set_freq.call_count == 1  # DL only
        ctrl._rig.set_freq.reset_mock()

        ctrl.set_ptt(True, freeze_doppler=False)

        # Keying flushed the pending UL so the carrier comes up on frequency.
        ctrl._rig.set_freq.assert_any_call(8388608, 145_992_992)  # RIG_VFO_SUB
        ctrl._rig.set_ptt.assert_called_once()

    def test_no_flush_when_freezing(self) -> None:
        """Packet modes keep the default freeze and must not be given a
        surprise frequency write on the way into TX."""
        ctrl = self._make_satmode_ctrl()
        ctrl.set_vfo_frequencies(435_612_000.0, 145_993_000.0)
        ctrl.set_vfo_frequencies(435_611_950.0, 145_992_992.0)
        ctrl._rig.set_freq.reset_mock()

        ctrl.set_ptt(True)

        ctrl._rig.set_freq.assert_not_called()

    def test_uplink_tracked_at_1hz_while_transmitting(self) -> None:
        """Once keyed with freeze_doppler=False, sub-20 Hz drift that would
        normally be throttled away must reach the rig."""
        ctrl = self._make_satmode_ctrl()
        ctrl.set_vfo_frequencies(435_612_000.0, 145_993_000.0)
        ctrl.set_ptt(True, freeze_doppler=False)
        ctrl._rig.set_freq.reset_mock()

        ctrl.set_vfo_frequencies(435_611_990.0, 145_992_997.0)  # UL moved 3 Hz

        ctrl._rig.set_freq.assert_any_call(8388608, 145_992_997)  # RIG_VFO_SUB


# ---------------------------------------------------------------------------
# HamlibDirectController satmode (IC-9100/9700) — Hamlib return-code checks
# ---------------------------------------------------------------------------


class TestCheckRigOk:
    """_check_rig_ok() reads rig.error_status, NOT the Hamlib call's own
    return value -- Hamlib's Python binding returns None from Rig methods
    regardless of outcome (confirmed empirically, both on Linux and
    reported live on Windows), so the return value can never be used."""

    def test_passes_when_error_status_is_ok(self) -> None:
        rig = MagicMock()
        rig.error_status = 0
        _check_rig_ok(rig, "some step")  # must not raise

    def test_raises_with_step_name_and_code_on_failure(self) -> None:
        rig = MagicMock()
        rig.error_status = -6  # RIG_EIO
        with pytest.raises(RigControlError, match="some step") as exc_info:
            _check_rig_ok(rig, "some step")
        assert "-6" in str(exc_info.value)

    def test_ignores_the_calls_own_return_value(self) -> None:
        """A call returning None (the real, always-happens case) must not
        be mistaken for failure as long as error_status is 0."""
        rig = MagicMock()
        rig.open.return_value = None
        rig.error_status = 0
        rig.open()
        _check_rig_ok(rig, "open()")  # must not raise despite None return


class TestOpenRigWithRetry:
    """_open_rig_with_retry() works around a Windows-specific quirk where
    Hamlib's own rig.open() (for Icom rigs, which runs an internal CI-V
    echo-status probe as part of opening) can time out on the very first
    attempt even with a verified-correct port/baud/CI-V address, with no
    retry inside Hamlib itself for this specific failure. See its
    docstring for the full account (confirmed live 2026-07-20, Windows 11
    + IC-9100)."""

    def test_succeeds_immediately_when_error_status_is_ok(self) -> None:
        rig = MagicMock()
        rig.error_status = 0
        with patch("rig.controller.time.sleep"):
            _open_rig_with_retry(rig, "some step")
        rig.open.assert_called_once()
        rig.close.assert_not_called()

    def test_succeeds_on_a_later_attempt(self) -> None:
        """A transient failure (e.g. the Windows COM-port timing quirk)
        followed by success on retry must not raise."""
        rig = MagicMock()
        statuses = iter([-5, -5, 0])  # fails twice, then succeeds
        type(rig).error_status = property(lambda self: next(statuses))
        with patch("rig.controller.time.sleep"):
            _open_rig_with_retry(rig, "some step", attempts=3, retry_delay=0.01)
        assert rig.open.call_count == 3
        assert rig.close.call_count == 2  # once between each failed attempt

    def test_raises_after_exhausting_all_attempts(self) -> None:
        rig = MagicMock()
        rig.error_status = -5
        with (
            patch("rig.controller.time.sleep"),
            pytest.raises(RigControlError, match="some step") as exc_info,
        ):
            _open_rig_with_retry(rig, "some step", attempts=3, retry_delay=0.01)
        assert rig.open.call_count == 3
        assert "-5" in str(exc_info.value)


class TestSatmodeHamlibReturnCodeChecks:
    """_apply_mode_and_ctcss_hamlib() (IC-9100/9700 Direct-mode cross-band
    path) must surface a real Hamlib failure instead of reporting success.

    IMPORTANT (discovered empirically against the bundled 4.7.1 build, both
    on Linux and reported live on Windows): Rig methods (open/close/
    set_freq/set_mode/set_func/set_split_vfo) all return None regardless of
    outcome -- the real per-call result lives in `rig.error_status`
    (RIG_OK=0, negative RIG_E* on failure) and must be read from there
    instead. Every mock below therefore sets each method's return_value to
    None (matching real Hamlib) and drives the check purely via
    `error_status`, so this test suite cannot pass by accident if the
    return-value-based mistake is ever reintroduced. See _check_rig_ok()."""

    def _make_ctrl(self) -> HamlibDirectController:
        # model_id=3068 -> IC-9100, a satmode rig not in _SATMODE_USE_VFO_SUB
        # (so the cross-band UL preset uses RIG_VFO_TX).
        ctrl = HamlibDirectController(model_id=3068, port="/dev/null", baud_rate=19200)
        # Cross-band transponder (different DL/UL bands) so
        # _apply_mode_and_ctcss_hamlib takes the SAT-mode sequence branch
        # rather than the same-band VFO-A/B fallback.
        ctrl._transponder_dl_hz = 435612000.0  # UHF
        ctrl._transponder_ul_hz = 145993000.0  # VHF
        return ctrl

    @staticmethod
    def _mock_hamlib(mock_rig_inst: MagicMock, error_status: int = 0) -> MagicMock:
        """Build a mock Hamlib module. Every Rig method returns None (real
        Hamlib behaviour); `error_status` (constant here, since a static
        mock cannot model it changing per-call) drives _check_rig_ok()."""
        mock_hamlib = MagicMock()
        mock_hamlib.Rig.return_value = mock_rig_inst
        mock_hamlib.RIG_MODE_FM = 32
        mock_hamlib.RIG_MODE_USB = 4
        mock_hamlib.RIG_MODE_LSB = 8
        mock_hamlib.RIG_MODE_CW = 2
        mock_hamlib.RIG_MODE_CWR = 128
        mock_hamlib.RIG_MODE_AM = 1
        mock_hamlib.RIG_MODE_PKTUSB = 2048
        mock_hamlib.RIG_MODE_PKTLSB = 1024
        mock_hamlib.RIG_VFO_MAIN = 4194304
        mock_hamlib.RIG_VFO_SUB = 8388608
        mock_hamlib.RIG_VFO_TX = 16777216
        mock_hamlib.RIG_FUNC_SATMODE = 1
        mock_hamlib.RIG_FUNC_TONE = 2
        for name in (
            "open",
            "close",
            "set_func",
            "set_freq",
            "set_mode",
            "set_ctcss_tone",
            "set_vfo",
        ):
            getattr(mock_rig_inst, name).return_value = None
        # get_mode() is a real (mode, width) getter, unlike the setters above
        # (see class docstring) — used by the DIAG mode read-back added for
        # GitHub Issue #16.  The exact value doesn't matter to these tests
        # (only that it unpacks like the real Python binding does).
        mock_rig_inst.get_mode.return_value = (mock_hamlib.RIG_MODE_USB, 0)
        mock_rig_inst.error_status = error_status
        return mock_hamlib

    @staticmethod
    def _mock_serial_module() -> MagicMock:
        """Build a fake `serial` module for _send_sub_mode_civ_pyserial()
        (GitHub Issue #16 — the Sub DATA-mode flag is now written via raw
        CI-V over pyserial instead of Hamlib's set_mode(), since Hamlib's
        own legacy data-mode command does not stick on Sub for this rig
        family). Needed so these tests don't depend on pyserial actually
        being importable / a real serial port being present -- see this
        class's docstring for why every Hamlib call is mocked the same way.

        Replies just need to be shaped plausibly enough for
        _send_sub_mode_civ_pyserial()'s own parsing (reply[4] etc.) to not
        choke; the exact mode/filter values read back don't matter to
        these tests (they only check _check_rig_ok()'s Hamlib-side
        error-status handling, which this pyserial step is not part of).
        """

        class _FakeSerial:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self._last_write = b""

            def write(self, data: bytes) -> None:
                self._last_write = bytes(data)

            def flush(self) -> None:
                pass

            def read_until(self, _terminator: bytes = b"\xfd") -> bytes:
                w = self._last_write
                if len(w) >= 5 and w[4] == 0x04:
                    return bytes([0xFE, 0xFE, 0xE0, 0xA2, 0x04, 0x00, 0x01, 0xFD])
                if len(w) == 7 and w[4] == 0x1A and w[5] == 0x06:
                    return bytes([0xFE, 0xFE, 0xE0, 0xA2, 0x1A, 0x06, 0x01, 0xFD])
                return bytes([0xFE, 0xFE, 0xE0, 0xA2, 0xFB, 0xFD])

            def close(self) -> None:
                pass

        mock_serial = MagicMock()
        mock_serial.Serial.side_effect = _FakeSerial
        return mock_serial

    def test_happy_path_returns_true_and_clears_last_error(self) -> None:
        """error_status == 0 (RIG_OK) throughout -> success, despite every
        Hamlib call itself returning None."""
        ctrl = self._make_ctrl()
        mock_rig_inst = MagicMock()
        mock_hamlib = self._mock_hamlib(mock_rig_inst, error_status=0)
        with (
            patch("rig.controller.HAMLIB_AVAILABLE", True),
            patch.dict(
                "sys.modules", {"Hamlib": mock_hamlib, "serial": self._mock_serial_module()}
            ),
            patch("rig.controller.time.sleep"),
        ):
            ok = ctrl._apply_mode_and_ctcss_hamlib("USB", "LSB", 0.0)
        assert ok is True
        assert ctrl._last_hamlib_error is None

    def test_error_status_failure_is_reported_not_swallowed(self) -> None:
        """A non-zero rig.error_status (e.g. a Windows COM-port timing
        glitch, or a rejected CI-V command) must make
        _apply_mode_and_ctcss_hamlib() return False with a specific reason,
        not silently report success like before this change (real symptom
        reported by a Windows 11 IC-9100 user: Connect succeeds, but mode
        is never actually set on the rig)."""
        ctrl = self._make_ctrl()
        mock_rig_inst = MagicMock()
        mock_hamlib = self._mock_hamlib(mock_rig_inst, error_status=-5)  # RIG_ETIMEOUT
        with (
            patch("rig.controller.HAMLIB_AVAILABLE", True),
            patch.dict(
                "sys.modules", {"Hamlib": mock_hamlib, "serial": self._mock_serial_module()}
            ),
            patch("rig.controller.time.sleep"),
        ):
            ok = ctrl._apply_mode_and_ctcss_hamlib("USB", "LSB", 0.0)
        assert ok is False
        assert ctrl._last_hamlib_error is not None
        assert "-5" in ctrl._last_hamlib_error

    def test_apply_transponder_state_raises_with_specific_reason(self) -> None:
        """apply_transponder_state() must propagate the specific failure
        reason (not a generic "apply failed") so it reaches the status bar
        via main_window.py's existing RigControlError handling."""
        ctrl = self._make_ctrl()
        mock_rig_inst = MagicMock()
        mock_hamlib = self._mock_hamlib(mock_rig_inst, error_status=-9)  # RIG_ERJCTED
        with (
            patch("rig.controller.HAMLIB_AVAILABLE", True),
            patch.dict(
                "sys.modules", {"Hamlib": mock_hamlib, "serial": self._mock_serial_module()}
            ),
            patch("rig.controller.time.sleep"),
            pytest.raises(RigControlError, match="-9"),
        ):
            ctrl.apply_transponder_state("USB", "LSB", 0.0)


class TestApplyModeCtcssLive:
    """apply_mode_ctcss_live() (GitHub Issues #21/#22) must change mode/CTCSS
    on an already-connected cross-band satmode rig without touching
    connection state at all -- the CW/DATA toggle buttons rely on this to
    avoid a disconnect+reconnect cycle on every mode change mid-pass."""

    def _make_connected_ctrl(
        self, mock_rig_inst: MagicMock, *, satmode_active: bool = True
    ) -> HamlibDirectController:
        """mock_rig_inst must be the same instance _mock_hamlib() configured
        (mock_hamlib.Rig.return_value) so it starts out as the already-open
        session -- error_status must be readable on it from the very first
        _check_rig_ok() call, before _resend_mode_ctcss_via_rig() even
        reopens a session of its own."""
        ctrl = HamlibDirectController(model_id=3068, port="/dev/null", baud_rate=19200)
        ctrl._state = RigState.CONNECTED
        ctrl._satmode_active = satmode_active
        ctrl._rig = mock_rig_inst
        return ctrl

    @staticmethod
    def _mock_hamlib(mock_rig_inst: MagicMock, error_status: int = 0) -> MagicMock:
        """Same shape as TestSatmodeHamlibReturnCodeChecks._mock_hamlib():
        every Rig method returns None (real Hamlib behaviour) and
        error_status drives _check_rig_ok()."""
        mock_hamlib = MagicMock()
        mock_hamlib.Rig.return_value = mock_rig_inst
        mock_hamlib.RIG_MODE_FM = 32
        mock_hamlib.RIG_MODE_USB = 4
        mock_hamlib.RIG_MODE_LSB = 8
        mock_hamlib.RIG_MODE_CW = 2
        mock_hamlib.RIG_MODE_CWR = 128
        mock_hamlib.RIG_VFO_MAIN = 4194304
        mock_hamlib.RIG_VFO_SUB = 8388608
        mock_hamlib.RIG_FUNC_TONE = 2
        mock_hamlib.RIG_FUNC_SATMODE = 1
        for name in (
            "open",
            "close",
            "set_freq",
            "set_mode",
            "set_ctcss_tone",
            "set_vfo",
            "set_func",
        ):
            getattr(mock_rig_inst, name).return_value = None
        mock_rig_inst.error_status = error_status
        return mock_hamlib

    @staticmethod
    def _mock_serial_module() -> MagicMock:
        """Same fake as TestSatmodeHamlibReturnCodeChecks._mock_serial_module()
        for _send_sub_mode_civ_pyserial()'s CI-V exchange."""

        class _FakeSerial:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self._last_write = b""

            def write(self, data: bytes) -> None:
                self._last_write = bytes(data)

            def flush(self) -> None:
                pass

            def read_until(self, _terminator: bytes = b"\xfd") -> bytes:
                w = self._last_write
                if len(w) >= 5 and w[4] == 0x04:
                    return bytes([0xFE, 0xFE, 0xE0, 0xA2, 0x04, 0x00, 0x01, 0xFD])
                if len(w) == 7 and w[4] == 0x1A and w[5] == 0x06:
                    return bytes([0xFE, 0xFE, 0xE0, 0xA2, 0x1A, 0x06, 0x01, 0xFD])
                return bytes([0xFE, 0xFE, 0xE0, 0xA2, 0xFB, 0xFD])

            def close(self) -> None:
                pass

        mock_serial = MagicMock()
        mock_serial.Serial.side_effect = _FakeSerial
        return mock_serial

    def test_success_updates_modes_and_stays_connected(self) -> None:
        """A CW toggle (cross-band, satmode already active) must succeed
        without ever leaving RigState.CONNECTED -- no disconnect."""
        mock_rig_inst = MagicMock()
        mock_hamlib = self._mock_hamlib(mock_rig_inst, error_status=0)
        ctrl = self._make_connected_ctrl(mock_rig_inst)
        with (
            patch.dict(
                "sys.modules", {"Hamlib": mock_hamlib, "serial": self._mock_serial_module()}
            ),
            patch("rig.controller.time.sleep"),
        ):
            ctrl.apply_mode_ctcss_live("CW", "CW", 0.0)
        assert ctrl.is_connected
        assert ctrl._current_dl_mode == "CW"
        assert ctrl._current_ul_mode == "CW"
        assert ctrl._last_hamlib_error is None

    def test_raises_when_not_connected(self) -> None:
        ctrl = HamlibDirectController(model_id=3068, port="/dev/null", baud_rate=19200)
        with pytest.raises(RigControlError, match="not connected"):
            ctrl.apply_mode_ctcss_live("CW", "CW", 0.0)

    def test_raises_when_not_cross_band(self) -> None:
        """Same-band duplex (satmode not active) is out of scope for this
        method -- the caller must fall back to the existing
        disconnect+reconnect path for that rare case."""
        ctrl = self._make_connected_ctrl(MagicMock(), satmode_active=False)
        with pytest.raises(RigControlError, match="cross-band"):
            ctrl.apply_mode_ctcss_live("CW", "CW", 0.0)

    def test_ci_v_failure_is_raised_not_swallowed(self) -> None:
        """A rejected CI-V command must surface as RigControlError to the
        caller (main_window._apply_mode_toggle_to_rig(), which shows it on
        the status bar) instead of being silently logged only, unlike the
        Stage-2-resend-on-connect caller which intentionally swallows it."""
        mock_rig_inst = MagicMock()
        mock_hamlib = self._mock_hamlib(mock_rig_inst, error_status=-5)
        ctrl = self._make_connected_ctrl(mock_rig_inst)
        with (
            patch.dict(
                "sys.modules", {"Hamlib": mock_hamlib, "serial": self._mock_serial_module()}
            ),
            patch("rig.controller.time.sleep"),
            pytest.raises(RigControlError, match="-5"),
        ):
            ctrl.apply_mode_ctcss_live("CW", "CW", 0.0)


# ---------------------------------------------------------------------------
# HamlibNetController — socket mocked
# ---------------------------------------------------------------------------


class TestHamlibNetController:
    def _make_ctrl(self, ctcss_method: str = "hamlib") -> HamlibNetController:
        return HamlibNetController(host="localhost", port=4532, ctcss_method=ctcss_method)

    def test_initial_state_disconnected(self) -> None:
        ctrl = self._make_ctrl()
        assert ctrl.state == RigState.DISCONNECTED

    def test_connect_fails_when_no_server(self) -> None:
        ctrl = self._make_ctrl()
        # Mock socket to avoid environment dependency
        with patch("rig.controller.socket.socket") as mock_cls:
            mock_sock = MagicMock()
            mock_sock.connect.side_effect = ConnectionRefusedError("connection refused")
            mock_cls.return_value = mock_sock
            result = ctrl.connect()
        assert result is False
        assert ctrl.state == RigState.ERROR

    def test_operations_when_disconnected_are_safe(self) -> None:
        ctrl = self._make_ctrl()
        assert ctrl.set_frequency(145_800_000.0) is False
        assert ctrl.get_frequency() == -1.0
        assert ctrl.set_mode("FM") is False
        assert ctrl.get_mode() == "FM"
        # set_ctcss_tone is exercised separately below: unlike these, it
        # intentionally opens an independent socket when disconnected (see
        # test_set_ctcss_tone_disconnected_*), so it isn't a pure no-op here.
        assert ctrl.set_dcs_code(23) is False
        assert ctrl.set_vfo("VFOA") is False
        assert ctrl.get_rig_info() is None

    def test_set_ctcss_tone_disconnected_uses_independent_socket(self) -> None:
        """set_ctcss_tone() opens its own socket when not yet connected (e.g.
        transponder selected before Connect is pressed), instead of silently
        no-op'ing like it used to when it only used self._cmd(). It also
        selects VFOB first (confirmed live: the IC-705 stores CTCSS tone
        independently per VFO, so writing without switching lands it on
        whatever VFO send_mode_only() left selected — VFOA/downlink), and
        enables the encoder via "U TONE 1" — confirmed live that "C" only
        sets the tone frequency and does NOT enable the encoder on its own."""
        ctrl = self._make_ctrl()
        assert ctrl._sock is None
        sent: list[bytes] = []
        mock_sock = MagicMock(spec=socket.socket)
        mock_sock.recv.return_value = b"RPRT 0\n"
        mock_sock.sendall.side_effect = lambda data: sent.append(data)
        with patch("rig.controller.socket.socket", return_value=mock_sock):
            result = ctrl.set_ctcss_tone(74.4)
        assert result is True
        data = b"".join(sent)
        assert b"C 744\n" in data
        assert b"U TONE 1\n" in data
        idx_vfob = data.index(b"V VFOB\n")
        idx_c = data.index(b"C 744\n")
        idx_tone = data.index(b"U TONE 1\n")
        idx_vfoa = data.index(b"V VFOA\n")
        assert idx_vfob < idx_c < idx_tone < idx_vfoa

    def test_set_ctcss_tone_disconnected_zero_skips_c_disables_tone(self) -> None:
        """tone_hz <= 0 skips "C" (rigctld rejects tone 0 with RPRT -9,
        confirmed live) and only disables the encoder via "U TONE 0"."""
        ctrl = self._make_ctrl()
        sent: list[bytes] = []
        mock_sock = MagicMock(spec=socket.socket)
        mock_sock.recv.return_value = b"RPRT 0\n"
        mock_sock.sendall.side_effect = lambda data: sent.append(data)
        with patch("rig.controller.socket.socket", return_value=mock_sock):
            result = ctrl.set_ctcss_tone(0.0)
        assert result is True
        data = b"".join(sent)
        assert b"C " not in data
        assert b"U TONE 0\n" in data

    def test_set_ctcss_tone_disconnected_connect_failure_returns_false(self) -> None:
        ctrl = self._make_ctrl()
        with patch("rig.controller.socket.socket") as mock_cls:
            mock_sock = MagicMock()
            mock_sock.connect.side_effect = ConnectionRefusedError("connection refused")
            mock_cls.return_value = mock_sock
            result = ctrl.set_ctcss_tone(74.4)
        assert result is False

    def test_disconnect_when_disconnected_is_safe(self) -> None:
        ctrl = self._make_ctrl()
        ctrl.disconnect()

    def _make_connected_ctrl(self, ctcss_method: str = "hamlib") -> HamlibNetController:
        """Returns a connected controller with a mock socket injected."""
        ctrl = self._make_ctrl(ctcss_method=ctcss_method)
        mock_sock = MagicMock(spec=socket.socket)
        mock_sock.recv.return_value = b"RPRT 0\n"
        ctrl._sock = mock_sock
        with ctrl._lock:
            ctrl._state = RigState.CONNECTED
        return ctrl

    def test_set_ctcss_tone_sends_c_command(self) -> None:
        """set_ctcss_tone() sends rigctld's dedicated "C" command, not "L CTCSS_TONE".

        "L CTCSS_TONE {value}" is rejected by rigctld with RPRT -11
        (ENAVAIL) since CTCSS_TONE is not a LEVEL — confirmed live against
        an IC-705, where this previously silently failed to change the tone.
        Also selects VFOB first and restores VFOA after (see the
        disconnected-path test above for why).
        """
        ctrl = self._make_connected_ctrl()
        calls: list[bytes] = []
        ctrl._sock.sendall.side_effect = lambda data: calls.append(data)  # type: ignore[union-attr]
        result = ctrl.set_ctcss_tone(74.4)
        assert result is True
        sent = b"".join(calls)
        assert b"C 744\n" in sent
        assert b"U TONE 1\n" in sent
        assert b"L CTCSS_TONE" not in sent
        idx_vfob = sent.index(b"V VFOB\n")
        idx_c = sent.index(b"C 744\n")
        idx_tone = sent.index(b"U TONE 1\n")
        idx_vfoa = sent.index(b"V VFOA\n")
        assert idx_vfob < idx_c < idx_tone < idx_vfoa

    def test_set_frequency_sends_command(self) -> None:
        ctrl = self._make_connected_ctrl()
        result = ctrl.set_frequency(145_800_000.0)
        assert result is True
        ctrl._sock.sendall.assert_called()  # type: ignore[union-attr]

    def test_get_frequency_parses_response(self) -> None:
        ctrl = self._make_connected_ctrl()
        ctrl._sock.recv.return_value = b"145800000\nRPRT 0\n"  # type: ignore[union-attr]
        freq = ctrl.get_frequency()
        assert freq == 145_800_000.0

    def test_get_frequency_returns_minus1_on_bad_response(self) -> None:
        ctrl = self._make_connected_ctrl()
        ctrl._sock.recv.return_value = b"RPRT -1\n"  # type: ignore[union-attr]
        freq = ctrl.get_frequency()
        assert freq == -1.0

    def test_set_mode_sends_command(self) -> None:
        ctrl = self._make_connected_ctrl()
        result = ctrl.set_mode("FM", 15000)
        assert result is True
        ctrl._sock.sendall.assert_called()  # type: ignore[union-attr]

    def test_get_mode_parses_fm(self) -> None:
        ctrl = self._make_connected_ctrl()
        ctrl._sock.recv.return_value = b"FM\nRPRT 0\n"  # type: ignore[union-attr]
        assert ctrl.get_mode() == "FM"

    def test_get_mode_parses_usb_as_ssb(self) -> None:
        ctrl = self._make_connected_ctrl()
        ctrl._sock.recv.return_value = b"USB\nRPRT 0\n"  # type: ignore[union-attr]
        assert ctrl.get_mode() == "SSB"

    def test_get_rig_info_returns_host_port(self) -> None:
        """get_rig_info returns host:port as model_name (no socket I/O)."""
        ctrl = self._make_connected_ctrl()
        ctrl._sock.reset_mock()  # type: ignore[union-attr]
        info = ctrl.get_rig_info()
        assert info is not None
        assert "localhost" in info.port
        assert info.model_name == "localhost:4532"
        ctrl._sock.sendall.assert_not_called()  # type: ignore[union-attr]

    def test_disconnect_closes_socket(self) -> None:
        ctrl = self._make_connected_ctrl()
        sock = ctrl._sock
        ctrl.disconnect()
        sock.close.assert_called()  # type: ignore[union-attr]
        assert ctrl.state == RigState.DISCONNECTED

    # -- VFO control --

    def test_is_connected_false_when_sock_none(self) -> None:
        """is_connected is False when _sock is None, even if state is CONNECTED."""
        ctrl = self._make_ctrl()
        with ctrl._lock:
            ctrl._state = RigState.CONNECTED
        assert ctrl._sock is None
        assert ctrl.is_connected is False

    def test_normalize_vfo_known_names(self) -> None:
        """_normalize_vfo returns known VFO strings unchanged."""
        assert HamlibNetController._normalize_vfo("VFOA") == "VFOA"
        assert HamlibNetController._normalize_vfo("VFOB") == "VFOB"
        assert HamlibNetController._normalize_vfo("Main") == "Main"
        assert HamlibNetController._normalize_vfo("Sub") == "Sub"

    def test_vfo_mode_false_sends_v_then_f(self) -> None:
        """When vfo_mode=False, sends V {vfo}\\nF {freq} in that order."""
        ctrl = self._make_connected_ctrl()
        ctrl._vfo_mode = False
        calls: list[bytes] = []
        ctrl._sock.sendall.side_effect = lambda data: calls.append(data)  # type: ignore[union-attr]
        ctrl.set_frequency(144_800_000.0, "VFOA")
        sent = b"".join(calls)
        assert b"V VFOA\n" in sent
        assert b"F 144800000\n" in sent
        assert sent.index(b"V VFOA\n") < sent.index(b"F 144800000\n")

    def test_vfo_mode_true_sends_set_freq(self) -> None:
        """When vfo_mode=True, sends \\\\set_freq {vfo} {freq}."""
        ctrl = self._make_connected_ctrl()
        ctrl._vfo_mode = True
        ctrl.set_frequency(144_800_000.0, "VFOA")
        ctrl._sock.sendall.assert_called_with(b"\\set_freq VFOA 144800000\n")  # type: ignore[union-attr]

    def test_set_frequency_raises_rig_control_error_on_failure(self) -> None:
        """Raises RigControlError when RPRT != 0 is returned while connected."""
        ctrl = self._make_connected_ctrl()
        ctrl._vfo_mode = True
        ctrl._sock.recv.return_value = b"RPRT -1\n"  # type: ignore[union-attr]
        with pytest.raises(RigControlError):
            ctrl.set_frequency(144_800_000.0, "VFOA")

    def test_detect_vfo_mode_true(self) -> None:
        """_detect_vfo_mode() returns True when rigctld responds with "1\\nRPRT 0"."""
        ctrl = self._make_connected_ctrl()
        ctrl._sock.recv.return_value = b"1\nRPRT 0\n"  # type: ignore[union-attr]
        assert ctrl._detect_vfo_mode() is True

    def test_detect_vfo_mode_false(self) -> None:
        """_detect_vfo_mode() returns False when rigctld responds with "0\\nRPRT 0"."""
        ctrl = self._make_connected_ctrl()
        ctrl._sock.recv.return_value = b"0\nRPRT 0\n"  # type: ignore[union-attr]
        assert ctrl._detect_vfo_mode() is False

    def test_detect_vfo_mode_unsupported(self) -> None:
        """_detect_vfo_mode() returns False when rigctld responds with RPRT -1 (unsupported)."""
        ctrl = self._make_connected_ctrl()
        ctrl._sock.recv.return_value = b"RPRT -1\n"  # type: ignore[union-attr]
        assert ctrl._detect_vfo_mode() is False

    def test_detect_vfo_mode_timeout_keeps_connection(self) -> None:
        """Returns False on timeout without disrupting the connection."""
        ctrl = self._make_connected_ctrl()
        ctrl._sock.recv.side_effect = TimeoutError("timed out")  # type: ignore[union-attr]
        result = ctrl._detect_vfo_mode()
        assert result is False
        # socket is not closed
        assert ctrl._sock is not None
        # connection state remains CONNECTED
        assert ctrl.state == RigState.CONNECTED

    def test_set_frequency_disconnected_returns_false(self) -> None:
        """set_frequency returns False when disconnected (no exception)."""
        ctrl = self._make_ctrl()
        assert ctrl.set_frequency(144_800_000.0, "VFOA") is False

    def test_set_frequency_vfob(self) -> None:
        """Sends V VFOB and F commands when VFOB is specified."""
        ctrl = self._make_connected_ctrl()
        ctrl._vfo_mode = False
        calls: list[bytes] = []
        ctrl._sock.sendall.side_effect = lambda data: calls.append(data)  # type: ignore[union-attr]
        ctrl.set_frequency(145_900_000.0, "VFOB")
        sent = b"".join(calls)
        assert b"V VFOB\n" in sent
        assert b"F 145900000\n" in sent

    # -- set_vfo_frequencies --

    def test_set_vfo_frequencies_disconnected_returns_false(self) -> None:
        """Returns False when disconnected (no exception)."""
        ctrl = self._make_ctrl()
        assert ctrl.set_vfo_frequencies(145_000_000.0, 144_000_000.0) is False

    def test_set_vfo_frequencies_first_cycle_sends_F_I_only(self) -> None:
        """On first call (_last=None), sends only F/I and never sends f/i.
        No readback, no leading dial check — sequence: F → I
        """
        ctrl = self._make_connected_ctrl()
        calls: list[bytes] = []
        ctrl._sock.sendall.side_effect = lambda data: calls.append(data)  # type: ignore[union-attr]
        ctrl.set_vfo_frequencies(145_000_000.0, 144_000_000.0)
        sent = b"".join(calls)
        assert b"F 145000000\n" in sent
        assert b"I 144000000\n" in sent
        assert b"f\n" not in sent
        assert b"i\n" not in sent
        assert b"\\set_freq" not in sent
        assert b"\\set_split_freq" not in sent
        assert b"\\set_split_vfo" not in sent

    def test_set_vfo_frequencies_dl_only_no_tx(self) -> None:
        """When ul_hz=None, sends only the RX cycle (F only) and skips the TX cycle.
        On first call (_last=None), no readback or leading check — sends F only.
        """
        ctrl = self._make_connected_ctrl()
        calls: list[bytes] = []
        ctrl._sock.sendall.side_effect = lambda data: calls.append(data)  # type: ignore[union-attr]
        ctrl.set_vfo_frequencies(145_000_000.0, None)
        sent = b"".join(calls)
        assert b"F 145000000\n" in sent
        assert b"f\n" not in sent
        assert b"I " not in sent
        assert b"i\n" not in sent

    def test_set_vfo_frequencies_raises_on_rprt_error(self) -> None:
        """Raises RigControlError when RPRT != 0."""
        ctrl = self._make_connected_ctrl()
        ctrl._sock.recv.return_value = b"RPRT -1\n"  # type: ignore[union-attr]
        with pytest.raises(RigControlError):
            ctrl.set_vfo_frequencies(145_000_000.0, 144_000_000.0)

    def test_set_vfo_frequencies_first_cycle_no_f_i(self) -> None:
        """On first call (_last=None), never sends f/i (no leading check, no readback).
        Avoids CAT delay immediately after S 1 Main. First-cycle sequence: F → I only.
        """
        ctrl = self._make_connected_ctrl()
        calls: list[bytes] = []
        ctrl._sock.sendall.side_effect = lambda data: calls.append(data)  # type: ignore[union-attr]
        ctrl.set_vfo_frequencies(145_000_000.0, 144_000_000.0)
        sent = b"".join(calls)
        assert b"F 145000000\n" in sent
        assert b"I 144000000\n" in sent
        assert b"f\n" not in sent
        assert b"i\n" not in sent

    def test_set_vfo_frequencies_sends_nothing_when_freq_unchanged(self) -> None:
        """Sends nothing (no F, I, f, or i) when frequency is unchanged (diff < 1 Hz)."""
        ctrl = self._make_connected_ctrl()
        ctrl._last_dl_hz = 145_000_000.0
        ctrl._last_ul_hz = 144_000_000.0
        calls: list[bytes] = []
        ctrl._sock.sendall.side_effect = lambda data: calls.append(data)  # type: ignore[union-attr]
        result = ctrl.set_vfo_frequencies(145_000_000.0, 144_000_000.0)
        assert calls == []  # nothing sent
        assert result is True

    def test_set_vfo_frequencies_sends_F_when_freq_changes_by_1hz(self) -> None:
        """Sends F when frequency changes by 1 Hz or more (boundary test)."""
        ctrl = self._make_connected_ctrl()
        ctrl._last_dl_hz = 145_000_000.0
        calls: list[bytes] = []
        ctrl._sock.sendall.side_effect = lambda data: calls.append(data)  # type: ignore[union-attr]
        ctrl.set_vfo_frequencies(145_000_001.0, None)
        sent = b"".join(calls)
        assert b"F 145000001\n" in sent

    def test_set_vfo_frequencies_skips_F_when_change_less_than_1hz(self) -> None:
        """Does not send F when change is 0.9 Hz (boundary test)."""
        ctrl = self._make_connected_ctrl()
        ctrl._last_dl_hz = 145_000_000.9
        calls: list[bytes] = []
        ctrl._sock.sendall.side_effect = lambda data: calls.append(data)  # type: ignore[union-attr]
        ctrl.set_vfo_frequencies(145_000_000.0, None)  # diff = 0.9 Hz < 1.0
        sent = b"".join(calls)
        assert b"F " not in sent

    def test_disconnect_resets_last_frequencies(self) -> None:
        """disconnect() resets _last_dl_hz and _last_ul_hz to None."""
        ctrl = self._make_connected_ctrl()
        ctrl._last_dl_hz = 145_000_000.0
        ctrl._last_ul_hz = 144_000_000.0
        ctrl.disconnect()
        assert ctrl._last_dl_hz is None
        assert ctrl._last_ul_hz is None

    def test_set_vfo_frequencies_sends_F_when_last_is_none(self) -> None:
        """_last_dl_hz=None（connect直後）は値に関わらず必ず F/I を送る。"""
        ctrl = self._make_connected_ctrl()
        assert ctrl._last_dl_hz is None
        assert ctrl._last_ul_hz is None
        calls: list[bytes] = []
        ctrl._sock.sendall.side_effect = lambda data: calls.append(data)  # type: ignore[union-attr]
        ctrl.set_vfo_frequencies(435_000_000.0, 145_000_000.0)
        sent = b"".join(calls)
        assert b"F 435000000\n" in sent
        assert b"I 145000000\n" in sent

    def test_connect_resets_last_frequencies(self) -> None:
        """connect() 後は _last_dl_hz と _last_ul_hz が必ず None にリセットされる。"""
        ctrl = self._make_ctrl()
        with patch("rig.controller.socket.socket") as mock_cls:
            mock_sock = MagicMock()
            mock_sock.recv.return_value = b"RPRT 0\n"
            mock_cls.return_value = mock_sock
            ctrl.connect()
        assert ctrl._last_dl_hz is None
        assert ctrl._last_ul_hz is None

    def test_set_vfo_frequencies_second_cycle_sends_F_only_on_change(self) -> None:
        """2 サイクル目以降は f/i を送らず、変化があるときのみ F を送る。"""
        ctrl = self._make_connected_ctrl()
        ctrl._last_dl_hz = 145_000_000.0  # 2サイクル目を再現
        calls: list[bytes] = []
        ctrl._sock.sendall.side_effect = lambda data: calls.append(data)  # type: ignore[union-attr]
        ctrl.set_vfo_frequencies(145_001_000.0, None)
        sent = b"".join(calls)
        assert b"f\n" not in sent  # f/i は一切送らない
        assert b"F 145001000\n" in sent

    def test_set_vfo_frequencies_skips_tx_when_disconnected_between_rx_and_tx(self) -> None:
        """RX サイクル後に切断した場合 TX サイクルをスキップして True を返す。

        シナリオ: 同一周波数（F 送信なし） → RX/TX 間のガードが切断を検出
        """
        from unittest.mock import PropertyMock

        ctrl = self._make_connected_ctrl()
        ctrl._last_dl_hz = 145_000_000.0  # 変化なし → F 送信なし
        ctrl._last_ul_hz = None
        calls: list[bytes] = []
        ctrl._sock.sendall.side_effect = lambda data: calls.append(data)  # type: ignore[union-attr]

        # is_connected: 初回 True（入り口通過）→ ガード False（TX スキップ）
        with patch.object(
            HamlibNetController, "is_connected", new_callable=PropertyMock
        ) as mock_prop:
            mock_prop.side_effect = [True, False]
            result = ctrl.set_vfo_frequencies(145_000_000.0, 144_000_000.0)

        assert result is True
        assert b"F " not in b"".join(calls)
        assert b"I " not in b"".join(calls)

    def test_connect_sends_split_vfob_for_generic_rig(self) -> None:
        """connect() sends S 1 VFOB (split ON) for generic rigs like IC-705."""
        ctrl = self._make_ctrl()  # default ctcss_method="hamlib"
        with patch("rig.controller.socket.socket") as mock_cls:
            mock_sock = MagicMock()
            mock_sock.recv.return_value = b"RPRT 0\n"
            mock_cls.return_value = mock_sock
            result = ctrl.connect()
        assert result is True
        sent = b"".join(call.args[0] for call in mock_sock.sendall.call_args_list)
        assert b"S 1 VFOB\n" in sent

    def test_connect_sends_split_main_for_ftx1(self) -> None:
        """connect() still sends S 1 Main (split ON) for FTX-1F."""
        ctrl = self._make_ctrl(ctcss_method="ftx1")
        with patch("rig.controller.socket.socket") as mock_cls:
            mock_sock = MagicMock()
            mock_sock.recv.return_value = b"RPRT 0\n"
            mock_cls.return_value = mock_sock
            result = ctrl.connect()
        assert result is True
        sent = b"".join(call.args[0] for call in mock_sock.sendall.call_args_list)
        assert b"S 1 Main\n" in sent

    def test_init_vfo_timeout_disconnects(self) -> None:
        """S 1 Main がタイムアウトすると _cmd() がソケットを閉じて DISCONNECTED になる。

        raw socket 直接アクセスではなく _cmd() 経由にしたことで、
        タイムアウト後の応答データがバッファに残留してコマンド応答がずれる
        バッファ汚染を起こさなくなった。
        """
        ctrl = self._make_connected_ctrl()
        ctrl._sock.recv.side_effect = TimeoutError("timed out")  # type: ignore[union-attr]
        ctrl._init_vfo()  # should not raise
        assert ctrl._sock is None
        assert ctrl.state == RigState.DISCONNECTED

    def test_connect_returns_false_when_S1Main_fails(self) -> None:
        """S 1 Main がタイムアウトした場合 connect() は False を返し ERROR 状態になる。

        以前は _init_vfo() 失敗を無視して True を返していたため、
        接続ボタンが「接続済み」のまま固まる問題があった。
        """
        ctrl = self._make_ctrl()
        with patch("rig.controller.socket.socket") as mock_cls:
            mock_sock = MagicMock()
            # TCP 接続自体は成功、S 1 Main の recv でタイムアウト
            mock_sock.connect.return_value = None
            mock_sock.recv.side_effect = TimeoutError("timed out")
            mock_cls.return_value = mock_sock
            result = ctrl.connect()
        assert result is False
        assert ctrl.state == RigState.ERROR
        assert ctrl._sock is None

    # -- _init_vfo: split ON --

    def test_init_vfo_generic_sends_s1vfob(self) -> None:
        """_init_vfo() sends S 1 VFOB for generic (non-Yaesu) rigs like IC-705.

        IC-705 has no true Main/Sub VFO concept; "S 1 Main" gets misparsed
        by its Hamlib backend and inverts which VFO becomes RX/TX (confirmed
        live). Plain VFOA/VFOB split works correctly instead.
        """
        ctrl = self._make_connected_ctrl()  # default ctcss_method="hamlib"
        calls: list[bytes] = []
        ctrl._sock.sendall.side_effect = lambda data: calls.append(data)  # type: ignore[union-attr]
        ctrl._init_vfo()
        sent = b"".join(calls)
        assert b"S 1 VFOB\n" in sent
        assert b"S 1 Main\n" not in sent

    def test_init_vfo_ftx1_sends_s1main(self) -> None:
        """_init_vfo() still sends S 1 Main for FTX-1F (ctcss_method='ftx1')."""
        ctrl = self._make_connected_ctrl(ctcss_method="ftx1")
        calls: list[bytes] = []
        ctrl._sock.sendall.side_effect = lambda data: calls.append(data)  # type: ignore[union-attr]
        ctrl._init_vfo()
        sent = b"".join(calls)
        assert b"S 1 Main\n" in sent

    def test_init_vfo_yaesu_cat_resets_uplink(self) -> None:
        r"""_init_vfo() sends "\uplink 0" after split init for ftx1/ft991.

        rigctld shares one RIG object across all TCP clients, so a past
        client on the same port (e.g. GPredict, which rig_set_uplink()'s own
        doc comment says this API exists for) may have left rs->uplink set
        to 1 or 2. Hamlib's rig_get_freq() then returns a frozen cached
        value for the ignored VFO indefinitely (not on any timeout) until
        reset -- this caused the Lock dial-feedback feature's live_dl read
        to freeze for arbitrary durations (confirmed 2026-07-20). Reset
        unconditionally on every connect.
        """
        ctrl = self._make_connected_ctrl(ctcss_method="ftx1")
        calls: list[bytes] = []
        ctrl._sock.sendall.side_effect = lambda data: calls.append(data)  # type: ignore[union-attr]
        ctrl._init_vfo()
        sent = b"".join(calls)
        assert b"\\uplink 0\n" in sent

    def test_init_vfo_generic_does_not_reset_uplink(self) -> None:
        r"""Generic (non-Yaesu-CAT) rigs don't get the "\uplink 0" reset.

        Only ftx1/ft991 NET mode ever calls get_frequency()/
        get_split_frequency() via the Lock feature, so the reset is scoped
        to that same condition rather than sent unconditionally.
        """
        ctrl = self._make_connected_ctrl()  # default ctcss_method="hamlib"
        calls: list[bytes] = []
        ctrl._sock.sendall.side_effect = lambda data: calls.append(data)  # type: ignore[union-attr]
        ctrl._init_vfo()
        sent = b"".join(calls)
        assert b"\\uplink 0\n" not in sent

    # -- _send_split_init_independent: same generic-vs-Yaesu split as _init_vfo --

    def test_send_split_init_independent_generic_sends_s1vfob(self) -> None:
        """Generic rigs (e.g. IC-705) also get a trailing V VFOA to force the
        display to refresh — confirmed live that the write lands correctly
        internally but the screen doesn't update without reselecting VFOA."""
        ctrl = self._make_ctrl()  # default ctcss_method="hamlib"
        sent: list[bytes] = []
        mock_sock = MagicMock(spec=socket.socket)
        mock_sock.recv.return_value = b"RPRT 0\n"
        mock_sock.sendall.side_effect = lambda data: sent.append(data)
        with patch("rig.controller.socket.socket", return_value=mock_sock):
            ctrl._send_split_init_independent()
        data = b"".join(sent)
        assert b"S 1 VFOB\n" in data
        assert b"S 1 Main\n" not in data
        assert b"V VFOA\n" in data

    def test_send_split_init_independent_ftx1_sends_s1main(self) -> None:
        """FTX-1F must not get the generic-rig V VFOA display-refresh call —
        it's unnecessary and unvalidated for the Yaesu Main/Sub convention."""
        ctrl = self._make_ctrl(ctcss_method="ftx1")
        sent: list[bytes] = []
        mock_sock = MagicMock(spec=socket.socket)
        mock_sock.recv.return_value = b"RPRT 0\n"
        mock_sock.sendall.side_effect = lambda data: sent.append(data)
        with patch("rig.controller.socket.socket", return_value=mock_sock):
            ctrl._send_split_init_independent()
        data = b"".join(sent)
        assert b"S 1 Main\n" in data
        assert b"V VFOA\n" not in data

    # -- _send_freq_preset_independent: same generic-vs-Yaesu V VFOA scoping --

    def test_send_freq_preset_independent_generic_sends_v_vfoa(self) -> None:
        ctrl = self._make_ctrl()  # default ctcss_method="hamlib"
        ctrl._transponder_dl_hz = 145_920_000.0
        ctrl._transponder_ul_hz = 435_830_000.0
        sent: list[bytes] = []
        mock_sock = MagicMock(spec=socket.socket)
        mock_sock.recv.return_value = b"RPRT 0\n"
        mock_sock.sendall.side_effect = lambda data: sent.append(data)
        with patch("rig.controller.socket.socket", return_value=mock_sock):
            ctrl._send_freq_preset_independent()
        data = b"".join(sent)
        assert b"F 145920000\n" in data
        assert b"I 435830000\n" in data
        assert b"V VFOA\n" in data
        assert data.index(b"I 435830000\n") < data.index(b"V VFOA\n")

    def test_send_freq_preset_independent_ftx1_no_v_vfoa(self) -> None:
        ctrl = self._make_ctrl(ctcss_method="ftx1")
        ctrl._transponder_dl_hz = 145_920_000.0
        ctrl._transponder_ul_hz = 435_830_000.0
        sent: list[bytes] = []
        mock_sock = MagicMock(spec=socket.socket)
        mock_sock.recv.return_value = b"RPRT 0\n"
        mock_sock.sendall.side_effect = lambda data: sent.append(data)
        with patch("rig.controller.socket.socket", return_value=mock_sock):
            ctrl._send_freq_preset_independent()
        data = b"".join(sent)
        assert b"F 145920000\n" in data
        assert b"I 435830000\n" in data
        assert b"V VFOA\n" not in data

    # -- _cmd_raw: query commands (lowercase) must not wait for RPRT --
    #
    # Confirmed live (2026-07-15, FTX-1F): a successful query response (e.g.
    # "f" -> "435612000\n") never includes an RPRT line -- RPRT only appears
    # on the query's *error* path. The old unconditional "wait for RPRT"
    # loop therefore blocked every query until the socket timeout, which
    # was misdiagnosed for years as "get_freq doesn't work on this rig".

    def test_get_frequency_query_response_without_rprt_returns_immediately(self) -> None:
        ctrl = self._make_connected_ctrl()
        ctrl._sock.recv.return_value = b"435612000\n"  # type: ignore[union-attr]
        freq = ctrl.get_frequency()
        assert freq == 435_612_000.0
        # A single recv() call was enough -- the loop did not keep polling
        # waiting for an RPRT line that was never coming.
        ctrl._sock.recv.assert_called_once()  # type: ignore[union-attr]

    def test_get_split_frequency_query_response_without_rprt(self) -> None:
        ctrl = self._make_connected_ctrl()
        ctrl._sock.recv.return_value = b"145993000\n"  # type: ignore[union-attr]
        freq = ctrl.get_split_frequency()
        assert freq == 145_993_000.0
        ctrl._sock.sendall.assert_called_with(b"i\n")  # type: ignore[union-attr]

    def test_cmd_raw_set_command_still_waits_for_rprt_across_fragments(self) -> None:
        """Set commands (uppercase) are unaffected by the query fix -- they
        still read until RPRT appears, even if the response arrives split
        across multiple recv() calls."""
        ctrl = self._make_connected_ctrl()
        ctrl._sock.recv.side_effect = [b"RP", b"RT 0\n"]  # type: ignore[union-attr]
        with ctrl._cmd_lock:
            resp = ctrl._cmd_raw("F 145800000")
        assert resp == "RPRT 0"
        assert ctrl._sock.recv.call_count == 2  # type: ignore[union-attr]

    # -- read_dl_ul_independent: Lock dial feedback --

    def test_read_dl_ul_independent_yaesu_cat_reads_both(self) -> None:
        ctrl = self._make_ctrl(ctcss_method="ftx1")
        mock_sock = MagicMock(spec=socket.socket)
        mock_sock.recv.side_effect = [b"RPRT 0\n", b"435612020\n", b"145993000\n"]
        sent: list[bytes] = []
        mock_sock.sendall.side_effect = lambda data: sent.append(data)
        with patch("rig.controller.socket.socket", return_value=mock_sock):
            result = ctrl.read_dl_ul_independent()
        assert result == (435_612_020.0, 145_993_000.0)
        data = b"".join(sent)
        assert data == b"S 1 Main\nf\ni\n"

    def test_read_dl_ul_independent_none_for_non_yaesu_cat(self) -> None:
        """Only verified against Yaesu-CAT NET-mode rigs (2026-07-15) --
        must not be used for satmode/generic rigs it was never tested on."""
        ctrl = self._make_ctrl(ctcss_method="icom_civ")
        with patch("rig.controller.socket.socket") as mock_cls:
            result = ctrl.read_dl_ul_independent()
        assert result is None
        mock_cls.assert_not_called()

    def test_read_dl_ul_independent_returns_none_on_connect_failure(self) -> None:
        ctrl = self._make_ctrl(ctcss_method="ft991")
        with patch("rig.controller.socket.socket") as mock_cls:
            mock_sock = MagicMock()
            mock_sock.connect.side_effect = ConnectionRefusedError("refused")
            mock_cls.return_value = mock_sock
            result = ctrl.read_dl_ul_independent()
        assert result is None

    # -- set_vfo_frequencies: F/I only, no M --

    def test_set_vfo_frequencies_sends_no_mode_command(self) -> None:
        """set_vfo_frequencies() sends no M command."""
        ctrl = self._make_connected_ctrl()
        calls: list[bytes] = []
        ctrl._sock.sendall.side_effect = lambda data: calls.append(data)  # type: ignore[union-attr]
        ctrl.set_vfo_frequencies(145_000_000.0, 144_000_000.0)
        sent = b"".join(calls)
        assert b"M " not in sent
        assert b"F 145000000\n" in sent
        assert b"I 144000000\n" in sent

    # -- send_mode_only --

    def test_send_mode_only_generic_sends_v_vfob_ul_v_vfoa_dl(self) -> None:
        """Generic rigs (e.g. IC-705): V VFOB → M {ul} 0 → V VFOA → M {dl} 0.

        "Main"/"Sub" naming is Yaesu-specific (see test_send_mode_only_ftx1_*
        below); generic rigs have no such concept and use plain VFOA/VFOB.
        """
        ctrl = self._make_ctrl()  # default ctcss_method="hamlib"
        sent: list[bytes] = []
        mock_sock = MagicMock(spec=socket.socket)
        mock_sock.recv.return_value = b"RPRT 0\n"
        mock_sock.sendall.side_effect = lambda data: sent.append(data)
        with patch("rig.controller.socket.socket", return_value=mock_sock):
            ctrl.send_mode_only("FM", "FM")
        data = b"".join(sent)
        assert b"V VFOB\n" in data
        assert b"M FM 0\n" in data
        assert b"V VFOA\n" in data
        assert b"V Sub\n" not in data
        assert b"V Main\n" not in data
        assert data.index(b"V VFOB\n") < data.index(b"V VFOA\n")

    def test_send_mode_only_generic_invert_usb_dl_lsb_ul(self) -> None:
        """invert=True case: ul=LSB (VFOB/TX) is sent before dl=USB (VFOA/RX)."""
        ctrl = self._make_ctrl()
        sent: list[bytes] = []
        mock_sock = MagicMock(spec=socket.socket)
        mock_sock.recv.return_value = b"RPRT 0\n"
        mock_sock.sendall.side_effect = lambda data: sent.append(data)
        with patch("rig.controller.socket.socket", return_value=mock_sock):
            ctrl.send_mode_only("USB", "LSB")  # dl=USB, ul=LSB (RS-44 style)
        data = b"".join(sent)
        # V VFOB must precede M LSB 0 (uplink/TX)
        assert b"V VFOB\n" in data
        assert b"M LSB 0\n" in data
        idx_vfob = data.index(b"V VFOB\n")
        idx_lsb = data.index(b"M LSB 0\n")
        assert idx_vfob < idx_lsb
        # V VFOA must precede M USB 0 (downlink/RX) and come after V VFOB
        assert b"V VFOA\n" in data
        assert b"M USB 0\n" in data
        idx_vfoa = data.index(b"V VFOA\n")
        idx_usb = data.index(b"M USB 0\n")
        assert idx_vfoa < idx_usb
        assert idx_vfob < idx_vfoa

    def test_send_mode_only_generic_ends_with_split_init(self) -> None:
        """send_mode_only re-sends S 1 VFOB at the end for generic non-satmode rigs.

        V VFOA (used to set DL mode) leaves TX on VFOA. A trailing S 1 VFOB
        restores TX=VFOB (uplink) immediately at transponder selection.
        """
        ctrl = self._make_ctrl()
        sent: list[bytes] = []
        mock_sock = MagicMock(spec=socket.socket)
        mock_sock.recv.return_value = b"RPRT 0\n"
        mock_sock.sendall.side_effect = lambda data: sent.append(data)
        with patch("rig.controller.socket.socket", return_value=mock_sock):
            ctrl.send_mode_only("USB", "USB")
        data = b"".join(sent)
        assert b"S 1 VFOB\n" in data

    def test_send_mode_only_ftx1_sends_v_sub_ul_v_main_dl(self) -> None:
        """FTX-1F (ctcss_method='ftx1') still sends V Sub → M {ul} 0 → V Main → M {dl} 0."""
        ctrl = self._make_ctrl(ctcss_method="ftx1")
        sent: list[bytes] = []
        mock_sock = MagicMock(spec=socket.socket)
        mock_sock.recv.return_value = b"RPRT 0\n"
        mock_sock.sendall.side_effect = lambda data: sent.append(data)
        with patch("rig.controller.socket.socket", return_value=mock_sock):
            ctrl.send_mode_only("FM", "FM")
        data = b"".join(sent)
        assert b"V Sub\n" in data
        assert b"M FM 0\n" in data
        assert b"V Main\n" in data
        assert data.index(b"V Sub\n") < data.index(b"V Main\n")

    def test_send_mode_only_ftx1_ends_with_split_init(self) -> None:
        """FTX-1F still re-sends S 1 Main at the end (rigctld backend quirk)."""
        ctrl = self._make_ctrl(ctcss_method="ftx1")
        sent: list[bytes] = []
        mock_sock = MagicMock(spec=socket.socket)
        mock_sock.recv.return_value = b"RPRT 0\n"
        mock_sock.sendall.side_effect = lambda data: sent.append(data)
        with patch("rig.controller.socket.socket", return_value=mock_sock):
            ctrl.send_mode_only("USB", "USB")
        data = b"".join(sent)
        assert b"S 1 Main\n" in data

    def test_send_mode_only_unknown_mode_falls_back_to_fm(self) -> None:
        """Unmapped modes (e.g. SSTV, SSDV, DOKA) must fall back to FM rather
        than silently sending no CAT command at all — leaving the rig parked
        in whatever mode a previously selected transponder had set is exactly
        the bug that made APRS/SSTV transponders appear stuck in CW after
        testing an FT4/CW-mode transponder beforehand."""
        ctrl = self._make_ctrl()
        sent: list[bytes] = []
        mock_sock = MagicMock(spec=socket.socket)
        mock_sock.recv.return_value = b"RPRT 0\n"
        mock_sock.sendall.side_effect = lambda data: sent.append(data)
        with patch("rig.controller.socket.socket", return_value=mock_sock):
            ctrl.send_mode_only("SSTV", "SSTV")
        data = b"".join(sent)
        assert b"M FM 0\n" in data

    def test_send_mode_only_ft991_unknown_mode_falls_back_to_fm(self) -> None:
        """Same fallback, but for the ctcss_method == "ft991" raw-CAT branch
        (e.g. an FTX-1F configured with ctcss_method="ft991"): AFSK/SSTV must
        still send MD0 4; (FM) rather than skipping the CAT command."""
        ctrl = HamlibNetController(host="localhost", port=4532, ctcss_method="ft991")
        sent: list[bytes] = []
        mock_sock = MagicMock(spec=socket.socket)
        mock_sock.recv.return_value = b"RPRT 0\n"
        mock_sock.sendall.side_effect = lambda data: sent.append(data)
        with patch("rig.controller.socket.socket", return_value=mock_sock):
            ctrl.send_mode_only("AFSK", "AFSK")
        data = b"".join(sent)
        assert b"w MD04;\n" in data

    def test_send_mode_only_ssb_maps_to_usb(self) -> None:
        """SSB は rigctld の USB として送信される。"""
        ctrl = self._make_ctrl()
        sent: list[bytes] = []
        mock_sock = MagicMock(spec=socket.socket)
        mock_sock.recv.return_value = b"RPRT 0\n"
        mock_sock.sendall.side_effect = lambda data: sent.append(data)
        with patch("rig.controller.socket.socket", return_value=mock_sock):
            ctrl.send_mode_only("SSB", "SSB")
        data = b"".join(sent)
        assert b"M USB 0\n" in data

    def test_send_mode_only_silently_ignores_oserror(self) -> None:
        """OSError（接続失敗など）を無視して例外を送出しない。"""
        ctrl = self._make_ctrl()
        with patch("rig.controller.socket.socket") as mock_cls:
            mock_cls.return_value.connect.side_effect = OSError("refused")
            ctrl.send_mode_only("FM", "FM")  # must not raise

    def test_send_mode_only_uses_independent_socket(self) -> None:
        """send_mode_only は main の _sock を使わず独立したソケットを開く。"""
        ctrl = self._make_connected_ctrl()
        original_sock = ctrl._sock
        # Create the new-socket mock before entering the patch block so that
        # socket.socket is still the real class and spec= doesn't fail.
        mock_new_sock = MagicMock(spec=socket.socket)
        mock_new_sock.recv.return_value = b"RPRT 0\n"
        with patch("rig.controller.socket.socket", return_value=mock_new_sock):
            ctrl.send_mode_only("FM", "FM")
        assert ctrl._sock is original_sock  # main socket unchanged


# ---------------------------------------------------------------------------
# HamlibRotatorController
# ---------------------------------------------------------------------------


class TestSouthInitOffset:
    """Test the 180-degree AZ offset computation used in south-init rotator mode."""

    @staticmethod
    def _apply(az: float) -> float:
        return (az + 180) % 360

    def test_north_maps_to_south(self) -> None:
        assert self._apply(0.0) == pytest.approx(180.0)

    def test_south_maps_to_north(self) -> None:
        assert self._apply(180.0) == pytest.approx(0.0)

    def test_east_maps_to_west(self) -> None:
        assert self._apply(90.0) == pytest.approx(270.0)

    def test_near_zero_forward(self) -> None:
        assert self._apply(350.0) == pytest.approx(170.0)

    def test_near_zero_reverse(self) -> None:
        assert self._apply(10.0) == pytest.approx(190.0)

    def test_identity_after_double_offset(self) -> None:
        az = 137.5
        assert self._apply(self._apply(az)) == pytest.approx(az)


class TestHamlibRotatorController:
    def _make_ctrl(self) -> HamlibRotatorController:
        return HamlibRotatorController(model_id=1, port="/dev/null")

    def test_initial_state_disconnected(self) -> None:
        ctrl = self._make_ctrl()
        assert not ctrl.is_connected

    def test_connect_mock_mode(self) -> None:
        if not HAMLIB_AVAILABLE:
            ctrl = self._make_ctrl()
            assert ctrl.connect()
            assert ctrl.is_connected
            ctrl.disconnect()

    def test_operations_when_disconnected_are_safe(self) -> None:
        ctrl = self._make_ctrl()
        assert ctrl.set_position(180.0, 45.0) is False
        state = ctrl.get_position()
        assert state.azimuth_deg == 0.0
        assert ctrl.stop() is False
        assert ctrl.park() is False

    @pytest.mark.skipif(HAMLIB_AVAILABLE, reason="mock-only test")
    def test_full_workflow_mock(self) -> None:
        ctrl = self._make_ctrl()
        assert ctrl.connect()

        assert ctrl.set_position(180.0, 45.0)
        state = ctrl.get_position()
        assert state.azimuth_deg == 180.0
        assert state.elevation_deg == 45.0
        assert state.is_moving

        assert ctrl.stop()
        assert ctrl.park()
        ctrl.disconnect()
        assert not ctrl.is_connected

    def test_net_mode_connect_fails_without_server(self) -> None:
        ctrl = HamlibRotatorController(net_mode=True, net_host="localhost", net_port=4533)
        # ソケット接続をモックして環境依存を排除する
        with patch("rig.controller.socket.socket") as mock_cls:
            mock_sock = MagicMock()
            mock_sock.connect.side_effect = ConnectionRefusedError("connection refused")
            mock_cls.return_value = mock_sock
            result = ctrl.connect()
        assert result is False

    def _make_net_ctrl_connected(self) -> HamlibRotatorController:
        ctrl = HamlibRotatorController(net_mode=True)
        mock_sock = MagicMock(spec=socket.socket)
        mock_sock.recv.return_value = b"180.0\n45.0\nRPRT 0\n"
        ctrl._sock = mock_sock
        with ctrl._lock:
            ctrl._state = RigState.CONNECTED
        return ctrl

    def test_net_set_position_sends_command(self) -> None:
        ctrl = self._make_net_ctrl_connected()
        assert ctrl.set_position(270.0, 30.0)
        ctrl._sock.sendall.assert_called()  # type: ignore[union-attr]

    def test_net_get_position_parses_response(self) -> None:
        ctrl = self._make_net_ctrl_connected()
        state = ctrl.get_position()
        assert state.azimuth_deg == 180.0
        assert state.elevation_deg == 45.0

    def test_zero_crossing_forward_reenters_catchup(self) -> None:
        ctrl = self._make_net_ctrl_connected()
        ctrl._last_az = 350.0
        ctrl._catching_up = False
        assert ctrl.set_position(1.0, 10.0)
        assert ctrl._catching_up
        assert ctrl._last_az == pytest.approx(1.0)

    def test_zero_crossing_reverse_reenters_catchup(self) -> None:
        ctrl = self._make_net_ctrl_connected()
        ctrl._last_az = 5.0
        ctrl._catching_up = False
        assert ctrl.set_position(355.0, 10.0)
        assert ctrl._catching_up
        assert ctrl._last_az == pytest.approx(355.0)

    def test_net_stop_sends_command(self) -> None:
        ctrl = self._make_net_ctrl_connected()
        assert ctrl.stop()
        ctrl._sock.sendall.assert_called_with(b"S\n")  # type: ignore[union-attr]

    def test_net_park_sends_command(self) -> None:
        ctrl = self._make_net_ctrl_connected()
        assert ctrl.park()
        ctrl._sock.sendall.assert_called_with(b"K\n")  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# HamlibVersionChecker
# ---------------------------------------------------------------------------


class TestHamlibVersionChecker:
    def test_get_installed_version_returns_string(self) -> None:
        checker = HamlibVersionChecker()
        ver = checker.get_installed_version()
        assert isinstance(ver, str)
        assert len(ver) > 0

    def test_not_installed_returns_not_installed(self) -> None:
        if not HAMLIB_AVAILABLE:
            checker = HamlibVersionChecker()
            assert checker.get_installed_version() == "not installed"

    def test_version_lt_basic(self) -> None:
        assert HamlibVersionChecker._version_lt("4.5.0", "4.6.0")
        assert HamlibVersionChecker._version_lt("4.5.0", "4.5.1")
        assert HamlibVersionChecker._version_lt("3.9.9", "4.0.0")
        assert not HamlibVersionChecker._version_lt("4.6.0", "4.5.0")
        assert not HamlibVersionChecker._version_lt("4.6.0", "4.6.0")

    def test_version_lt_handles_non_numeric(self) -> None:
        # クラッシュしないことを確認
        assert isinstance(HamlibVersionChecker._version_lt("4.5.x", "4.6.0"), bool)

    @pytest.mark.asyncio
    async def test_check_version_network_error_returns_safe_result(self) -> None:
        """ネットワーク不通時は is_outdated=False で返す。"""
        checker = HamlibVersionChecker()
        with patch("rig.controller.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get.side_effect = httpx.ConnectError("unreachable")
            mock_client_cls.return_value = mock_client

            result = await checker.check_version()

        assert isinstance(result, VersionInfo)
        assert result.is_outdated is False
        assert isinstance(result.installed, str)

    @pytest.mark.asyncio
    async def test_check_version_detects_outdated(self) -> None:
        """インストール版より新しいリリースがある場合 is_outdated=True。"""
        checker = HamlibVersionChecker()

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "tag_name": "v99.0.0",
            "html_url": "https://github.com/Hamlib/Hamlib/releases/tag/v99.0.0",
        }
        mock_response.raise_for_status = MagicMock()

        with patch("rig.controller.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            # Hamlib がない環境では "not installed" → is_outdated=False になるので
            # インストール済みバージョンをモックする
            with patch.object(checker, "get_installed_version", return_value="4.5.0"):
                result = await checker.check_version()

        assert result.latest == "99.0.0"
        assert result.is_outdated is True
        assert "99.0.0" in result.warning_message

    @pytest.mark.asyncio
    async def test_check_version_not_outdated_when_current(self) -> None:
        """インストール版が最新と同じなら is_outdated=False。"""
        checker = HamlibVersionChecker()

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "tag_name": "v4.5.0",
            "html_url": "https://example.com",
        }
        mock_response.raise_for_status = MagicMock()

        with patch("rig.controller.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            with patch.object(checker, "get_installed_version", return_value="4.5.0"):
                result = await checker.check_version()

        assert result.is_outdated is False
        assert result.warning_message == ""


# ---------------------------------------------------------------------------
# SdrRigAdapter retune deadband
# ---------------------------------------------------------------------------


class _FakeSdrDevice:
    """Minimal stand-in recording every accepted set_center_freq() call."""

    def __init__(self) -> None:
        self.writes: list[float] = []
        self.center_freq: float = 0.0
        self.fail_next = False

    def set_center_freq(self, freq_hz: float) -> bool:
        if self.fail_next:
            self.fail_next = False
            return False
        self.writes.append(freq_hz)
        self.center_freq = freq_hz
        return True


class TestSdrRetuneDeadband:
    """Slow Doppler drift must not re-lock the tuner PLL every cycle."""

    def _adapter(self) -> tuple[SdrRigAdapter, _FakeSdrDevice]:
        adapter = SdrRigAdapter()
        dev = _FakeSdrDevice()
        adapter._sdr_device = dev  # type: ignore[assignment]
        return adapter, dev

    def test_first_write_always_applied(self) -> None:
        adapter, dev = self._adapter()
        assert adapter.set_frequency(435_612_000.0) is True
        assert dev.writes == [435_612_000.0]

    def test_small_doppler_drift_suppressed(self) -> None:
        adapter, dev = self._adapter()
        adapter.set_frequency(435_612_000.0)
        # 50 Hz per cycle: +50/+100/+150 all stay under the 200 Hz deadband.
        for i in range(1, 4):
            assert adapter.set_frequency(435_612_000.0 + 50.0 * i) is True
        assert dev.writes == [435_612_000.0]

    def test_drift_beyond_deadband_writes_once(self) -> None:
        adapter, dev = self._adapter()
        adapter.set_frequency(435_612_000.0)
        adapter.set_frequency(435_612_150.0)  # inside
        adapter.set_frequency(435_612_250.0)  # crosses -> written
        adapter.set_frequency(435_612_300.0)  # inside again, new reference
        assert dev.writes == [435_612_000.0, 435_612_250.0]

    def test_deadband_is_measured_from_last_write_not_last_request(self) -> None:
        """Suppressed requests must not creep the reference frequency.

        Otherwise a long run of sub-deadband steps would never write at
        all, and the SDR would drift arbitrarily far off frequency.
        """
        adapter, dev = self._adapter()
        adapter.set_frequency(435_612_000.0)
        for i in range(1, 9):
            adapter.set_frequency(435_612_000.0 + 60.0 * i)
        # +60/+120/+180 suppressed, +240 lands and becomes the new
        # reference; +300/+360/+420 are then suppressed relative to it and
        # +480 lands.  A reference that crept with every request would
        # instead never write again after the first one.
        assert dev.writes == [435_612_000.0, 435_612_240.0, 435_612_480.0]

    def test_invalidate_forces_next_write(self) -> None:
        adapter, dev = self._adapter()
        adapter.set_frequency(435_612_000.0)
        adapter.invalidate_retune_cache()
        # 100 Hz -- the smallest Passband Tune step, well under the deadband.
        assert adapter.set_frequency(435_612_100.0) is True
        assert dev.writes == [435_612_000.0, 435_612_100.0]

    def test_failed_write_is_not_cached(self) -> None:
        """A rejected retune must not become the deadband reference."""
        adapter, dev = self._adapter()
        dev.fail_next = True
        assert adapter.set_frequency(435_612_000.0) is False
        assert dev.writes == []
        assert adapter.set_frequency(435_612_010.0) is True
        assert dev.writes == [435_612_010.0]

    def test_zero_deadband_restores_per_cycle_retuning(self) -> None:
        adapter, dev = self._adapter()
        adapter.set_retune_deadband(0.0)
        for hz in (435_612_000.0, 435_612_001.0, 435_612_002.0):
            adapter.set_frequency(hz)
        assert len(dev.writes) == 3

    def test_disconnect_clears_reference(self) -> None:
        adapter, dev = self._adapter()
        adapter.set_frequency(435_612_000.0)
        adapter.disconnect()
        adapter._sdr_device = dev  # type: ignore[assignment]
        assert adapter.set_frequency(435_612_010.0) is True
        assert dev.writes == [435_612_000.0, 435_612_010.0]

    def test_set_vfo_frequencies_ignores_uplink(self) -> None:
        adapter, dev = self._adapter()
        adapter.set_vfo_frequencies(435_612_000.0, 145_993_000.0)
        adapter.set_vfo_frequencies(None, 145_994_000.0)
        assert dev.writes == [435_612_000.0]

    def test_no_device_returns_false(self) -> None:
        adapter = SdrRigAdapter()
        assert adapter.set_frequency(435_612_000.0) is False
