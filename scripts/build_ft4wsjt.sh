#!/usr/bin/env bash
# build_ft4wsjt.sh — build libft4wsjt.{so,dylib,dll} from WSJT-X's own FT4
# decode engine (lib/ft4_decode.f90 and its dependency closure), instead of
# the lightweight single-pass kgoba/ft8_lib reference decoder.
#
# The file list below was derived by statically tracing lib/ft4_decode.f90's
# call graph and confirmed by actually linking + loading the result (see
# scripts/wsjtx_bridge/ for the small bridge files we maintain ourselves).
# It intentionally excludes all Qt/GUI/network code — only the pure
# computational decode chain is built.
#
# Usage:
#   ./scripts/build_ft4wsjt.sh [WSJTX_SRC_DIR] [OUT_DIR]
#
# Env overrides: WSJTX_TAG (git ref to clone if WSJTX_SRC_DIR not given),
# FC, CC, CXX (compiler executables), LIB_NAME (output filename).

set -euo pipefail

WSJTX_SRC_DIR="${1:-}"
OUT_DIR="${2:-$(pwd)/ft4wsjt-out}"
WSJTX_TAG="${WSJTX_TAG:-master}"
FC="${FC:-gfortran}"
CC="${CC:-gcc}"
CXX="${CXX:-g++}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$SCRIPT_DIR/wsjtx_bridge"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

case "$(uname -s)" in
  Darwin) LIB_NAME="${LIB_NAME:-libft4wsjt.dylib}" ;;
  MINGW*|MSYS*|CYGWIN*) LIB_NAME="${LIB_NAME:-ft4wsjt.dll}" ;;
  *) LIB_NAME="${LIB_NAME:-libft4wsjt.so}" ;;
esac

if [ -z "$WSJTX_SRC_DIR" ]; then
  WSJTX_SRC_DIR="$WORK_DIR/wsjtx-src"
  echo "Cloning wsjtx/wsjtx @ ${WSJTX_TAG} ..."
  git clone --depth 1 --branch "$WSJTX_TAG" https://github.com/wsjtx/wsjtx.git "$WSJTX_SRC_DIR" 2>/dev/null || \
  git clone --depth 1 https://github.com/wsjtx/wsjtx.git "$WSJTX_SRC_DIR"
fi

# Files that must be compiled in this order (module producers before consumers).
# Paths are relative to WSJTX_SRC_DIR.
MODULE_CHAIN=(
  lib/types.f90
  lib/C_interface_module.f90
  lib/crc.f90
  lib/timer_module.f90
  lib/fftw3mod.f90
  lib/four2a.f90
  lib/packjt.f90
  lib/77bit/packjt77.f90
  lib/ft8/encode174_91.f90
  lib/ft8/decode174_91.f90
  lib/ft4/genft4.f90
  lib/ft4/ft4_downsample.f90
  lib/ft4/getcandidates4.f90
  lib/ft4/sync4d.f90
  lib/ft4/get_ft4_bitmetrics.f90
  lib/ft4/subtractft4.f90
  lib/ft4/ft4_baseline.f90
  lib/ft4_decode.f90
)

# Plain subroutines/functions with no module interdependencies — order
# doesn't matter, but they must be present before the final link.
LEAF_FILES=(
  lib/pfx.f90
  lib/ft8/ldpc_174_91_c_generator.f90
  lib/ft8/ldpc_174_91_c_parity.f90
  lib/ft4/ft4_params.f90
  lib/grid2deg.f90
  lib/platanh.f90
  lib/chkcall.f90
  lib/deg2grid.f90
  lib/fmtmsg.f90
  lib/ft4/gen_ft4wave.f90
  lib/ft8/get_crc14.f90
  lib/nuttal_window.f90
  lib/ft8/osd174_91.f90
  lib/pctile.f90
  lib/polyfit.f90
  lib/ft8/twkfreq1.f90
  lib/determ.f90
  lib/ft8/encode174_91_nocrc.f90
  lib/ft2/gfsk_pulse.f90
  lib/indexx.f90
  lib/shell.f90
)

CXX_FILES=(
  lib/crc14.cpp
)

SRC_DIR="$WORK_DIR/src"
mkdir -p "$SRC_DIR/ft4" "$SRC_DIR/ft8"

echo "Staging sources ..."
for rel in "${MODULE_CHAIN[@]}" "${LEAF_FILES[@]}" "${CXX_FILES[@]}"; do
  src="$WSJTX_SRC_DIR/$rel"
  if [ ! -f "$src" ]; then
    echo "ERROR: missing expected upstream file: $rel" >&2
    echo "       (WSJT-X source layout may have changed since this script was written)" >&2
    exit 1
  fi
  cp "$src" "$SRC_DIR/$(basename "$rel")"
done
# ft4_decode.f90 includes 'ft4/ft4_params.f90' (subdirectory-relative) —
# keep a second copy under ft4/ to satisfy that include path.
cp "$WSJTX_SRC_DIR/lib/ft4/ft4_params.f90" "$SRC_DIR/ft4/ft4_params.f90"

# Our own maintained files (not fetched from upstream).
cp "$BRIDGE_DIR/normalizebmet.f90" "$SRC_DIR/"
cp "$BRIDGE_DIR/ft4wsjt_bridge.f90" "$SRC_DIR/"

BUILD_DIR="$WORK_DIR/build"
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"

# gfortran's `include` directive does not search default system C include
# paths, so fftw3.f03 (from libfftw3-dev / homebrew fftw / conda-forge fftw)
# must be located explicitly.
FFTW3_F03_DIR=""
for cand in /usr/include /usr/local/include /opt/homebrew/include \
            "${CONDA_PREFIX:-}/include" "${CONDA_PREFIX:-}/Library/include"; do
  if [ -f "$cand/fftw3.f03" ]; then
    FFTW3_F03_DIR="$cand"
    break
  fi
done
if [ -z "$FFTW3_F03_DIR" ]; then
  echo "ERROR: fftw3.f03 not found (install libfftw3-dev / brew fftw / conda-forge fftw)" >&2
  exit 1
fi

# crc14.cpp needs <boost/crc.hpp> (header-only). Homebrew on Apple Silicon
# installs to /opt/homebrew, which clang/g++ do not search by default, so
# this must be located explicitly just like fftw3.f03 above.
BOOST_INCLUDE_DIR=""
for cand in /usr/include /usr/local/include /opt/homebrew/include \
            "${CONDA_PREFIX:-}/include" "${CONDA_PREFIX:-}/Library/include"; do
  if [ -f "$cand/boost/crc.hpp" ]; then
    BOOST_INCLUDE_DIR="$cand"
    break
  fi
done
if [ -z "$BOOST_INCLUDE_DIR" ]; then
  echo "ERROR: boost/crc.hpp not found (install libboost-dev / brew boost / conda-forge boost)" >&2
  exit 1
fi

FFLAGS="-O2 -fPIC -I$SRC_DIR -I$FFTW3_F03_DIR"
OBJS=()

echo "Compiling module chain ..."
for rel in "${MODULE_CHAIN[@]}"; do
  f="$SRC_DIR/$(basename "$rel")"
  obj="$(basename "${f%.f90}").o"
  "$FC" $FFLAGS -c "$f" -o "$obj"
  OBJS+=("$obj")
done

echo "Compiling leaf files ..."
for rel in "${LEAF_FILES[@]}"; do
  base="$(basename "$rel")"
  # pfx.f90 and the two ldpc_174_91_c_*.f90 files are textually `include`d,
  # not separately compiled; ft4_params.f90 is include-only too.
  case "$base" in
    pfx.f90|ldpc_174_91_c_generator.f90|ldpc_174_91_c_parity.f90|ft4_params.f90) continue ;;
  esac
  f="$SRC_DIR/$base"
  obj="${base%.f90}.o"
  "$FC" $FFLAGS -c "$f" -o "$obj"
  OBJS+=("$obj")
done

echo "Compiling our own bridge files ..."
"$FC" $FFLAGS -c "$SRC_DIR/normalizebmet.f90" -o normalizebmet.o
OBJS+=("normalizebmet.o")
"$FC" $FFLAGS -c "$SRC_DIR/ft4wsjt_bridge.f90" -o ft4wsjt_bridge.o
OBJS+=("ft4wsjt_bridge.o")

echo "Compiling C++ CRC helper ..."
"$CXX" -O2 -fPIC -fpermissive -I"$BOOST_INCLUDE_DIR" -c "$SRC_DIR/crc14.cpp" -o crc14.o
OBJS+=("crc14.o")

mkdir -p "$OUT_DIR"
echo "Linking $LIB_NAME ..."
case "$(uname -s)" in
  Darwin)
    "$FC" -dynamiclib -undefined dynamic_lookup "${OBJS[@]}" \
      -o "$OUT_DIR/$LIB_NAME" -lfftw3f -lstdc++
    ;;
  *)
    "$FC" -shared -fPIC "${OBJS[@]}" \
      -o "$OUT_DIR/$LIB_NAME" -lfftw3f -lstdc++ -lm
    ;;
esac

echo "Built: $OUT_DIR/$LIB_NAME"
