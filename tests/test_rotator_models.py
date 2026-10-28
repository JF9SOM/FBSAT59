"""Tests for the rotator model list of the Rotator Settings dialog."""

from __future__ import annotations

import pytest

from rig.controller import HAMLIB_AVAILABLE
from ui import rotator_dialog


@pytest.mark.skipif(not HAMLIB_AVAILABLE, reason="needs the Hamlib binding")
def test_model_list_comes_from_hamlib() -> None:
    models = rotator_dialog._load_rot_models()
    ids = {mid for mid, _mfg, _name in models}
    # Far more than the old hard-coded list, and the models the app relies on.
    assert len(models) > len(rotator_dialog._FALLBACK_ROT_MODELS)
    assert {608, 2801, 603} <= ids
    assert models == sorted(models, key=lambda x: (x[1].lower(), x[2].lower()))


def test_model_list_falls_back_when_hamlib_gives_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rotator_dialog, "_load_from_hamlib_rot_api", lambda: [])
    models = rotator_dialog._load_rot_models()
    assert {mid for mid, _m, _n in models} == {
        mid for mid, _m, _n in rotator_dialog._FALLBACK_ROT_MODELS
    }


@pytest.mark.skipif(not HAMLIB_AVAILABLE, reason="needs the Hamlib binding")
def test_scan_restores_the_hamlib_debug_level() -> None:
    import Hamlib

    Hamlib.rig_set_debug(Hamlib.RIG_DEBUG_ERR)
    try:
        rotator_dialog._load_from_hamlib_rot_api()
        assert Hamlib.rig_get_debug() == Hamlib.RIG_DEBUG_ERR
    finally:
        Hamlib.rig_set_debug(Hamlib.RIG_DEBUG_NONE)
