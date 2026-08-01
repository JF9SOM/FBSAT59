"""
Extract SoapySDR dylibs and Python bindings from conda-forge (osx-arm64) packages.

Usage:
    python scripts/extract_soapy_conda_macos.py

Reads all *.conda and *.tar.bz2 files in the current directory and extracts:
  - lib/*.dylib                          -> soapy-macos/lib/
  - lib/python3.11/site-packages/SoapySDR.py, _SoapySDR*.so
                                          -> soapy-macos/python/
  - lib/SoapySDR/modules0.8/*.so         -> soapy-macos/modules/

conda-forge macOS packages store the SONAME-level dylib name (e.g.
libSoapySDR.0.8.dylib, the exact name other binaries reference via @rpath)
as a tar *symlink* member pointing at the real file (libSoapySDR.0.8.1.dylib).
Symlinks don't carry their own data in a tarball, and re-tarring/uploading
them as a CI artifact is an easy way to lose them silently, so this script
dereferences every symlink under lib/ and writes the target's actual bytes
under the symlink's own name — the extracted directory ends up flat, with
no symlinks, and @rpath/libSoapySDR.0.8.dylib resolves to a real file.

Dependency-of-dependency dylibs (librtlsdr, libhackrf, libusb, etc., each
pulled in by its own separate conda-forge package) land in the same
soapy-macos/lib/ via the same code path — this script does not
distinguish "SoapySDR itself" from "a device library it depends on".

rpath fixup (making the extracted, relocated files actually find each
other via @loader_path/@rpath) is intentionally NOT done here — see the
"Fix up SoapySDR dylib rpaths for macOS bundle" CI step, which runs
dylibbundler on the actual macOS runner (rpath rewriting needs
install_name_tool, a macOS-only tool).
"""

import bz2
import glob
import io
import os
import tarfile
import zipfile

import zstandard


def extract_conda(fname: str, lib_dir: str, py_dir: str, mod_dir: str) -> None:
    if fname.endswith(".conda"):
        with zipfile.ZipFile(fname) as z:
            pkg_name = next(n for n in z.namelist() if n.startswith("pkg-"))
            data = z.read(pkg_name)
            dctx = zstandard.ZstdDecompressor()
            with tarfile.open(fileobj=io.BytesIO(dctx.decompress(data))) as tf:
                _extract_members(tf, lib_dir, py_dir, mod_dir)
        return
    else:  # .tar.bz2
        with (
            open(fname, "rb") as f,
            tarfile.open(fileobj=io.BytesIO(bz2.decompress(f.read()))) as tf,
        ):
            _extract_members(tf, lib_dir, py_dir, mod_dir)
        return


def _extract_members(tf: tarfile.TarFile, lib_dir: str, py_dir: str, mod_dir: str) -> None:
    members = tf.getmembers()

    # First pass: cache the bytes of every regular file under lib/, keyed by
    # its full in-archive name, so symlinks (second pass, below) can be
    # dereferenced regardless of member ordering in the tarball.
    lib_file_bytes: dict[str, bytes] = {}
    for m in members:
        if m.isfile() and m.name.startswith("lib/"):
            fobj = tf.extractfile(m)
            if fobj:
                lib_file_bytes[m.name] = fobj.read()
                fobj.close()

    for m in members:
        name = m.name
        base = os.path.basename(name)

        if m.issym() and name.startswith("lib/") and name.endswith(".dylib"):
            # Resolve the symlink target relative to its own directory
            # (conda-forge symlinks are always same-directory, e.g.
            # "lib/libSoapySDR.0.8.dylib" -> "libSoapySDR.0.8.1.dylib").
            target_name = os.path.normpath(os.path.join(os.path.dirname(name), m.linkname))
            data = lib_file_bytes.get(target_name)
            if data is None:
                print(f"  [warn] symlink {name} -> {m.linkname}: target not in archive, skip")
                continue
            dest = os.path.join(lib_dir, base)
            with open(dest, "wb") as out:
                out.write(data)
            print(f"  -> {dest} (dereferenced symlink -> {m.linkname})")
            continue

        if not m.isfile():
            continue

        if name.startswith("lib/SoapySDR/modules0.8/") and name.endswith(".so"):
            dest = os.path.join(mod_dir, base)
        elif name.startswith("lib/python3.11/site-packages/") and (
            base == "SoapySDR.py" or (base.startswith("_SoapySDR") and base.endswith(".so"))
        ):
            dest = os.path.join(py_dir, base)
        elif name.startswith("lib/") and name.count("/") == 1 and name.endswith(".dylib"):
            # Direct child of lib/ only — excludes lib/python3.11/... and
            # lib/SoapySDR/... which are handled by the branches above.
            dest = os.path.join(lib_dir, base)
        else:
            continue

        fobj = tf.extractfile(m)
        if not fobj:
            continue
        with open(dest, "wb") as out:
            out.write(fobj.read())
        print(f"  -> {dest}")
        fobj.close()


os.makedirs("soapy-macos/lib", exist_ok=True)
os.makedirs("soapy-macos/python", exist_ok=True)
os.makedirs("soapy-macos/modules", exist_ok=True)

for fname in glob.glob("*.conda") + glob.glob("*.tar.bz2"):
    print(f"Extracting {fname}")
    extract_conda(fname, "soapy-macos/lib", "soapy-macos/python", "soapy-macos/modules")
