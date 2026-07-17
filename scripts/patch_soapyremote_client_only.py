"""
Patch pothosware/SoapyRemote's CMakeLists.txt files to build only the
client plugin (remoteSupport.dll), not the server executable, and to
consistently link against our namespaced "SoapySDR::SoapySDR" IMPORTED
target instead of a bare "SoapySDR" target name.

Why: our generated SoapySDRConfig.cmake (scripts/write_soapy_cmake_config.py)
only defines the namespaced "SoapySDR::SoapySDR" IMPORTED target (matching
what SoapyRTLSDR's CMakeLists.txt already consumes via SOAPY_SDR_MODULE_UTIL).
Two places in SoapyRemote's own CMakeLists.txt reference a bare "SoapySDR"
target name instead:

  1. server/CMakeLists.txt:
         target_link_libraries(SoapySDRServer PRIVATE SoapySDR SoapySDRRemoteCommon)
     We only need the client plugin for FBSAT59 (it connects TO a remote
     SoapySDRServer, it never runs one), so drop the server subdirectory
     entirely rather than also emulating a bare "SoapySDR" target for code
     we don't ship.

  2. common/CMakeLists.txt (built as a static lib, consumed by the client):
         target_link_libraries(SoapySDRRemoteCommon PRIVATE SoapySDR)
     Unlike an undefined CMake *target*, a plain library *name* with no
     matching target is valid syntax to target_link_libraries() (CMake
     falls back to treating it as a raw linker flag), so this does NOT
     fail at configure time — it silently fails to propagate
     INTERFACE_INCLUDE_DIRECTORIES from our SoapySDR::SoapySDR target,
     so every .cpp in common/ fails at compile time with
     "Cannot open include file: 'SoapySDR/Config.hpp'". This one must be
     rewritten to use SoapySDR::SoapySDR, not just dropped.

add_subdirectory(system) is already gated to Linux only in the same
top-level CMakeLists.txt and needs no patching here.
"""

import sys

TOP_SRC = "SoapyRemote/CMakeLists.txt"
TOP_TARGET_LINE = "add_subdirectory(server)"

with open(TOP_SRC, encoding="utf-8", errors="replace") as f:
    top_content = f.read()

if TOP_TARGET_LINE not in top_content:
    print(f"ERROR: '{TOP_TARGET_LINE}' not found in {TOP_SRC}", file=sys.stderr)
    print(top_content, file=sys.stderr)
    sys.exit(1)

top_patched = top_content.replace(
    TOP_TARGET_LINE,
    "# " + TOP_TARGET_LINE + "  # removed by scripts/patch_soapyremote_client_only.py"
    " (FBSAT59 only needs the client plugin)",
    1,
)

with open(TOP_SRC, "w", encoding="utf-8") as f:
    f.write(top_patched)

print(f"Patched {TOP_SRC}: disabled add_subdirectory(server).")

COMMON_SRC = "SoapyRemote/common/CMakeLists.txt"
COMMON_FIND = "target_link_libraries(SoapySDRRemoteCommon PRIVATE SoapySDR)"
COMMON_REPLACE = "target_link_libraries(SoapySDRRemoteCommon PRIVATE SoapySDR::SoapySDR)"

with open(COMMON_SRC, encoding="utf-8", errors="replace") as f:
    common_content = f.read()

if COMMON_FIND not in common_content:
    print(f"ERROR: '{COMMON_FIND}' not found in {COMMON_SRC}", file=sys.stderr)
    print(common_content, file=sys.stderr)
    sys.exit(1)

common_patched = common_content.replace(COMMON_FIND, COMMON_REPLACE, 1)

with open(COMMON_SRC, "w", encoding="utf-8") as f:
    f.write(common_patched)

print(f"Patched {COMMON_SRC}: SoapySDR -> SoapySDR::SoapySDR.")
