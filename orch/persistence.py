"""Stato atomico e lock esclusivo con controllo del PID."""

import ctypes
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4


class StateError(RuntimeError):
    pass


class LockError(RuntimeError):
    pass


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class StateStore:
    def __init__(self, base: Path):
        self.path = base / "stato.json"

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise StateError(f"Stato non leggibile; non verrà sovrascritto: {exc}") from exc
        if not isinstance(value, dict):
            raise StateError("stato.json deve contenere un oggetto JSON")
        return value

    def save(self, state: dict) -> None:
        atomic_write(self.path, (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: PID inesistente
                return False
            if error == 5:  # Non sostituire il lock di un processo non interrogabile.
                return True
            raise LockError(f"Impossibile verificare il PID {pid}: errore Windows {error}")
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise LockError(f"Impossibile leggere lo stato del PID {pid}")
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ProcessLock:
    def __init__(self, base: Path, *, alive=pid_is_alive):
        self.path = base / "orchestratore.lock"
        self.alive = alive
        self.handle = None
        self.payload = json.dumps({"pid": os.getpid(), "token": uuid4().hex})

    def __enter__(self):
        for _ in range(10):
            try:
                self.handle = self.path.open("x", encoding="utf-8")
            except FileExistsError:
                try:
                    old = self.path.read_text(encoding="utf-8")
                    parsed = json.loads(old)
                    pid = parsed["pid"] if isinstance(parsed, dict) else parsed
                    if type(pid) is not int:
                        raise ValueError("PID non intero")
                except (ValueError, KeyError, UnicodeError) as exc:
                    raise LockError("Lock non valido: impossibile verificarne il proprietario") from exc
                except FileNotFoundError:
                    continue
                if self.alive(pid):
                    raise LockError(f"Orchestratore già in esecuzione (PID {pid})")
                try:
                    if self.path.read_text(encoding="utf-8") == old:
                        self.path.unlink()
                except (FileNotFoundError, PermissionError):
                    pass
                continue
            try:
                self.handle.write(self.payload)
                self.handle.flush()
                os.fsync(self.handle.fileno())
            except BaseException:
                self.handle.close()
                self.path.unlink(missing_ok=True)
                raise
            return self
        raise LockError("Impossibile acquisire il lock dopo la rimozione del lock stantio")

    def __exit__(self, *exc):
        if self.handle:
            self.handle.close()
        try:
            if self.path.read_text(encoding="utf-8") == self.payload:
                self.path.unlink()
        except FileNotFoundError:
            pass
