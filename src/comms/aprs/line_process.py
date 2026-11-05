"""Start a console program and read its output line by line, unbuffered.

Direwolf (like most C programs on Windows) block-buffers its stdout when that is
a pipe, so a ``subprocess.PIPE`` reader sees nothing until the buffer fills or
the program exits (Issue #35: "0 line(s) read").  On Windows 10 1809+ the
program is therefore started on a pseudo console (ConPTY), which makes it
believe it writes to a real console and flush every line.  Elsewhere (and where
ConPTY is unavailable) an ordinary pipe is used, which is line buffered on
macOS / Linux for Direwolf.

Both variants expose the same small interface: ``lines`` (a queue of text
lines, ``None`` at the end of the output), ``poll()``, ``terminate()``,
``kill()`` and ``wait()``.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# CSI sequences (colours, cursor moves, ...) and OSC sequences (window title).
_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]"
)


def split_console_text(pending: str, chunk: str) -> tuple[list[str], str]:
    """Add *chunk* to the unfinished text *pending*; return (complete lines, rest)."""
    text = _ANSI_RE.sub("", pending + chunk)
    parts = re.split(r"\r\n|\n|\r", text)
    return [p for p in parts[:-1] if p.strip()], parts[-1]


class LineProcess:
    """Common interface of the two process wrappers (documentation only)."""

    lines: queue.Queue[str | None]

    def poll(self) -> int | None:  # pragma: no cover - interface
        raise NotImplementedError

    def terminate(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def kill(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def wait(self, timeout: float | None = None) -> int | None:  # pragma: no cover
        raise NotImplementedError


class PipeLineProcess(LineProcess):
    """A normal subprocess whose stdout is read line by line on a thread."""

    def __init__(self, args: list[str], cwd: Path) -> None:
        extra: dict[str, Any] = {}
        if sys.platform == "win32":
            extra["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        self._proc = subprocess.Popen(
            args,
            cwd=cwd,
            # Kept open: Direwolf may quit when its stdin reaches EOF.
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            **extra,
        )
        self.lines = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            self.lines.put(line.rstrip())
        self.lines.put(None)

    def poll(self) -> int | None:
        return self._proc.poll()

    def terminate(self) -> None:
        self._proc.terminate()

    def kill(self) -> None:
        self._proc.kill()

    def wait(self, timeout: float | None = None) -> int | None:
        return self._proc.wait(timeout=timeout)


class ConPtyProcess(LineProcess):  # pragma: no cover - Windows only
    """A process on a Windows pseudo console (ConPTY); raises OSError if unavailable."""

    _COLS = 250
    _ROWS = 50
    _STILL_ACTIVE = 259

    def __init__(self, args: list[str], cwd: Path) -> None:
        import ctypes as _ctypes
        from ctypes import wintypes

        # Typed as Any: the Windows-only ctypes members are unknown off Windows.
        ctypes: Any = _ctypes
        self._ct = ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32 = k32
        if not hasattr(k32, "CreatePseudoConsole"):
            raise OSError("ConPTY is not available on this Windows version")

        class COORD(_ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class STARTUPINFOW(_ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD),
                ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD),
                ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD),
                ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE),
            ]

        class STARTUPINFOEXW(_ctypes.Structure):
            _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]

        class PROCESS_INFORMATION(_ctypes.Structure):
            _fields_ = [
                ("hProcess", wintypes.HANDLE),
                ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD),
            ]

        handle = ctypes.c_void_p
        k32.CreatePipe.argtypes = [
            ctypes.POINTER(handle),
            ctypes.POINTER(handle),
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        k32.CreatePseudoConsole.argtypes = [
            COORD,
            handle,
            handle,
            wintypes.DWORD,
            ctypes.POINTER(handle),
        ]
        k32.CreatePseudoConsole.restype = ctypes.c_long
        k32.ClosePseudoConsole.argtypes = [handle]
        k32.CloseHandle.argtypes = [handle]
        k32.InitializeProcThreadAttributeList.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        k32.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_size_t,
            handle,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        k32.CreateProcessW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        k32.ReadFile.argtypes = [
            handle,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        k32.TerminateProcess.argtypes = [handle, wintypes.UINT]
        k32.GetExitCodeProcess.argtypes = [handle, ctypes.POINTER(wintypes.DWORD)]
        k32.WaitForSingleObject.argtypes = [handle, wintypes.DWORD]

        in_read, in_write = handle(), handle()
        out_read, out_write = handle(), handle()
        if not k32.CreatePipe(ctypes.byref(in_read), ctypes.byref(in_write), None, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        if not k32.CreatePipe(ctypes.byref(out_read), ctypes.byref(out_write), None, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        self._hpc = handle()
        hr = k32.CreatePseudoConsole(
            COORD(self._COLS, self._ROWS), in_read, out_write, 0, ctypes.byref(self._hpc)
        )
        if hr != 0:
            raise OSError(f"CreatePseudoConsole failed (HRESULT {hr & 0xFFFFFFFF:#x})")
        # The pseudo console owns these ends now.
        k32.CloseHandle(in_read)
        k32.CloseHandle(out_write)
        self._in_write = in_write
        self._out_read = out_read

        size = ctypes.c_size_t(0)
        k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        self._attr_buf = ctypes.create_string_buffer(size.value)
        attr_ptr = ctypes.cast(self._attr_buf, ctypes.c_void_p)
        if not k32.InitializeProcThreadAttributeList(attr_ptr, 1, 0, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        # PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE
        if not k32.UpdateProcThreadAttribute(
            attr_ptr, 0, 0x00020016, self._hpc, ctypes.sizeof(handle), None, None
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        siex = STARTUPINFOEXW()
        siex.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        siex.lpAttributeList = attr_ptr
        pi = PROCESS_INFORMATION()
        cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(args))
        # EXTENDED_STARTUPINFO_PRESENT
        if not k32.CreateProcessW(
            None,
            cmdline,
            None,
            None,
            False,
            0x00080000,
            None,
            str(cwd),
            ctypes.byref(siex),
            ctypes.byref(pi),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        k32.CloseHandle(pi.hThread)
        self._hproc = handle(pi.hProcess)
        self._closed = False
        self.lines = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        ct = self._ct
        from ctypes import wintypes

        buf = ct.create_string_buffer(4096)
        n = wintypes.DWORD(0)
        pending = ""
        decoder_pending = b""
        while self._k32.ReadFile(self._out_read, buf, 4096, ct.byref(n), None) and n.value:
            data = decoder_pending + buf.raw[: n.value]
            try:
                text = data.decode("utf-8")
                decoder_pending = b""
            except UnicodeDecodeError as exc:
                # A multi-byte character split across reads: keep its head for next time.
                if exc.start >= len(data) - 3 and exc.reason == "unexpected end of data":
                    text = data[: exc.start].decode("utf-8", errors="replace")
                    decoder_pending = data[exc.start :]
                else:
                    text = data.decode("utf-8", errors="replace")
                    decoder_pending = b""
            done, pending = split_console_text(pending, text)
            for line in done:
                self.lines.put(line)
        if pending.strip():
            self.lines.put(pending.strip())
        self.lines.put(None)

    def poll(self) -> int | None:
        from ctypes import wintypes

        code = wintypes.DWORD(0)
        if not self._k32.GetExitCodeProcess(self._hproc, self._ct.byref(code)):
            return None
        return None if code.value == self._STILL_ACTIVE else int(code.value)

    def kill(self) -> None:
        if self.poll() is None:
            self._k32.TerminateProcess(self._hproc, 1)

    terminate = kill

    def wait(self, timeout: float | None = None) -> int | None:
        ms = 0xFFFFFFFF if timeout is None else int(timeout * 1000)
        if self._k32.WaitForSingleObject(self._hproc, ms) != 0:
            raise subprocess.TimeoutExpired("direwolf", timeout or 0)
        self._close()
        return self.poll()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Closing the pseudo console ends the output pipe, which stops the reader.
        threading.Thread(target=self._close_handles, daemon=True).start()

    def _close_handles(self) -> None:
        with contextlib.suppress(Exception):
            self._k32.ClosePseudoConsole(self._hpc)
        with contextlib.suppress(Exception):
            self._reader.join(2)
        for h in (self._in_write, self._out_read, self._hproc):
            with contextlib.suppress(Exception):
                self._k32.CloseHandle(h)


def start_line_process(args: list[str], cwd: Path) -> LineProcess:
    """Start *args* so that its output arrives promptly, line by line."""
    if sys.platform == "win32":
        try:
            return ConPtyProcess(args, cwd)
        except Exception as exc:  # noqa: BLE001 - fall back to a plain pipe
            logger.warning("ConPTY unavailable (%s); using a plain pipe", exc)
    return PipeLineProcess(args, cwd)
