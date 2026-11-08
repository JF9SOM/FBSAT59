#!/usr/bin/env bash
# Run `gh <args>` and try again when it fails. GitHub's release-file downloads
# sometimes answer "504 Gateway Time-out" for a while (it broke the v0.3.90 builds).
# Usage: scripts/gh_retry.sh release download direwolf-bundle --repo ... --pattern ...
rc=1
for i in 1 2 3 4 5 6; do
    if gh "$@"; then
        exit 0
    fi
    rc=$?
    echo "gh $* failed (attempt $i/6, exit $rc)" >&2
    if [ "$i" -lt 6 ]; then
        sleep $((15 * i))
    fi
done
exit "$rc"
