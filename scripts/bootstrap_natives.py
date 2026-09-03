#!/usr/bin/env python3
"""Download the pre-built native components FBSAT59 needs to run from source.

The Windows one-click launcher (``scripts/win_launch.bat``) runs this before
starting the app:

    .venv\\Scripts\\python.exe scripts\\bootstrap_natives.py

Every component is a release asset on one of this project's own GitHub
releases (repo ``JF9SOM/fbsat59``).  Each one is downloaded once into the
per-user data directory the application already searches
(``platformdirs.user_data_dir("fbsat59")``) and then skipped on later runs
unless ``--force`` is given.  A network failure is reported but never fatal so
the app can still start offline with whatever is already installed.

Components: ``hamlib`` (rig control), ``ft8lib`` (FT8/FT4 decode), ``q65lib``
(Q65), ``ft4wsjt`` (WSJT-X FT4 engine), ``direwolf`` (APRS), ``cwmodel`` (CW
decoder ONNX model + ``onnxruntime``).

Usage:
    bootstrap_natives.py [--force] [--only a,b] [--skip a,b] [--quiet]

This file is intentionally dependency-light (standard library + ``platformdirs``
+ the project's own ``src.core.hamlib_info``) so it can run under a bare venv.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import platformdirs  # noqa: E402  (after sys.path setup)
from src.core.hamlib_info import (  # noqa: E402
    HAMLIB_BUNDLE_REPO,
    HAMLIB_BUNDLE_TAG,
    _asset_pattern,
    parse_asset_version,
    version_key,
)

_DEFAULT_REPO = "JF9SOM/fbsat59"
_USER_AGENT = "fbsat59-bootstrap-natives"

# CW decoder model — raw file, not a release asset.
_CW_MODEL_URL = "https://raw.githubusercontent.com/e04/deepcw-engine/main/model.onnx"


def _data_root() -> Path:
    """Return ``platformdirs.user_data_dir("fbsat59")`` as a Path."""
    return Path(platformdirs.user_data_dir("fbsat59"))


@dataclass(frozen=True)
class Component:
    """One downloadable native component."""

    name: str
    subdir: str
    key_file: str  # relative to the install dir; its presence means "installed"
    release_tag: str | None = None  # None -> use the /releases/latest endpoint
    asset_name: str | None = None  # exact Windows asset file name
    repo: str = _DEFAULT_REPO
    extra_env_keys: tuple[str, ...] = field(default=())

    def install_dir(self) -> Path:
        return _data_root() / self.subdir


# Order matters only for readability; Hamlib first because it is the one users
# most often need.  The Windows asset for every release below is a flat .zip.
_COMPONENTS: tuple[Component, ...] = (
    Component(
        name="hamlib",
        subdir="hamlib",
        key_file="Hamlib.py",
        release_tag=HAMLIB_BUNDLE_TAG,
        asset_name=None,  # resolved from the version-stamped asset list
        repo=HAMLIB_BUNDLE_REPO,
    ),
    Component(
        name="ft8lib",
        subdir="ft8lib",
        key_file="ft8.dll",
        release_tag="ft8lib-bundle",
        asset_name="ft8lib-windows-x86_64.zip",
    ),
    Component(
        name="q65lib",
        subdir="q65lib",
        key_file="q65.dll",
        release_tag="q65lib-bundle",
        asset_name="q65lib-windows-x86_64.zip",
    ),
    Component(
        name="ft4wsjt",
        subdir="ft4wsjt",
        key_file="ft4wsjt.dll",
        release_tag="ft4wsjt-bundle",
        asset_name="ft4wsjt-windows-x86_64.zip",
    ),
    Component(
        name="direwolf",
        subdir="direwolf",
        key_file="direwolf.exe",
        release_tag=None,  # latest published release
        asset_name="direwolf-windows-x86_64.zip",
    ),
    # cwmodel is handled specially (pip + raw download); listed for --only/--skip.
    Component(
        name="cwmodel",
        subdir="cwmodel",
        key_file="model.onnx",
    ),
)


class BootstrapError(RuntimeError):
    """A component could not be installed (network, missing asset, …)."""


def _log(msg: str, *, quiet: bool) -> None:
    if not quiet:
        print(f"[bootstrap] {msg}", flush=True)


def _github_get(url: str) -> dict:
    """GET a GitHub API URL and return the decoded JSON object."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": _USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    if not isinstance(data, dict):
        raise BootstrapError(f"unexpected GitHub API response from {url}")
    return data


def _resolve_release(comp: Component) -> dict:
    if comp.release_tag is None:
        url = f"https://api.github.com/repos/{comp.repo}/releases/latest"
    else:
        url = f"https://api.github.com/repos/{comp.repo}/releases/tags/{comp.release_tag}"
    try:
        return _github_get(url)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise BootstrapError(f"cannot reach GitHub release for {comp.name}: {exc}") from exc


def _pick_asset(comp: Component, release: dict) -> tuple[str, str, str | None]:
    """Return (asset_name, download_url, version) for this platform."""
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        raise BootstrapError(f"{comp.name}: malformed asset list")

    if comp.name == "hamlib":
        pattern = _asset_pattern()  # (prefix, suffix) for the current OS/pyver
        if pattern is None:
            raise BootstrapError("hamlib: unsupported platform")
        prefix, suffix = pattern
        candidates: list[tuple[str, str, str]] = []
        for asset in assets:
            aname = asset.get("name", "")
            if aname.startswith(prefix) and aname.endswith(suffix):
                ver = parse_asset_version(aname)
                if ver:
                    candidates.append((aname, asset.get("browser_download_url", ""), ver))
        if not candidates:
            raise BootstrapError(
                f"hamlib: no asset matching {prefix}<version>{suffix} in the release"
            )
        candidates.sort(key=lambda c: version_key(c[2]))
        aname, url, ver = candidates[-1]
        return aname, url, ver

    assert comp.asset_name is not None
    for asset in assets:
        if asset.get("name") == comp.asset_name:
            return comp.asset_name, asset.get("browser_download_url", ""), release.get("tag_name")
    raise BootstrapError(
        f"{comp.name}: asset {comp.asset_name!r} not found in release "
        f"{release.get('tag_name', '?')}"
    )


def _download(url: str, dest: Path, *, quiet: bool) -> None:
    if not url:
        raise BootstrapError("empty download URL")
    _log(f"downloading {url}", quiet=quiet)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, dest.open("wb") as fh:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                fh.write(chunk)
    except (urllib.error.URLError, OSError) as exc:
        raise BootstrapError(f"download failed: {exc}") from exc


def _extract_flat(archive: Path, dest_dir: Path) -> None:
    """Extract *archive* into *dest_dir*.

    Windows assets are flat zips; .tar.gz assets (other platforms) have a single
    top-level directory that is stripped so the layout matches.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest_dir)
        return
    with tarfile.open(archive) as tf:
        members = tf.getmembers()
        prefix = members[0].name.split("/")[0] + "/" if members else ""
        for member in members:
            if member.name.startswith(prefix):
                member.name = member.name[len(prefix) :]
            if member.name:
                tf.extract(member, dest_dir)


def _install_release_component(comp: Component, *, force: bool, quiet: bool) -> str:
    install_dir = comp.install_dir()
    key = install_dir / comp.key_file
    if key.exists() and not force:
        return "present"

    release = _resolve_release(comp)
    asset_name, url, version = _pick_asset(comp, release)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / asset_name
        _download(url, archive, quiet=quiet)
        _extract_flat(archive, install_dir)

    if not key.exists():
        raise BootstrapError(f"{comp.name}: {comp.key_file} missing after extracting {asset_name}")
    if version:
        (install_dir / "version.txt").write_text(version, encoding="ascii")
    return f"installed {version or asset_name}"


def _install_cwmodel(comp: Component, *, force: bool, quiet: bool) -> str:
    install_dir = comp.install_dir()
    model = install_dir / comp.key_file

    # onnxruntime is a plain pip package; install it into the running venv.
    try:
        import onnxruntime  # noqa: F401

        have_ort = True
    except ImportError:
        have_ort = False
    if not have_ort:
        _log("installing onnxruntime via pip", quiet=quiet)
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "onnxruntime"],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if result.returncode != 0:
            raise BootstrapError("pip install onnxruntime failed:\n" + result.stderr[-500:])

    if model.exists() and not force:
        return "present (onnxruntime ok)"

    install_dir.mkdir(parents=True, exist_ok=True)
    _download(_CW_MODEL_URL, model, quiet=quiet)
    if model.stat().st_size < 1_000_000:  # the real model is ~15 MB
        model.unlink(missing_ok=True)
        raise BootstrapError("cwmodel: downloaded model.onnx looks truncated")
    return "installed model.onnx"


def _select(only: str | None, skip: str | None) -> list[Component]:
    names = [c.name for c in _COMPONENTS]
    chosen = set(names)
    if only:
        want = {n.strip() for n in only.split(",") if n.strip()}
        unknown = want - set(names)
        if unknown:
            raise SystemExit(f"--only: unknown component(s): {', '.join(sorted(unknown))}")
        chosen = want
    if skip:
        chosen -= {n.strip() for n in skip.split(",") if n.strip()}
    return [c for c in _COMPONENTS if c.name in chosen]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--force", action="store_true", help="re-download even if already installed"
    )
    parser.add_argument("--only", metavar="A,B", help="only these components")
    parser.add_argument("--skip", metavar="A,B", help="exclude these components")
    parser.add_argument("--quiet", action="store_true", help="less output")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        _log(f"not Windows (sys.platform={sys.platform}); nothing to do", quiet=args.quiet)
        return 0

    components = _select(args.only, args.skip)
    results: list[tuple[str, str, str]] = []  # (name, status, detail)
    for comp in components:
        try:
            if comp.name == "cwmodel":
                detail = _install_cwmodel(comp, force=args.force, quiet=args.quiet)
            else:
                detail = _install_release_component(comp, force=args.force, quiet=args.quiet)
            status = "OK" if detail.startswith("installed") else "SKIP"
            results.append((comp.name, status, detail))
        except BootstrapError as exc:
            results.append((comp.name, "FAIL", str(exc)))

    width = max(len(name) for name, _, _ in results)
    print("[bootstrap] summary:")
    for name, status, detail in results:
        print(f"[bootstrap]   {name.ljust(width)}  {status:<5} {detail}")

    # Never fail the launcher: the app degrades gracefully when a component is
    # absent, and a transient network error should not block startup.
    failed = [n for n, s, _ in results if s == "FAIL"]
    if failed:
        print(f"[bootstrap] {len(failed)} component(s) unavailable this run: {', '.join(failed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
