"""
Fix up @rpath/dylib references in the extracted SoapySDR macOS bundle,
without dylibbundler.

Two workflow_dispatch CI runs (2026-08-01) hung indefinitely on
dylibbundler while processing conda-forge-built SoapySDR binaries —
even a minimal single-file invocation with a hard CI-level
timeout-minutes could not be killed. dylibbundler works fine elsewhere
in the same CI job (the locally-built Hamlib .so), so the problem is
specific to something about the conda-forge SoapySDR binaries, not this
runner or dylibbundler in general — but the exact mechanism was never
identified, since no log output ever appeared before the hang. Rather
than keep guessing at dylibbundler flags, this script reimplements the
narrow slice of dylibbundler's behavior actually needed here directly
via `otool -L` / `install_name_tool`, both simple, single-purpose macOS
tools with no history of hanging in this project.

Usage:
    python3 scripts/fix_soapy_rpaths_macos.py \
        --fix-file <path> [--fix-file <path> ...] \
        --dest-dir <dir> \
        --install-path <@loader_path | @loader_path/..> \
        --search-path <dir>

For each --fix-file (already sitting at its final location — this
script edits it in place, it does not copy it):
  1. `otool -L` lists its direct dependencies.
  2. Any dependency whose basename matches a file in --search-path is
     "ours" (extracted from conda-forge alongside it) and gets:
       - copied into --dest-dir (if not already there)
       - its reference in the fix-file rewritten via
         `install_name_tool -change <old> <install-path>/<basename>`
       - recursively processed the same way, except recursively
         discovered dependencies always reference each other via plain
         @loader_path (not --install-path) since they all end up
         co-located in --dest-dir regardless of where the original
         --fix-file lives.
  Any dependency NOT found in --search-path (system libraries like
  libc++, libSystem, Security.framework, etc.) is left untouched.

Every binary this script modifies (with install_name_tool) is
re-signed ad-hoc (`codesign --force -s -`) afterwards — required on
Apple Silicon, where install_name_tool invalidates the existing
signature and an unsigned dylib will not load.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys


def otool_deps(path: str) -> list[str]:
    out = subprocess.run(["otool", "-L", path], capture_output=True, text=True, check=True).stdout
    lines = out.splitlines()[1:]  # first line is "<path>:", not a dependency
    deps = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # "<dep path> (compatibility version X.Y.Z, current version A.B.C)"
        dep = line.rsplit(" (compatibility version", 1)[0].strip()
        deps.append(dep)
    return deps


def codesign(path: str) -> None:
    subprocess.run(["codesign", "--force", "-s", "-", path], check=True)


def fix_deps(
    path: str,
    own_install_path: str,
    dest_dir: str,
    search_files: dict[str, str],
    processed: set[str],
) -> None:
    changed = False
    for dep in otool_deps(path):
        base = os.path.basename(dep)
        if base not in search_files:
            continue  # not ours — a system library, leave it alone
        dest = os.path.join(dest_dir, base)
        if not os.path.exists(dest):
            shutil.copy(search_files[base], dest)
            os.chmod(dest, 0o755)
            print(f"  [copy] {search_files[base]} -> {dest}")
        new_ref = f"{own_install_path}/{base}"
        if dep != new_ref:
            subprocess.run(["install_name_tool", "-change", dep, new_ref, path], check=True)
            print(f"  [fix]  {os.path.basename(path)}: {dep} -> {new_ref}")
            changed = True
        if base not in processed:
            processed.add(base)
            # Everything recursively discovered lives together in
            # dest_dir, so siblings always reference each other via
            # plain @loader_path — regardless of the top-level
            # --install-path (which only applies to the original
            # --fix-file's own references, set by the caller).
            fix_deps(dest, "@loader_path", dest_dir, search_files, processed)
            subprocess.run(
                ["install_name_tool", "-id", f"@loader_path/{base}", dest],
                check=True,
            )
            codesign(dest)
    if changed:
        codesign(path)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--fix-file", action="append", required=True, dest="fix_files")
    p.add_argument("--dest-dir", required=True)
    p.add_argument("--install-path", required=True)
    p.add_argument("--search-path", required=True)
    args = p.parse_args()

    search_files = {
        os.path.basename(f): os.path.join(args.search_path, f) for f in os.listdir(args.search_path)
    }
    os.makedirs(args.dest_dir, exist_ok=True)
    processed: set[str] = set()

    for target in args.fix_files:
        print(f"=== fixing {target} (install-path {args.install_path}) ===")
        fix_deps(target, args.install_path, args.dest_dir, search_files, processed)

    return 0


if __name__ == "__main__":
    sys.exit(main())
