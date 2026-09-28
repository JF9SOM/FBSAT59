"""Tests for scripts/bootstrap_natives.py archive extraction."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import bootstrap_natives as bn  # noqa: E402


def _make_zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


class TestExtractFlat:
    def test_flat_zip_is_extracted_as_is(self, tmp_path: Path) -> None:
        archive = _make_zip(tmp_path / "a.zip", {"ssdv.exe": b"x", "version.txt": b"1"})
        bn._extract_flat(archive, tmp_path / "out")
        assert (tmp_path / "out" / "ssdv.exe").read_bytes() == b"x"
        assert (tmp_path / "out" / "version.txt").exists()

    def test_single_top_level_directory_is_stripped(self, tmp_path: Path) -> None:
        archive = _make_zip(
            tmp_path / "a.zip",
            {"ssdv-flat/ssdv.exe": b"x", "ssdv-flat/COPYING": b"c", "ssdv-flat/sub/f.txt": b"s"},
        )
        bn._extract_flat(archive, tmp_path / "out")
        assert (tmp_path / "out" / "ssdv.exe").read_bytes() == b"x"
        assert (tmp_path / "out" / "sub" / "f.txt").read_bytes() == b"s"
        assert not (tmp_path / "out" / "ssdv-flat").exists()

    def test_backslash_separators_and_directory_entries(self, tmp_path: Path) -> None:
        # Windows PowerShell's Compress-Archive can write backslash paths and dir entries.
        archive = _make_zip(tmp_path / "a.zip", {"ssdv-flat/": b"", "ssdv-flat\\ssdv.exe": b"x"})
        bn._extract_flat(archive, tmp_path / "out")
        assert (tmp_path / "out" / "ssdv.exe").read_bytes() == b"x"

    def test_several_top_level_entries_are_left_alone(self, tmp_path: Path) -> None:
        archive = _make_zip(tmp_path / "a.zip", {"bin/a.dll": b"a", "b.dll": b"b"})
        bn._extract_flat(archive, tmp_path / "out")
        assert (tmp_path / "out" / "bin" / "a.dll").exists()
        assert (tmp_path / "out" / "b.dll").exists()

    def test_path_traversal_is_rejected(self, tmp_path: Path) -> None:
        archive = _make_zip(tmp_path / "a.zip", {"root/../../evil.txt": b"x", "root/ok.txt": b"y"})
        with pytest.raises(bn.BootstrapError):
            bn._extract_flat(archive, tmp_path / "out")
        assert not (tmp_path / "evil.txt").exists()
