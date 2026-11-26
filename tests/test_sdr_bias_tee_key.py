"""Bias-T writeSetting() key selection (fake device, no hardware).

A SoapyRemote proxy reports driver="remote", so the key must come from the
real hardware driver in the "remote:driver" arg. Before this was handled,
a remote HackRF received the generic "biastee" key, which the HackRF module
silently ignores, so Bias-T never switched on (found 2026-11-26).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import sdr.device as device_mod
from sdr.device import SdrDevice, SdrDeviceInfo


def _device(driver: str, args: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> SdrDevice:
    monkeypatch.setattr(device_mod, "SOAPY_AVAILABLE", True)
    info = SdrDeviceInfo(driver=driver, label="x", serial="1", hardware="", args=args)
    dev = SdrDevice(info)
    dev._dev = MagicMock()
    return dev


@pytest.mark.parametrize(
    ("driver", "args", "enabled", "expected"),
    [
        ("hackrf", {"driver": "hackrf"}, True, ("bias_tx", "true")),
        ("hackrf", {"driver": "hackrf"}, False, ("bias_tx", "false")),
        ("rtlsdr", {"driver": "rtlsdr"}, True, ("biastee", "1")),
        ("rtlsdr", {"driver": "rtlsdr"}, False, ("biastee", "0")),
        ("airspy", {"driver": "airspy"}, True, ("biastee", "true")),
        # SoapyRemote: the real driver comes from the remote:driver hint.
        (
            "remote",
            {"driver": "remote", "remote": "h:55132", "remote:driver": "hackrf"},
            True,
            ("bias_tx", "true"),
        ),
        (
            "remote",
            {"driver": "remote", "remote": "h:55132", "remote:driver": "HackRF"},
            False,
            ("bias_tx", "false"),
        ),
        (
            "remote",
            {"driver": "remote", "remote": "h:55132", "remote:driver": "rtlsdr"},
            True,
            ("biastee", "1"),
        ),
        # No hint: unchanged generic best-effort key.
        ("remote", {"driver": "remote", "remote": "h:55132"}, True, ("biastee", "true")),
    ],
)
def test_bias_tee_setting_key(
    driver: str,
    args: dict[str, str],
    enabled: bool,
    expected: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dev = _device(driver, args, monkeypatch)
    assert dev._bias_tee_setting(enabled) == expected


def test_set_bias_tee_writes_the_hackrf_key_through_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dev = _device(
        "remote",
        {"driver": "remote", "remote": "h:55132", "remote:driver": "hackrf"},
        monkeypatch,
    )
    assert dev.set_bias_tee(True) is True
    dev._dev.writeSetting.assert_called_once_with("bias_tx", "true")
