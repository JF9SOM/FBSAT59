#!/usr/bin/env bash
# PreToolUse hook (matcher: Edit|Write).
#
# When Claude is about to edit code under a mapped src/ area, inject the relevant
# docs/*.md files into the model's context — ONCE per session per area — so the
# hardware-verified constraints and past-bug write-ups for that area are always
# present before the edit. This is the "make it actually automatic" layer on top
# of CLAUDE.md's trigger table and the src/*/CLAUDE.md stubs.
#
# Reads the hook payload (JSON) on stdin; emits JSON with
# hookSpecificOutput.additionalContext when it wants to inject, otherwise nothing.
set -uo pipefail

payload="$(cat)"

tool="$(printf '%s' "$payload" | jq -r '.tool_name // empty')"
case "$tool" in
  Edit | Write) ;;
  *) exit 0 ;;
esac

fpath="$(printf '%s' "$payload" | jq -r '.tool_input.file_path // empty')"
[ -n "$fpath" ] || exit 0
sid="$(printf '%s' "$payload" | jq -r '.session_id // "nosession"')"

repo_root="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
rel="${fpath#"$repo_root"/}"

key=""
docs=()
case "$rel" in
  src/comms/meteor/*)
    key="meteor"
    docs=(docs/meteor-satdump.md)
    ;;
  src/comms/*)
    key="comms"
    docs=(docs/communications.md)
    ;;
  src/rig/*)
    key="rig"
    docs=(docs/hamlib.md docs/rig-specific-notes.md)
    ;;
  src/sdr/*)
    key="sdr"
    docs=(docs/sdr.md)
    ;;
  src/data/*)
    key="data"
    docs=(docs/tle.md)
    ;;
  src/core/celestial_engine.py)
    key="moon"
    docs=(docs/moon-eme.md)
    ;;
  src/core/autotrack.py)
    key="autotrack"
    docs=(docs/ui-components.md docs/meteor-satdump.md)
    ;;
  src/core/*)
    key="core"
    docs=(docs/doppler-tuning.md)
    ;;
  src/ui/main_window.py)
    # main_window.py hosts _doppler_cycle / _rig_send / dial-feedback / Autotrack
    # wiring — the most bug-dense file in the docs. Inject the concern docs, not
    # just the generic UI doc.
    key="main-window"
    docs=(docs/lock-dial-feedback.md docs/doppler-tuning.md docs/ui-components.md)
    ;;
  src/ui/*)
    key="ui"
    docs=(docs/ui-components.md)
    ;;
  src/i18n/*)
    key="i18n"
    docs=(docs/i18n.md)
    ;;
  *)
    exit 0
    ;;
esac

marker_dir="${TMPDIR:-/tmp}/claude-fbsat59-docinj/${sid}"
mkdir -p "$marker_dir" 2>/dev/null || true
marker="${marker_dir}/${key}"
[ -e "$marker" ] && exit 0
: >"$marker" 2>/dev/null || true

ctx="あなたは \`${rel}\` を編集しようとしています。まず以下の FBSAT59 詳細ドキュメントを"
ctx+="権威ある情報として扱ってください（この領域の過去の不具合・実機で確定した制約が"
ctx+="記録されています）。全文を貼り付けてあるので、ファイルを開き直す必要はありません。"
for d in "${docs[@]}"; do
  if [ -f "${repo_root}/${d}" ]; then
    ctx+=$'\n\n===== '"${d}"$' =====\n'
    ctx+="$(cat "${repo_root}/${d}")"
  fi
done

jq -n --arg c "$ctx" \
  '{hookSpecificOutput: {hookEventName: "PreToolUse", additionalContext: $c}}'
