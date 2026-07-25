#!/usr/bin/env bash
# build_q65lib.sh — build libq65.{so,dylib,dll} from WSJT-X's own Q65 decode
# engine (lib/q65_decode.f90 and its dependency closure), wrapped through
# scripts/wsjtx_bridge/q65wsjt_bridge.f90.
#
# The file list below was derived by iteratively compiling+linking
# lib/q65_decode.f90's call graph against a real WSJT-X checkout until every
# undefined reference was resolved, then confirmed end-to-end with a
# round-trip test: encode a message with this repo's own Python Q65 TX
# encoder (src/comms/q65/encoder.py), decode the resulting audio through
# this library, and check the decoded text matches.
#
# Deliberately excludes contest-mode-only files (q65_set_list2.f90's real
# callers are gated behind ncontest==1, which the bridge always passes as 0)
# — q65_set_list2.f90 itself is still linked because Fortran resolves all
# called subroutines at link time regardless of runtime branching.
#
# Usage:
#   ./scripts/build_q65lib.sh [WSJTX_SRC_DIR] [OUT_DIR]
#
# Env overrides: WSJTX_TAG (git ref to clone if WSJTX_SRC_DIR not given),
# FC, CC (compiler executables), LIB_NAME (output filename).

set -euo pipefail

WSJTX_SRC_DIR="${1:-}"
OUT_DIR="${2:-$(pwd)/q65lib-out}"
WSJTX_TAG="${WSJTX_TAG:-master}"
FC="${FC:-gfortran}"
CC="${CC:-gcc}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$SCRIPT_DIR/wsjtx_bridge"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

case "$(uname -s)" in
  Darwin) LIB_NAME="${LIB_NAME:-libq65.dylib}" ;;
  MINGW*|MSYS*|CYGWIN*) LIB_NAME="${LIB_NAME:-q65.dll}" ;;
  *) LIB_NAME="${LIB_NAME:-libq65.so}" ;;
esac

if [ -z "$WSJTX_SRC_DIR" ]; then
  WSJTX_SRC_DIR="$WORK_DIR/wsjtx-src"
  echo "Cloning wsjtx/wsjtx @ ${WSJTX_TAG} ..."
  git clone --depth 1 --branch "$WSJTX_TAG" https://github.com/wsjtx/wsjtx.git "$WSJTX_SRC_DIR" 2>/dev/null || \
  git clone --depth 1 https://github.com/wsjtx/wsjtx.git "$WSJTX_SRC_DIR"
fi

# Fortran files, in an order that satisfies module producer-before-consumer
# (module chain) plus plain leaf subroutines (order doesn't matter for
# those, but they still must all be present at link time). Paths are
# relative to WSJTX_SRC_DIR.
MODULE_CHAIN=(
  lib/types.f90
  lib/timer_module.f90
  lib/packjt.f90
  lib/77bit/packjt77.f90
  lib/prog_args.f90
  lib/fftw3mod.f90
  lib/four2a.f90
  lib/ana64.f90
  lib/qra/q65/q65.f90
  lib/qra/q65/q65_loops.f90
  lib/qra/q65/q65_ap.f90
  lib/qra/q65/q65_set_list.f90
  lib/qra/q65/q65_set_list2.f90
  lib/qra/q65/genq65.f90
  lib/ft8/ft8apset.f90
  lib/q65_decode.f90
)

LEAF_FILES=(
  lib/pfx.f90
  lib/sec0.f90
  lib/chkcall.f90
  lib/grid2deg.f90
  lib/deg2grid.f90
  lib/fmtmsg.f90
  lib/indexx.f90
  lib/pctile.f90
  lib/db.f90
  lib/smo121.f90
  lib/twkfreq.f90
  lib/spec64.f90
  lib/shell.f90
)

C_FILES=(
  lib/qra/q65/q65_subs.c
  lib/qra/q65/q65.c
  lib/qra/q65/qracodes.c
  lib/qra/q65/pdmath.c
  lib/qra/q65/fadengauss.c
  lib/qra/q65/fadenlorentz.c
  lib/qra/q65/npfwht.c
  lib/qra/q65/qra15_65_64_irr_e23.c
)

# q65.f90 (Fortran, defines "module q65") and q65.c (plain C, the LDPC
# codec) share the same basename — flattening both into one directory
# would make the C compile silently overwrite the Fortran .o file (this
# bit us during development: link errors reported "undefined reference to
# __q65_MOD_*" symbols that were, in fact, already compiled — just
# clobbered). Stage the C sources into their own headers-only include dir
# instead of the flat SRC_DIR to sidestep the collision entirely.
SRC_DIR="$WORK_DIR/src"
C_SRC_DIR="$WORK_DIR/csrc"
mkdir -p "$SRC_DIR" "$C_SRC_DIR"

echo "Staging Fortran sources ..."
for rel in "${MODULE_CHAIN[@]}" "${LEAF_FILES[@]}"; do
  src="$WSJTX_SRC_DIR/$rel"
  if [ ! -f "$src" ]; then
    echo "ERROR: missing expected upstream file: $rel" >&2
    echo "       (WSJT-X source layout may have changed since this script was written)" >&2
    exit 1
  fi
  cp "$src" "$SRC_DIR/$(basename "$rel")"
done

echo "Staging C sources ..."
for rel in "${C_FILES[@]}"; do
  src="$WSJTX_SRC_DIR/$rel"
  if [ ! -f "$src" ]; then
    echo "ERROR: missing expected upstream file: $rel" >&2
    exit 1
  fi
  cp "$src" "$C_SRC_DIR/$(basename "$rel")"
done
# Headers needed by the C sources (same directory in upstream).
for h in q65.h qracodes.h pdmath.h npfwht.h qra15_65_64_irr_e23.h; do
  cp "$WSJTX_SRC_DIR/lib/qra/q65/$h" "$C_SRC_DIR/"
done

# Our own maintained bridge file.
cp "$BRIDGE_DIR/q65wsjt_bridge.f90" "$SRC_DIR/"

BUILD_DIR="$WORK_DIR/build"
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"

# gfortran's `include`/`use` module search does not search default system
# C include paths, so fftw3.f03 (from libfftw3-dev / homebrew fftw /
# conda-forge fftw) must be located explicitly — same issue already solved
# for build_ft4wsjt.sh.
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

FFLAGS="-O2 -fPIC -I$SRC_DIR -I$FFTW3_F03_DIR"
OBJS=()

echo "Compiling Fortran module chain ..."
for rel in "${MODULE_CHAIN[@]}"; do
  f="$SRC_DIR/$(basename "$rel")"
  obj="$(basename "${f%.f90}").o"
  "$FC" $FFLAGS -c "$f" -o "$obj"
  OBJS+=("$obj")
done

echo "Compiling Fortran leaf files ..."
for rel in "${LEAF_FILES[@]}"; do
  base="$(basename "$rel")"
  # pfx.f90 is textually `include`d by packjt.f90, not separately compiled.
  case "$base" in
    pfx.f90) continue ;;
  esac
  f="$SRC_DIR/$base"
  obj="${base%.f90}.o"
  "$FC" $FFLAGS -c "$f" -o "$obj"
  OBJS+=("$obj")
done

echo "Compiling our own bridge file ..."
"$FC" $FFLAGS -c "$SRC_DIR/q65wsjt_bridge.f90" -o q65wsjt_bridge.o
OBJS+=("q65wsjt_bridge.o")

echo "Compiling C LDPC codec ..."
for rel in "${C_FILES[@]}"; do
  base="$(basename "$rel" .c)"
  # q65.c and q65.f90 share a basename (see comment above) — always give
  # the C object an unambiguous name.
  obj="${base}_c.o"
  "$CC" -O2 -fPIC -I"$C_SRC_DIR" -c "$C_SRC_DIR/$base.c" -o "$obj"
  OBJS+=("$obj")
done

# Derive the lib dir from wherever fftw3.f03's include dir was found (e.g.
# Homebrew on Apple Silicon: /opt/homebrew/include -> /opt/homebrew/lib).
FFTW3_LIB_DIR="${FFTW3_F03_DIR%/include}/lib"

mkdir -p "$OUT_DIR"
echo "Linking $LIB_NAME ..."
case "$(uname -s)" in
  Darwin)
    "$FC" -dynamiclib -undefined dynamic_lookup "${OBJS[@]}" \
      -L"$FFTW3_LIB_DIR" -o "$OUT_DIR/$LIB_NAME" -lfftw3f
    ;;
  MINGW*|MSYS*|CYGWIN*)
    # -Wl,--export-all-symbols: MinGW GCC/gfortran only export symbols
    # explicitly marked for export by default; this codebase has no such
    # annotations (Linux .so builds export all globals by default without
    # it), so a Windows build without this flag loads fine but exposes none
    # of its functions. This is a PE/COFF-target-only ld option -- passing
    # it under Linux's plain ELF ld fails with "unrecognized option", so it
    # must stay gated to the MinGW/MSYS/Cygwin case, not the generic one.
    "$FC" -shared -fPIC -Wl,--export-all-symbols "${OBJS[@]}" \
      -L"$FFTW3_LIB_DIR" -o "$OUT_DIR/$LIB_NAME" -lfftw3f -lm
    ;;
  *)
    "$FC" -shared -fPIC "${OBJS[@]}" \
      -L"$FFTW3_LIB_DIR" -o "$OUT_DIR/$LIB_NAME" -lfftw3f -lm
    ;;
esac

echo "Built: $OUT_DIR/$LIB_NAME"
