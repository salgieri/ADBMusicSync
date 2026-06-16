"""Syncer module - producer/consumer pipeline for MTP sync via ADB."""

import os
import queue
import shlex
import signal
import subprocess
import tempfile
import threading
from concurrent.futures import ProcessPoolExecutor, Future
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .converter import convert_to_aac, ConversionError
from .scanner import ScanResult, FileStatus
from .utils import (
    setup_logging,
    map_local_to_remote,
    format_bytes,
    get_target_extension,
)

logger = setup_logging()


class MTPSyncError(Exception):
    """Raised when a sync operation fails."""
    pass


def _run_adb(args: list[str], adb_path: str = "adb") -> subprocess.CompletedProcess:
    return subprocess.run(
        [adb_path] + args,
        capture_output=True,
        text=True,
    )


def _run_adb_shell(command: str, adb_path: str = "adb") -> subprocess.CompletedProcess:
    return subprocess.run(
        [adb_path, "shell", command],
        capture_output=True,
        text=True,
    )


# ---- data structures ----

@dataclass
class _ConversionTask:
    """Describes a file that needs converting."""
    local_path: str   # serializable (str, not Path)
    remote_path: str
    bitrate: str
    temp_dir: str


@dataclass
class _ReadyFile:
    """A file ready to be pushed via ADB."""
    local_path: Path      # the actual file on disk (temp or native)
    remote_path: str
    is_temp: bool = False  # whether to delete after push


# ---- worker function (runs in separate processes) ----

def _do_convert(task: _ConversionTask) -> _ReadyFile | None:
    """Convert a single file. Returns _ReadyFile on success, None on failure."""
    input_path = Path(task.local_path)
    suffix = get_target_extension(input_path)
    stem = input_path.stem
    temp_out = Path(task.temp_dir) / f"mtpsync_{stem}_{id(input_path)}{suffix}"

    try:
        convert_to_aac(input_path, temp_out, bitrate=task.bitrate)
        return _ReadyFile(
            local_path=temp_out,
            remote_path=task.remote_path,
            is_temp=True,
        )
    except ConversionError as e:
        logger.error(f"Conversion failed for {input_path.name}: {e}")
        return None
    except Exception as e:
        logger.error(f"Conversion error for {input_path.name}: {e}")
        return None


class MTPSyncer:
    """Sync files to an Android device via ADB using a producer/consumer pipeline."""

    def __init__(
        self,
        local_root: Path,
        remote_root: str,
        dry_run: bool = False,
        bitrate: str = "320k",
        adb_path: str = "adb",
        workers: int = 0,
    ):
        self.local_root = local_root.resolve()
        self.remote_root = remote_root.rstrip("/")
        self.dry_run = dry_run
        self.bitrate = bitrate
        self.adb_path = adb_path
        self.workers = workers or os.cpu_count() or 4

        self._remote_files: Optional[set[str]] = None
        self._temp_dir: Optional[tempfile.TemporaryDirectory] = None

        # Statistics (protected by lock since consumer thread updates them)
        self._lock = threading.Lock()
        self.files_copied = 0
        self.files_converted = 0
        self.files_skipped = 0
        self.files_failed = 0
        self.bytes_transferred = 0

        # Graceful shutdown flag
        self._shutdown = threading.Event()

    # ---- public lifecycle ----

    def connect(self) -> None:
        if self.dry_run:
            logger.info("Dry-run mode - skipping ADB connection check.")
            return

        result = _run_adb(["version"], self.adb_path)
        if result.returncode != 0:
            raise MTPSyncError(
                f"ADB not found at '{self.adb_path}'. "
                "Make sure Android SDK platform-tools is installed."
            )

        result = _run_adb(["devices"], self.adb_path)
        lines = result.stdout.strip().splitlines()
        devices = [l for l in lines[1:] if "\tdevice" in l]
        if not devices:
            raise MTPSyncError(
                "No Android device found via ADB. "
                "Make sure USB debugging is enabled and the device is connected."
            )

        device_id = devices[0].split()[0]
        logger.info(f"ADB device connected: {device_id}")

        result = _run_adb(["shell", "echo", "ok"], self.adb_path)
        if result.returncode != 0:
            raise MTPSyncError(f"Cannot connect to device: {result.stderr.strip()}")

    def disconnect(self) -> None:
        if self._temp_dir:
            self._temp_dir.cleanup()
            self._temp_dir = None
        logger.info("Sync session ended.")

    # ---- remote filesystem helpers ----

    def _cache_remote_files(self) -> set[str]:
        """Scan the device and return a set of relative file paths that exist."""
        if self.dry_run:
            return set()

        result = _run_adb_shell(
            f"find {shlex.quote(self.remote_root)} -type f",
            self.adb_path,
        )
        if result.returncode != 0:
            logger.warning(
                f"Could not list remote files: {result.stderr.strip()}. "
                "All files will be treated as new."
            )
            return set()

        files = set()
        root = self.remote_root.rstrip("/")
        for line in result.stdout.strip().splitlines():
            if line.startswith(root):
                rel = line[len(root):].lstrip("/")
                files.add(rel)

        logger.info(f"Cached {len(files)} remote file(s)")
        return files

    def _remote_file_exists(self, remote_path: str) -> bool:
        """Check whether a specific remote path already exists on the device."""
        if self._remote_files is None:
            self._remote_files = self._cache_remote_files()

        # Compare relative-to-root paths
        root = self.remote_root.rstrip("/")
        rel_path = remote_path[len(root):].lstrip("/")
        return rel_path in (self._remote_files or set())

    def _mkdir_remote(self, remote_dir: str) -> None:
        if self.dry_run:
            logger.debug(f"[DRY RUN] Would create directory: {remote_dir}")
            return

        result = _run_adb_shell(
            f"mkdir -p {shlex.quote(remote_dir)}",
            self.adb_path,
        )
        if result.returncode != 0:
            logger.warning(
                f"Could not create directory {remote_dir}: "
                f"{result.stderr.strip()}"
            )

    def _push_file(self, local_path: Path, remote_path: str) -> None:
        """Push a single file to the device (called by consumer thread)."""
        if self.dry_run:
            logger.info(f"[DRY RUN] Would send: {local_path.name} → {remote_path}")
            with self._lock:
                self.files_copied += 1
            return

        remote_dir = str(Path(remote_path).parent)
        self._mkdir_remote(remote_dir)

        result = _run_adb(
            ["push", str(local_path), remote_path],
            self.adb_path,
        )

        if result.returncode != 0:
            with self._lock:
                self.files_failed += 1
            logger.error(
                f"Failed to push {local_path.name}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
            return

        file_size = local_path.stat().st_size
        with self._lock:
            self.bytes_transferred += file_size
            self.files_copied += 1
        logger.info(
            f"Sent: {local_path.name} "
            f"({format_bytes(file_size)}) → {remote_path}"
        )

    # ---- consumer thread ----

    def _consumer_loop(self, ready_queue: queue.Queue) -> None:
        """Pull ready files from the queue and push them via ADB.

        A sentinel value (None) signals the consumer to stop.
        """
        while True:
            item = ready_queue.get()
            if item is None:
                break
            if item is not None:
                self._push_file(item.local_path, item.remote_path)
                if item.is_temp:
                    try:
                        item.local_path.unlink()
                    except Exception:
                        pass

            # After each item, check if shutdown was requested
            if self._shutdown.is_set():
                logger.info("Consumer stopping after current file — clearing queue...")
                # Drain remaining items from queue and clean up temp files
                while True:
                    try:
                        orphan = ready_queue.get_nowait()
                        if orphan is not None and orphan.is_temp:
                            try:
                                orphan.local_path.unlink()
                            except Exception:
                                pass
                    except queue.Empty:
                        break
                break
    
    # ---- graceful shutdown ----

    # In _handle_shutdown:
    def _handle_shutdown(self, signum, frame):
        """Handle SIGINT/SIGTERM — finish current work then exit."""
        logger.info("\nShutdown requested — finishing current file then exiting...")
        self._shutdown.set()
        # Signal consumer to stop after current item
        try:
            self._ready_queue.put(None)
        except Exception:
            pass

    # ---- main sync pipeline ----

    def sync(self, scan_results: list[ScanResult]) -> dict:
        """Run the full sync using a producer/consumer pipeline."""
        total = len(scan_results)
        logger.info(f"Starting sync of {total} file(s) → {self.remote_root}")
        if self.dry_run:
            logger.info("*** DRY RUN - no files will be transferred ***")

        # --- Phase 1: build task list (filter out existing files) ---
        native_tasks: list[_ReadyFile] = []
        convert_tasks: list[_ConversionTask] = []

        for result in scan_results:
            if self._shutdown.is_set():
                logger.info("Shutdown during planning — aborting.")
                return self.get_summary()

            if result.status == FileStatus.SKIPPED:
                with self._lock:
                    self.files_skipped += 1
                continue

            remote_path = map_local_to_remote(
                result.local_path, self.local_root, self.remote_root
            )

            if self._remote_file_exists(remote_path):
                logger.info(f"Already exists, skipping: {Path(remote_path).name}")
                with self._lock:
                    self.files_skipped += 1
                continue

            if result.status == FileStatus.NATIVE:
                native_tasks.append(
                    _ReadyFile(
                        local_path=result.local_path,
                        remote_path=remote_path,
                        is_temp=False,
                    )
                )
            elif result.status == FileStatus.CONVERTIBLE:
                if self.dry_run:
                    logger.info(
                        f"[DRY RUN] Would convert & send: "
                        f"{result.local_path.name} → {remote_path}"
                    )
                    with self._lock:
                        self.files_converted += 1
                        self.files_copied += 1
                else:
                    convert_tasks.append(
                        _ConversionTask(
                            local_path=str(result.local_path),
                            remote_path=remote_path,
                            bitrate=self.bitrate,
                            temp_dir="",  # filled in below
                        )
                    )

        if not native_tasks and not convert_tasks:
            logger.info("No new files to sync.")
            self._print_summary()
            return self.get_summary()

        # --- Phase 2: pipeline ---
        self._ready_queue: queue.Queue[_ReadyFile | None] = queue.Queue(maxsize=32)
        ready_queue = self._ready_queue
        
        if self.dry_run:
            self._print_summary()
            return self.get_summary()

        # Install graceful shutdown handler
        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        try:
            # Start consumer thread (single ADB pusher)
            consumer = threading.Thread(
                target=self._consumer_loop,
                args=(ready_queue,),
                daemon=True,
            )
            consumer.start()

            # Enqueue native files immediately (no conversion needed)
            for nf in native_tasks:
                if self._shutdown.is_set():
                    break
                ready_queue.put(nf)

            # Start conversion producers
            if convert_tasks:
                logger.info(
                    f"Converting {len(convert_tasks)} file(s) "
                    f"({self.workers} parallel workers)..."
                )

                if self._temp_dir is None:
                    self._temp_dir = tempfile.TemporaryDirectory(prefix="mtpsync_")

                for task in convert_tasks:
                    task.temp_dir = self._temp_dir.name

                def _on_done(fut: Future) -> None:
                    """Callback when a conversion future completes."""
                    try:
                        ready = fut.result()  # _ReadyFile or None
                        if ready:
                            with self._lock:
                                self.files_converted += 1
                            # Don't enqueue if shutdown was requested
                            if not self._shutdown.is_set():
                                ready_queue.put(ready)
                            else:
                                logger.debug(f"Dropping converted file on shutdown: {ready.local_path.name}")
                                try:
                                    ready.local_path.unlink()
                                except Exception:
                                    pass
                        else:
                            with self._lock:
                                self.files_failed += 1
                    except KeyboardInterrupt:
                        pass
                    except Exception:
                        with self._lock:
                            self.files_failed += 1


                pool = ProcessPoolExecutor(max_workers=self.workers)
                try:
                    task_iter = iter(convert_tasks)
                    while True:
                        if self._shutdown.is_set():
                            logger.info("Shutdown requested — stopping new conversions...")
                            break

                        # Submit a batch of up to `workers` tasks
                        batch_futures = []
                        for _ in range(self.workers):
                            task = next(task_iter, None)
                            if task is None:
                                break
                            fut = pool.submit(_do_convert, task)
                            fut.add_done_callback(_on_done)
                            batch_futures.append(fut)

                        if not batch_futures:
                            break

                        # Wait for this batch to complete (poll with shutdown check)
                        for fut in batch_futures:
                            if self._shutdown.is_set():
                                logger.info("Waiting for in-progress conversions to finish...")
                            try:
                                fut.result(timeout=2)
                            except KeyboardInterrupt:
                                pass
                            except Exception:
                                pass
                finally:
                    # Don't wait for pending tasks when shutting down
                    pool.shutdown(wait=not self._shutdown.is_set())    

            # Signal consumer to stop (sentinel)
            if not self._shutdown.is_set():
                ready_queue.put(None)
            consumer.join(timeout=30)

        finally:
            # Restore signal handlers
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

        # --- Summary ---
        self._print_summary()
        return self.get_summary()

    def _print_summary(self) -> None:
        summary = self.get_summary()
        logger.info("=" * 50)
        if self._shutdown.is_set():
            logger.info("Sync interrupted — partial results:")
        else:
            logger.info("Sync complete!")
        logger.info(f"  Copied:    {summary['files_copied']}")
        logger.info(f"  Converted: {summary['files_converted']}")
        logger.info(f"  Skipped:   {summary['files_skipped']}")
        logger.info(f"  Failed:    {summary['files_failed']}")
        logger.info(
            f"  Transferred: {format_bytes(summary['bytes_transferred'])}"
        )
        logger.info("=" * 50)

    def get_summary(self) -> dict:
        with self._lock:
            return {
                "files_copied": self.files_copied,
                "files_converted": self.files_converted,
                "files_skipped": self.files_skipped,
                "files_failed": self.files_failed,
                "bytes_transferred": self.bytes_transferred,
            }

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False
