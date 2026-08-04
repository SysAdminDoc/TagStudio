# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

"""Platform-native recursive directory watching for live library updates."""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import select
import struct
import sys
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from threading import Event, Lock, Thread, get_ident
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class FileSystemEventKind(str, Enum):
    """The file-system changes that can affect a library entry."""

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    MOVED = "moved"


@dataclass(frozen=True, slots=True)
class FileSystemEvent:
    """A normalized file-system event emitted by a native watcher."""

    kind: FileSystemEventKind
    path: Path
    old_path: Path | None = None
    is_directory: bool = False


EventCallback = Callable[[FileSystemEvent], None]


class _WatcherBackend:
    name = "unsupported"

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


class LibraryWatcher:
    """Watch configured roots and emit normalized events from a daemon thread.

    The watcher never touches the library database. Consumers can therefore enqueue events
    and apply them on their own event loop or worker, which keeps SQLAlchemy sessions out of
    the native watcher threads.
    """

    def __init__(self, roots: Iterable[Path], callback: EventCallback) -> None:
        self.roots = tuple(Path(root).expanduser().resolve(strict=False) for root in roots)
        self.callback = callback
        self._backend: _WatcherBackend | None = None

    @property
    def backend_name(self) -> str:
        """Return the active native backend name, or ``unsupported`` before start."""
        return self._backend.name if self._backend is not None else self._backend_type_name()

    @property
    def supported(self) -> bool:
        """Whether this platform has a native backend available."""
        return self._backend_type_name() != "unsupported"

    def start(self) -> None:
        """Start watching all configured roots."""
        if self._backend is not None:
            return

        self._backend = self._build_backend()
        if self._backend is None:
            logger.info("[LibraryWatcher] No native directory watcher for this platform")
            return
        self._backend.start()
        logger.info(
            "[LibraryWatcher] Started",
            backend=self._backend.name,
            roots=self.roots,
        )

    def stop(self) -> None:
        """Stop all native watcher threads and release their handles."""
        if self._backend is None:
            return
        backend, self._backend = self._backend, None
        backend.stop()
        logger.info("[LibraryWatcher] Stopped", backend=backend.name)

    def _backend_type_name(self) -> str:
        if sys.platform == "win32":
            return "ReadDirectoryChangesW"
        if sys.platform.startswith("linux"):
            return "inotify"
        return "unsupported"

    def _build_backend(self) -> _WatcherBackend | None:
        if sys.platform == "win32":
            return _WindowsWatcher(self.roots, self.callback)
        if sys.platform.startswith("linux"):
            return _InotifyWatcher(self.roots, self.callback)
        return None


def _emit(callback: EventCallback, event: FileSystemEvent) -> None:
    try:
        callback(event)
    except Exception:
        logger.exception("[LibraryWatcher] Event callback failed", event=event)


class _InotifyWatcher(_WatcherBackend):
    name = "inotify"

    _IN_ACCESS = 0x00000001
    _IN_MODIFY = 0x00000002
    _IN_ATTRIB = 0x00000004
    _IN_CLOSE_WRITE = 0x00000008
    _IN_MOVED_FROM = 0x00000040
    _IN_MOVED_TO = 0x00000080
    _IN_CREATE = 0x00000100
    _IN_DELETE = 0x00000200
    _IN_DELETE_SELF = 0x00000400
    _IN_MOVE_SELF = 0x00000800
    _IN_ISDIR = 0x40000000
    _IN_IGNORED = 0x00008000
    _IN_Q_OVERFLOW = 0x00004000

    _WATCH_MASK = (
        _IN_CLOSE_WRITE
        | _IN_ATTRIB
        | _IN_MOVED_FROM
        | _IN_MOVED_TO
        | _IN_CREATE
        | _IN_DELETE
        | _IN_DELETE_SELF
        | _IN_MOVE_SELF
    )

    def __init__(self, roots: tuple[Path, ...], callback: EventCallback) -> None:
        self.roots = roots
        self.callback = callback
        self._stop = Event()
        self._thread: Thread | None = None
        self._fd: int | None = None
        self._wd_paths: dict[int, Path] = {}
        self._move_from: dict[int, Path] = {}
        self._libc: Any = None

    def start(self) -> None:
        self._thread = Thread(target=self._run, name="TagStudioInotify", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def _run(self) -> None:
        library_name = ctypes.util.find_library("c")
        self._libc = ctypes.CDLL(library_name or None, use_errno=True)
        init = getattr(self._libc, "inotify_init1", None)
        if init is not None:
            init.argtypes = [ctypes.c_int]
            init.restype = ctypes.c_int
            init_flags = int(getattr(os, "O_NONBLOCK", 0)) | int(
                getattr(os, "O_CLOEXEC", 0)
            )
            fd = int(init(init_flags))
        else:
            init = self._libc.inotify_init
            init.argtypes = []
            init.restype = ctypes.c_int
            fd = int(init())
            if fd >= 0:
                os.set_blocking(fd, False)

        if fd < 0:
            logger.warning(
                "[LibraryWatcher] Could not initialize inotify", errno=ctypes.get_errno()
            )
            return

        self._fd = fd
        try:
            for root in self.roots:
                self._add_tree(root)

            while not self._stop.is_set():
                try:
                    ready, _, _ = select.select([fd], [], [], 0.25)
                except (OSError, ValueError):
                    break
                if not ready:
                    continue
                try:
                    data = os.read(fd, 64 * 1024)
                except BlockingIOError:
                    continue
                except OSError:
                    break
                if data:
                    self._parse_events(data)
        finally:
            self._wd_paths.clear()
            self._move_from.clear()
            self._fd = None
            with suppress(OSError):
                os.close(fd)

    def _add_tree(self, root: Path) -> None:
        if not root.is_dir():
            return
        for directory, directories, _ in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            self._add_watch(directory_path)
            directories[:] = [
                name for name in directories if not (directory_path / name).is_symlink()
            ]

    def _add_watch(self, directory: Path) -> None:
        if self._fd is None or self._libc is None:
            return
        add_watch = self._libc.inotify_add_watch
        add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        add_watch.restype = ctypes.c_int
        watch_descriptor = int(
            add_watch(self._fd, os.fsencode(directory), ctypes.c_uint32(self._WATCH_MASK))
        )
        if watch_descriptor >= 0:
            self._wd_paths[watch_descriptor] = directory

    def _parse_events(self, data: bytes) -> None:
        offset = 0
        header_size = struct.calcsize("iIII")
        while offset + header_size <= len(data):
            watch_descriptor, mask, cookie, name_length = struct.unpack_from(
                "iIII", data, offset
            )
            name_start = offset + header_size
            name_bytes = data[name_start : name_start + name_length].split(b"\0", 1)[0]
            base = self._wd_paths.get(watch_descriptor)
            name = os.fsdecode(name_bytes) if name_bytes else ""
            path = base / name if base is not None and name else base
            if path is not None:
                self._handle_event(path, mask, cookie)

            next_offset = struct.unpack_from("I", data, offset + 0)[0]
            if next_offset == 0:
                break
            offset += next_offset

    def _handle_event(self, path: Path, mask: int, cookie: int) -> None:
        is_directory = bool(mask & self._IN_ISDIR)
        if mask & self._IN_Q_OVERFLOW:
            logger.warning("[LibraryWatcher] inotify queue overflow; a manual refresh is advised")
            return
        if mask & self._IN_IGNORED:
            return

        if is_directory and mask & self._IN_CREATE:
            self._add_tree(path)

        if mask & self._IN_MOVED_FROM:
            self._move_from[cookie] = path
            return
        if mask & self._IN_MOVED_TO:
            old_path = self._move_from.pop(cookie, None)
            if old_path is not None:
                self._rebase_watch_paths(old_path, path, is_directory)
                _emit(
                    self.callback,
                    FileSystemEvent(
                        FileSystemEventKind.MOVED,
                        path,
                        old_path=old_path,
                        is_directory=is_directory,
                    ),
                )
            else:
                _emit(
                    self.callback,
                    FileSystemEvent(FileSystemEventKind.CREATED, path, is_directory=is_directory),
                )
            return

        if mask & self._IN_CREATE:
            _emit(
                self.callback,
                FileSystemEvent(FileSystemEventKind.CREATED, path, is_directory=is_directory),
            )
        elif mask & (self._IN_CLOSE_WRITE | self._IN_ATTRIB):
            _emit(
                self.callback,
                FileSystemEvent(FileSystemEventKind.MODIFIED, path, is_directory=is_directory),
            )
        elif mask & (self._IN_DELETE | self._IN_DELETE_SELF | self._IN_MOVE_SELF):
            _emit(
                self.callback,
                FileSystemEvent(FileSystemEventKind.DELETED, path, is_directory=is_directory),
            )

    def _rebase_watch_paths(self, old_path: Path, new_path: Path, is_directory: bool) -> None:
        if not is_directory:
            return
        for watch_descriptor, directory in tuple(self._wd_paths.items()):
            try:
                relative = directory.relative_to(old_path)
            except ValueError:
                continue
            self._wd_paths[watch_descriptor] = new_path / relative


if sys.platform == "win32":

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("internal", ctypes.c_void_p),
            ("internal_high", ctypes.c_void_p),
            ("offset", ctypes.c_uint32),
            ("offset_high", ctypes.c_uint32),
            ("event", ctypes.c_void_p),
        ]


class _WindowsWatcher(_WatcherBackend):
    name = "ReadDirectoryChangesW"

    _FILE_LIST_DIRECTORY = 0x0001
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _OPEN_EXISTING = 3
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OVERLAPPED = 0x40000000
    _FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
    _FILE_NOTIFY_CHANGE_DIR_NAME = 0x00000002
    _FILE_NOTIFY_CHANGE_LAST_WRITE = 0x00000010
    _FILE_NOTIFY_CHANGE_SIZE = 0x00000008
    _FILE_NOTIFY_CHANGE_CREATION = 0x00000040
    _FILE_ACTION_ADDED = 0x00000001
    _FILE_ACTION_REMOVED = 0x00000002
    _FILE_ACTION_MODIFIED = 0x00000003
    _FILE_ACTION_RENAMED_OLD_NAME = 0x00000004
    _FILE_ACTION_RENAMED_NEW_NAME = 0x00000005
    _WAIT_OBJECT_0 = 0
    _WAIT_TIMEOUT = 258
    _ERROR_IO_PENDING = 997
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    def __init__(self, roots: tuple[Path, ...], callback: EventCallback) -> None:
        self.roots = roots
        self.callback = callback
        self._stop = Event()
        self._threads: list[Thread] = []
        self._handles: dict[int, tuple[Any, Any, Any]] = {}
        self._lock = Lock()

    def start(self) -> None:
        for root in self.roots:
            thread = Thread(
                target=self._run_root,
                args=(root,),
                name=f"TagStudioReadDirectoryChangesW-{root.name}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            active = tuple(self._handles.values())
        for handle, _, overlapped in active:
            _KERNEL32.CancelIoEx(handle, ctypes.byref(overlapped))
        for thread in self._threads:
            thread.join(timeout=3)
        self._threads.clear()

    def _run_root(self, root: Path) -> None:
        if not root.is_dir():
            logger.info("[LibraryWatcher] Root is unavailable", root=root)
            return

        handle = _KERNEL32.CreateFileW(
            str(root),
            self._FILE_LIST_DIRECTORY,
            self._FILE_SHARE_READ | self._FILE_SHARE_WRITE | self._FILE_SHARE_DELETE,
            None,
            self._OPEN_EXISTING,
            self._FILE_FLAG_BACKUP_SEMANTICS | self._FILE_FLAG_OVERLAPPED,
            None,
        )
        if handle in (None, self._INVALID_HANDLE_VALUE):
            logger.warning(
                "[LibraryWatcher] Could not watch root",
                root=root,
                error=ctypes.get_last_error(),
            )
            return

        event_handle = _KERNEL32.CreateEventW(None, 1, 0, None)
        if event_handle in (None, self._INVALID_HANDLE_VALUE):
            _KERNEL32.CloseHandle(handle)
            logger.warning(
                "[LibraryWatcher] Could not create watch event",
                root=root,
                error=ctypes.get_last_error(),
            )
            return

        buffer = ctypes.create_string_buffer(64 * 1024)
        overlapped = _Overlapped()
        overlapped.event = event_handle
        with self._lock:
            self._handles[get_ident()] = (handle, event_handle, overlapped)

        try:
            self._watch_root(root, handle, event_handle, overlapped, buffer)
        finally:
            with self._lock:
                self._handles.pop(get_ident(), None)
            _KERNEL32.CloseHandle(event_handle)
            _KERNEL32.CloseHandle(handle)

    def _watch_root(
        self,
        root: Path,
        handle: Any,
        event_handle: Any,
        overlapped: _Overlapped,
        buffer: Any,
    ) -> None:
        notify_filter = (
            self._FILE_NOTIFY_CHANGE_FILE_NAME
            | self._FILE_NOTIFY_CHANGE_DIR_NAME
            | self._FILE_NOTIFY_CHANGE_LAST_WRITE
            | self._FILE_NOTIFY_CHANGE_SIZE
            | self._FILE_NOTIFY_CHANGE_CREATION
        )
        pending_old_path: Path | None = None

        while not self._stop.is_set():
            _KERNEL32.ResetEvent(event_handle)
            ctypes.memset(ctypes.byref(overlapped), 0, ctypes.sizeof(overlapped))
            overlapped.event = event_handle
            started = _KERNEL32.ReadDirectoryChangesW(
                handle,
                ctypes.byref(buffer),
                ctypes.sizeof(buffer),
                1,
                notify_filter,
                None,
                ctypes.byref(overlapped),
                None,
            )
            if not started:
                error = ctypes.get_last_error()
                if error != self._ERROR_IO_PENDING:
                    logger.warning(
                        "[LibraryWatcher] ReadDirectoryChangesW failed",
                        root=root,
                        error=error,
                    )
                    return

            while not self._stop.is_set():
                wait_result = _KERNEL32.WaitForSingleObject(event_handle, 250)
                if wait_result != self._WAIT_TIMEOUT:
                    break
            if self._stop.is_set():
                _KERNEL32.CancelIoEx(handle, ctypes.byref(overlapped))
                return
            if wait_result != self._WAIT_OBJECT_0:
                continue

            byte_count = ctypes.c_uint32()
            if not _KERNEL32.GetOverlappedResult(
                handle, ctypes.byref(overlapped), ctypes.byref(byte_count), 0
            ):
                if self._stop.is_set():
                    return
                continue

            pending_old_path = self._parse_events(
                root, buffer.raw[: byte_count.value], pending_old_path
            )

    def _parse_events(self, root: Path, data: bytes, pending_old_path: Path | None) -> Path | None:
        offset = 0
        while offset + 12 <= len(data):
            next_offset = int.from_bytes(data[offset : offset + 4], "little")
            action = int.from_bytes(data[offset + 4 : offset + 8], "little")
            name_length = int.from_bytes(data[offset + 8 : offset + 12], "little")
            name = data[offset + 12 : offset + 12 + name_length].decode("utf-16-le")
            path = root / name
            is_directory = path.is_dir()

            if action == self._FILE_ACTION_RENAMED_OLD_NAME:
                pending_old_path = path
            elif action == self._FILE_ACTION_RENAMED_NEW_NAME:
                if pending_old_path is None:
                    _emit(
                        self.callback,
                        FileSystemEvent(
                            FileSystemEventKind.CREATED,
                            path,
                            is_directory=is_directory,
                        ),
                    )
                else:
                    _emit(
                        self.callback,
                        FileSystemEvent(
                            FileSystemEventKind.MOVED,
                            path,
                            old_path=pending_old_path,
                            is_directory=is_directory,
                        ),
                    )
                pending_old_path = None
            else:
                event_kind = {
                    self._FILE_ACTION_ADDED: FileSystemEventKind.CREATED,
                    self._FILE_ACTION_REMOVED: FileSystemEventKind.DELETED,
                    self._FILE_ACTION_MODIFIED: FileSystemEventKind.MODIFIED,
                }.get(action)
                if event_kind is not None:
                    _emit(
                        self.callback,
                        FileSystemEvent(event_kind, path, is_directory=is_directory),
                    )

            if next_offset == 0:
                break
            offset += next_offset
        return pending_old_path


if sys.platform == "win32":
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:
    _KERNEL32 = None
