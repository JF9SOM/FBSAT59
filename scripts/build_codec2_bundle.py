#!/usr/bin/env python3
"""Package libcodec2 (CODEC2 speech codec, LGPL-2.1) for the Digital Voice tab.

Takes the pre-built conda-forge packages (https://github.com/conda-forge/codec2-feedstock,
built from https://github.com/drowe67/codec2 1.2.0), checks their SHA-256, and writes one
archive per platform with just the shared library, the licence text and a source note:

    codec2-linux-x86_64.tar.gz   codec2-flat/libcodec2.so.1.2
    codec2-macos-arm64.tar.gz    codec2-flat/libcodec2.1.2.dylib
    codec2-windows-x86_64.zip    codec2-flat/libcodec2.dll

The archives go to the ``codec2-bundle`` release (workflow build-codec2.yml); ci.yml puts the
library next to the app (PyInstaller root), where comms/voice/codec2.py loads it with ctypes.
libcodec2 is only loaded at run time, never linked into the app, and its LGPL licence text
and source location ship with it.

Usage: build_codec2_bundle.py OUTPUT_DIR
"""

from __future__ import annotations

import hashlib
import io
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

import zstandard

VERSION = "1.2.0"
_BASE = "https://conda.anaconda.org/conda-forge"
# platform -> (conda subdir, package file, sha256, path of the library in the package,
#              library file name, archive file name)
PACKAGES: dict[str, tuple[str, str, str, str, str, str]] = {
    "linux-x86_64": (
        "linux-64",
        "libcodec2-1.2.0-hb9d3cd8_3.conda",
        "1bd08f09eb5f46f63bcd4cffd7786d98ec1b8d5934f5de2c292c2bd28eadb206",
        "lib/libcodec2.so.1.2",
        "libcodec2.so.1.2",
        "codec2-linux-x86_64.tar.gz",
    ),
    "macos-arm64": (
        "osx-arm64",
        "libcodec2-1.2.0-h5505292_3.conda",
        "1fe1d65012cd741443f4138c5f9641bcddaa7378d8901421ddf3d9b8b3c5d3cc",
        "lib/libcodec2.1.2.dylib",
        "libcodec2.1.2.dylib",
        "codec2-macos-arm64.tar.gz",
    ),
    "windows-x86_64": (
        "win-64",
        "libcodec2-1.2.0-hb66ff0d_3.conda",
        "989ad48c32db2a16f8b120382b9beb8a59eeaee00b33b96246d3a9d7ffb5ce76",
        "Library/bin/libcodec2.dll",
        "libcodec2.dll",
        "codec2-windows-x86_64.zip",
    ),
}
_LICENSE_MEMBER = "info/licenses/COPYING"


def _download(subdir: str, name: str, sha256: str) -> bytes:
    url = f"{_BASE}/{subdir}/{name}"
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - pinned https URL
        data: bytes = resp.read()
    got = hashlib.sha256(data).hexdigest()
    if got != sha256:
        raise SystemExit(f"SHA-256 mismatch for {name}: expected {sha256}, got {got}")
    return data


def _package_members(conda: bytes) -> dict[str, bytes]:
    """The files of a ``.conda`` package (a zip holding a zstd-compressed tarball)."""
    with zipfile.ZipFile(io.BytesIO(conda)) as zf:
        inner = next(n for n in zf.namelist() if n.startswith("pkg-"))
        raw = zstandard.ZstdDecompressor().stream_reader(zf.open(inner)).read()
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
        for member in tf.getmembers():
            if member.isfile():
                handle = tf.extractfile(member)
                if handle is not None:
                    out[member.name] = handle.read()
    return out


def build(platform: str, out_dir: Path) -> Path:
    """Write the archive for *platform* into *out_dir* and return its path."""
    subdir, package, sha256, lib_path, lib_name, archive = PACKAGES[platform]
    files = _package_members(_download(subdir, package, sha256))
    flat = {
        lib_name: files[lib_path],
        "COPYING": files[_LICENSE_MEMBER],
        "version.txt": f"{VERSION}\n".encode(),
        "SOURCE.txt": (
            f"libcodec2 {VERSION} (LGPL-2.1): https://github.com/drowe67/codec2\n"
            f"Pre-built by conda-forge: {_BASE}/{subdir}/{package}\n"
        ).encode(),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / archive
    if archive.endswith(".zip"):
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in flat.items():
                zf.writestr(f"codec2-flat/{name}", data)
    else:
        with tarfile.open(path, "w:gz") as tf:
            for name, data in flat.items():
                info = tarfile.TarInfo(f"codec2-flat/{name}")
                info.size = len(data)
                info.mode = 0o755 if name.startswith("libcodec2") else 0o644
                tf.addfile(info, io.BytesIO(data))
    return path


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    out = Path(sys.argv[1])
    for platform in PACKAGES:
        print(build(platform, out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
