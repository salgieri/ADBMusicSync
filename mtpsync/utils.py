"""Utility functions for MTPSync."""

import hashlib
import logging
import os
from pathlib import Path
from typing import Set

# Supported source formats (lowercase extensions)
CONVERTIBLE_FORMATS: Set[str] = {".flac", ".ape", ".ogg", ".oga"}
NATIVE_FORMATS: Set[str] = {".mp3", ".m4a", ".aac"}
SUPPORTED_FORMATS: Set[str] = CONVERTIBLE_FORMATS | NATIVE_FORMATS

# Logging setup
def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure and return the main logger."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    return logging.getLogger("mtpsync")


def compute_file_hash(filepath: Path, algorithm: str = "md5") -> str:
    """Compute a hash of a file's contents."""
    h = hashlib.new(algorithm)
    with open(filepath, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


def get_file_extension(path: Path) -> str:
    """Return the lowercase file extension."""
    return path.suffix.lower()


def is_supported(path: Path) -> bool:
    """Check if a file has a supported audio extension."""
    return get_file_extension(path) in SUPPORTED_FORMATS


def needs_conversion(path: Path) -> bool:
    """Check if a file needs to be converted before syncing."""
    return get_file_extension(path) in CONVERTIBLE_FORMATS


def get_target_extension(source_path: Path) -> str:
    """Return the target file extension after conversion.

    Native formats keep their extension.
    Convertible formats become .m4a (AAC).
    """
    ext = get_file_extension(source_path)
    if ext in CONVERTIBLE_FORMATS:
        return ".m4a"
    return ext


def _sanitize_name(name: str) -> str:
    """Replace characters that are invalid or problematic on Android filesystems."""
    bad_chars = ['>', '<', ':', '|', '"', '?']
    for ch in bad_chars:
        name = name.replace(ch, '-')
    return name


def map_local_to_remote(
    local_path: Path, local_root: Path, remote_root: str
) -> str:
    """Map a local file path to its corresponding remote MTP path.

    Filenames are sanitized to remove characters invalid on Android filesystems
    (>, <, :, |, ", ?) so both the existence check and push use the same path.

    For example:
      local_path  = /music/Artist/Album/song.flac
      local_root  = /music/Artist/Album/
      remote_root = /Music
      -> /Music/song.m4a
    """
    relative = local_path.relative_to(local_root)

    # Build sanitized path parts
    parts = []
    for part in relative.parts[:-1]:  # directory components
        parts.append(_sanitize_name(part))
    # File component: sanitize name, apply target extension
    filename = _sanitize_name(relative.name)
    stem = Path(filename).stem
    suffix = get_target_extension(local_path)
    parts.append(stem + suffix)

    return str(Path(remote_root) / Path(*parts))


def format_bytes(size: int) -> str:
    """Format a byte count into a human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"