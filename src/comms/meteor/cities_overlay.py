"""Re-render a received MSU-MR pass with a cities overlay via SatDump itself.

SatDump's GUI "Cities Overlay" checkbox is implemented by ``OverlayHandler``
(``src-core/common/overlay_handler.*`` in the SatDump source), which is not
exposed by the ``live`` receiving pipeline used elsewhere in this project.
A separate CLI subcommand, ``satdump project`` (``src-cli/project/project.cpp``),
accepts the same ``OverlayHandler`` settings as flags on its ``-target``
layer and can read the ``product.cbor`` file SatDump already writes
alongside every reception's PNGs — so FBSAT59 does not need to reimplement
the satellite's geometry/projection math itself.

Confirmed against a real ``product.cbor`` from a past reception::

    satdump project \\
        -layer --type product --file product.cbor --composite "AVHRR 221 False Color" \\
        -target --file output.png --type equirec --auto_mode --auto_scale_mode \\
                --draw_cities_overlay --cities_type 0 --cities_scale_rank 3

``auto_mode``/``auto_scale_mode`` let SatDump pick the projected image's
bounds/resolution itself, cropped to wherever the pass actually has data,
so no width/height needs to be guessed here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from comms.meteor.satdump import find_satdump

# The composite SatDump's own preset already enables white_balance for
# (see satdump_cfg.json's "AVHRR 221 False Color" entry) -- confirmed to
# produce a natural-looking Earth image, which is what the cities overlay
# is meant to be added on top of. Not user-selectable (yet): keeping this a
# single well-tested combination avoids exposing SatDump's full composite
# catalog (many entries need channels this satellite/pipeline may not emit).
CITIES_OVERLAY_COMPOSITE = "AVHRR 221 False Color"

# GUI default (city_categories[0]) and a middle-of-the-road scale rank —
# both settable in overlay_handler.cpp's drawUI(), mirrored here as fixed
# values rather than new user-facing controls.
_CITIES_TYPE = 0
_CITIES_SCALE_RANK = 3

_TIMEOUT_S = 120


def find_product_cbor(reception_dir: Path) -> Path | None:
    """Return the ``product.cbor`` under *reception_dir*, if any.

    SatDump writes exactly one ``product.cbor`` per instrument subfolder
    (e.g. ``MSU-MR/product.cbor``) alongside the PNGs from
    ``--finish_processing``. Older receptions that never got that far (no
    lock, or the app was closed before the post-processing pass finished)
    have no such file.
    """
    matches = sorted(reception_dir.rglob("product.cbor"))
    return matches[0] if matches else None


class CitiesOverlayProcess(QThread):
    """Runs ``satdump project`` in the background to add a cities overlay.

    Signals
    -------
    finished_ok(str)
        The overlay image was written; carries its path.
    finished_err(str)
        SatDump failed, timed out, or could not be found.
    """

    finished_ok = Signal(str)
    finished_err = Signal(str)

    def __init__(
        self,
        product_cbor: Path,
        output_path: Path,
        parent: object | None = None,
    ) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self._product_cbor = product_cbor
        self._output_path = output_path

    def run(self) -> None:
        satdump = find_satdump()
        if satdump is None:
            self.finished_err.emit(
                "satdump executable not found.\n"
                "Please install SatDump and make sure it is on PATH.\n"
                "See Help > SatDump… for instructions."
            )
            return

        cmd = [
            str(satdump),
            "project",
            "-layer",
            "--type",
            "product",
            "--file",
            str(self._product_cbor),
            "--composite",
            CITIES_OVERLAY_COMPOSITE,
            "-target",
            "--file",
            str(self._output_path),
            "--type",
            "equirec",
            "--auto_mode",
            "--auto_scale_mode",
            "--draw_cities_overlay",
            "--cities_type",
            str(_CITIES_TYPE),
            "--cities_scale_rank",
            str(_CITIES_SCALE_RANK),
        ]

        try:
            # Same CREATE_NO_WINDOW reasoning as SatDumpProcess: satdump.exe
            # is a console-subsystem executable and would otherwise pop up a
            # visible console window from this windowed PyInstaller build.
            # Unlike `live`, `project` is a short one-shot batch tool with no
            # graceful-shutdown path to preserve, so plain subprocess.run()
            # with a timeout is enough -- no CTRL_C_EVENT dance needed.
            if sys.platform == "win32":
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=_TIMEOUT_S,
                    creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
                )
            else:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=_TIMEOUT_S,
                )
        except subprocess.TimeoutExpired:
            self.finished_err.emit("satdump project timed out.")
            return
        except OSError as exc:
            self.finished_err.emit(f"Failed to start satdump: {exc}")
            return

        if result.returncode != 0 or not self._output_path.is_file():
            detail = (result.stderr or result.stdout or "").strip()
            msg = f"satdump project exited with code {result.returncode}"
            if detail:
                msg += f"\n{detail}"
            self.finished_err.emit(msg)
            return

        self.finished_ok.emit(str(self._output_path))
