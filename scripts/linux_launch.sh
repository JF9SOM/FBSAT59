#!/usr/bin/env bash
# =====================================================================
# FBSAT59 - Linux one-click launcher (run from a source checkout)
#
# Point a desktop launcher (.desktop, Terminal=true) at this script.  On each
# start it:
#   1. git pull --ff-only     (offline / local edits -> skipped)
#   2. pip install -e .[dev,sdr,ax100digi]
#                             (only when pyproject.toml changed)
#   3. python src/main.py
#
# Unlike scripts/win_launch.bat there is no bootstrap_natives.py step: that
# downloader is Windows-only.  On Linux Hamlib comes from the venv's
# activate script (LD_LIBRARY_PATH) and the other native pieces from the
# system packages.
#
# The terminal stays open while the app runs (so you can watch the log) and
# closes when the app exits cleanly.  A failure (missing venv, non-zero exit)
# waits for Enter so the message can be read.
# =====================================================================

# Everything lives in a function that is parsed in full before it runs.  The
# git pull below can rewrite this very file; bash reads scripts incrementally,
# so top-level commands after the pull would otherwise execute from a
# half-replaced file.
main() {
    local root
    root="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)" || return 1
    cd "$root" || return 1

    local venv="$root/.venv"
    if [ ! -f "$venv/bin/activate" ]; then
        echo
        echo "[FBSAT59] Python venv not found at: $venv"
        echo
        echo "[FBSAT59] Create it once, then start this launcher again:"
        echo "    python3 -m venv --system-site-packages .venv"
        echo "    .venv/bin/python -m pip install -e '.[dev,sdr,ax100digi]'"
        echo
        read -r -p "Press Enter to close..." _
        return 1
    fi

    echo "[FBSAT59] Updating source..."
    local head_before head_after
    head_before="$(git rev-parse HEAD 2>/dev/null)"
    # timeout: an unreachable network (e.g. the GPD acting as an access point)
    # must not stall the launch.  GIT_TERMINAL_PROMPT: never wait for a login.
    if ! GIT_TERMINAL_PROMPT=0 timeout 60 git pull --ff-only; then
        echo "[FBSAT59] git pull skipped (offline or local changes) - using the current checkout."
    fi
    head_after="$(git rev-parse HEAD 2>/dev/null)"

    # shellcheck disable=SC1091
    source "$venv/bin/activate"

    if [ "$head_before" != "$head_after" ] &&
        git diff --name-only "$head_before" "$head_after" 2>/dev/null | grep -qx "pyproject.toml"; then
        echo "[FBSAT59] Dependencies changed - running pip install -e .[dev,sdr,ax100digi]..."
        python -m pip install -e '.[dev,sdr,ax100digi]' -q ||
            echo "[FBSAT59] pip install failed - starting with the packages already installed."
    fi

    echo "[FBSAT59] Starting FBSAT59..."
    python src/main.py
    local rc=$?
    echo "[FBSAT59] FBSAT59 exited (code $rc)."
    if [ "$rc" -ne 0 ]; then
        read -r -p "Press Enter to close..." _
    fi
    return "$rc"
}

main "$@"
exit $?
