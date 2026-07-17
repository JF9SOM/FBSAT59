"""
Patch pothosware/SoapyRemote's top-level CMakeLists.txt to build only the
client plugin (remoteSupport.dll), not the server executable.

Why: server/CMakeLists.txt does
    target_link_libraries(SoapySDRServer PRIVATE SoapySDR SoapySDRRemoteCommon)
which references a bare "SoapySDR" cmake target. Our generated
SoapySDRConfig.cmake (scripts/write_soapy_cmake_config.py) only defines the
namespaced "SoapySDR::SoapySDR" IMPORTED target (matching what SoapyRTLSDR's
CMakeLists.txt already consumes via SOAPY_SDR_MODULE_UTIL). Configuring with
add_subdirectory(server) still present would fail with "target SoapySDR not
found" before we ever get to building the client. We only need the client
plugin for FBSAT59 (it connects TO a remote SoapySDRServer, it never runs
one), so drop the server subdirectory instead of also emulating a bare
"SoapySDR" target for code we don't ship.

add_subdirectory(system) is already gated to Linux only in the same
CMakeLists.txt and needs no patching here.
"""

import sys

SRC = "SoapyRemote/CMakeLists.txt"
TARGET_LINE = "add_subdirectory(server)"

with open(SRC, encoding="utf-8", errors="replace") as f:
    content = f.read()

if TARGET_LINE not in content:
    print(f"ERROR: '{TARGET_LINE}' not found in {SRC}", file=sys.stderr)
    print(content, file=sys.stderr)
    sys.exit(1)

patched = content.replace(
    TARGET_LINE,
    "# " + TARGET_LINE + "  # removed by scripts/patch_soapyremote_client_only.py"
    " (FBSAT59 only needs the client plugin)",
    1,
)

with open(SRC, "w", encoding="utf-8") as f:
    f.write(patched)

print(f"Patched {SRC}: disabled add_subdirectory(server).")
