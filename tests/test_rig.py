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
    VersionInfo,
    _build_mode_map,
    _MockRig,
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
# HamlibDirectController satmode (IC-9100/9700) — Hamlib return-code checks
# ---------------------------------------------------------------------------


class TestSatmodeHamlibReturnCodeChecks:
    """_apply_mode_and_ctcss_hamlib() (IC-9100/9700 Direct-mode cross-band
    path) must surface a real Hamlib failure (RIG_E* return code) instead of
    reporting success -- Hamlib's Python binding returns the raw status code
    rather than raising, so every call site must check it explicitly. See
    _check_rig_ok()."""

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
    def _mock_hamlib_ok(mock_rig_inst: MagicMock) -> MagicMock:
        """Build a mock Hamlib module where every call reports RIG_OK (0)."""
        mock_hamlib = MagicMock()
        mock_hamlib.Rig.return_value = mock_rig_inst
        mock_hamlib.RIG_OK = 0
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
        for name in ("open", "close", "set_func", "set_freq", "set_mode", "set_ctcss_tone"):
            getattr(mock_rig_inst, name).return_value = 0
        return mock_hamlib

    def test_happy_path_returns_true_and_clears_last_error(self) -> None:
        ctrl = self._make_ctrl()
        mock_rig_inst = MagicMock()
        mock_hamlib = self._mock_hamlib_ok(mock_rig_inst)
        with (
            patch("rig.controller.HAMLIB_AVAILABLE", True),
            patch.dict("sys.modules", {"Hamlib": mock_hamlib}),
            patch("rig.controller.time.sleep"),
        ):
            ok = ctrl._apply_mode_and_ctcss_hamlib("USB", "LSB", 0.0)
        assert ok is True
        assert ctrl._last_hamlib_error is None

    def test_set_mode_failure_is_reported_not_swallowed(self) -> None:
        """A rejected/timed-out set_mode() (e.g. a Windows COM-port timing
        glitch) must make _apply_mode_and_ctcss_hamlib() return False with a
        specific reason, not silently report success like before this
        change (real symptom reported by a Windows 11 IC-9100 user: Connect
        succeeds, but mode is never actually set on the rig)."""
        ctrl = self._make_ctrl()
        mock_rig_inst = MagicMock()
        mock_hamlib = self._mock_hamlib_ok(mock_rig_inst)
        mock_rig_inst.set_mode.return_value = -5  # RIG_ETIMEOUT
        with (
            patch("rig.controller.HAMLIB_AVAILABLE", True),
            patch.dict("sys.modules", {"Hamlib": mock_hamlib}),
            patch("rig.controller.time.sleep"),
        ):
            ok = ctrl._apply_mode_and_ctcss_hamlib("USB", "LSB", 0.0)
        assert ok is False
        assert ctrl._last_hamlib_error is not None
        assert "set_mode" in ctrl._last_hamlib_error
        assert "-5" in ctrl._last_hamlib_error

    def test_apply_transponder_state_raises_with_specific_reason(self) -> None:
        """apply_transponder_state() must propagate the specific failure
        reason (not a generic "apply failed") so it reaches the status bar
        via main_window.py's existing RigControlError handling."""
        ctrl = self._make_ctrl()
        mock_rig_inst = MagicMock()
        mock_hamlib = self._mock_hamlib_ok(mock_rig_inst)
        mock_rig_inst.set_freq.return_value = -9  # RIG_ERJCTED
        with (
            patch("rig.controller.HAMLIB_AVAILABLE", True),
            patch.dict("sys.modules", {"Hamlib": mock_hamlib}),
            patch("rig.controller.time.sleep"),
            pytest.raises(RigControlError, match="freq preset"),
        ):
            ctrl.apply_transponder_state("USB", "LSB", 0.0)


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
