#!/usr/bin/env bash
# Smoke test for a freshly built `ssdv`: encode a JPEG into SSDV packets, decode
# them again and check a JPEG comes out. Used by .github/workflows/build-ssdv.yml.
#
# Usage: scripts/ssdv_smoke_test.sh <path to ssdv or ssdv.exe>
# Needs Python with Pillow (to make the test JPEG).
set -euo pipefail

SSDV="${1:?usage: ssdv_smoke_test.sh <ssdv binary>}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PY=python3
command -v python3 >/dev/null 2>&1 || PY=python

# 160x112 4:2:0 baseline JPEG (dimensions are multiples of 16, as ssdv requires)
"$PY" - "$WORK/in.jpg" <<'PYEOF'
import sys
from PIL import Image, ImageDraw

im = Image.new("RGB", (160, 112), (40, 80, 160))
ImageDraw.Draw(im).ellipse((30, 20, 120, 90), fill=(230, 200, 40))
im.save(sys.argv[1], quality=85, subsampling=2)
PYEOF

for mode in "" "-n" "-l 100"; do
    # shellcheck disable=SC2086
    "$SSDV" -e -c TEST -i 1 $mode "$WORK/in.jpg" "$WORK/packets.bin"
    # shellcheck disable=SC2086
    "$SSDV" -d $mode "$WORK/packets.bin" "$WORK/out.jpg"
    # a JPEG starts with FF D8
    if [ "$(head -c 2 "$WORK/out.jpg" | od -An -tx1 | tr -d ' \n')" != "ffd8" ]; then
        echo "ssdv smoke test FAILED (mode: '${mode}'): output is not a JPEG" >&2
        exit 1
    fi
    echo "ssdv smoke test OK (mode: '${mode:-default}'): $(wc -c < "$WORK/out.jpg") bytes"
done
