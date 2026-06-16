"""Scanner module - scans local directories and classifies audio files."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator

from .utils import is_supported, needs_conversion, setup_logging

logger = setup_logging()


class FileStatus(str, Enum):
    NATIVE = "native"       # Can be copied directly (MP3, AAC)
    CONVERTIBLE = "convertible"  # Needs conversion (FLAC, APE, OGG)
    SKIPPED = "skipped"     # Unsupported or unknown format


@dataclass
class ScanResult:
    """Result of scanning a single file."""
    local_path: Path
    status: FileStatus
    reason: str = ""

    @classmethod
    def from_path(cls, path: Path) -> "ScanResult":
        """Classify a file based on its extension."""
        if not is_supported(path):
            return cls(
                local_path=path,
                status=FileStatus.SKIPPED,
                reason=f"Unsupported format: {path.suffix}",
            )
        if needs_conversion(path):
            return cls(
                local_path=path,
                status=FileStatus.CONVERTIBLE,
            )
        return cls(
            local_path=path,
            status=FileStatus.NATIVE,
        )


def scan_directory(root: Path) -> Iterator[ScanResult]:
    """Recursively scan a directory and yield ScanResults for each file."""
    root = root.resolve()
    if not root.is_dir():
        logger.error(f"Local path does not exist or is not a directory: {root}")
        return

    for filepath in root.rglob("*"):
        if filepath.is_file():
            yield ScanResult.from_path(filepath)


def get_scan_summary(results: list[ScanResult]) -> dict:
    """Return a summary count of file statuses."""
    summary = {s.value: 0 for s in FileStatus}
    for r in results:
        summary[r.status.value] += 1
    return summary