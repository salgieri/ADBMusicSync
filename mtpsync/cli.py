"""CLI module - command-line interface and orchestration."""

import argparse
import sys
from pathlib import Path

from .scanner import scan_directory, get_scan_summary, FileStatus
from .syncer import MTPSyncer, MTPSyncError
from .utils import setup_logging


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        prog="mtpsync",
        description="Sync music to an MTP device (Android) with on-the-fly format conversion.",
        epilog=(
            "Supported source formats: FLAC, APE, OGG (converted to AAC).\n"
            "Native formats (copied as-is): MP3, AAC, M4A."
        ),
    )
    parser.add_argument(
        "local_music_dir",
        type=Path,
        help="Path to the local music directory to sync.",
    )
    parser.add_argument(
        "--target",
        type=str,
        required=True,
        help="Target directory path on the device (e.g., /sdcard/Music).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview what would happen without transferring any files.",
    )
    parser.add_argument(
        "--bitrate",
        type=str,
        default="320k",
        help='AAC encoding bitrate for converted files (default: "320k").',
    )
    parser.add_argument(
        "--workers", "-j",
        type=int,
        default=0,
        help=(
            "Number of parallel conversion workers "
            "(default: 0 = auto-detect CPU count). "
            "Set to 1 for sequential processing."
        ),
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Enable verbose/debug logging output.",
    )
    return parser


def run(args: list[str] | None = None) -> int:
    """Main CLI entry point.

    Args:
        args: Optional list of command-line arguments (for testing).
              If None, sys.argv[1:] is used.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    parser = build_parser()
    parsed = parser.parse_args(args)

    logger = setup_logging(verbose=parsed.verbose)

    local_root = parsed.local_music_dir.resolve()
    remote_root = parsed.target
    dry_run = parsed.dry_run
    bitrate = parsed.bitrate
    workers = parsed.workers

    # Validate local directory
    if not local_root.is_dir():
        logger.error(f"Local path is not a directory: {local_root}")
        return 1

    logger.info(f"MTPSync v0.1.0")
    logger.info(f"Local source : {local_root}")
    logger.info(f"Remote target: {remote_root}")
    logger.info(f"Bitrate      : {bitrate}")
    logger.info(f"Workers      : {workers if workers else 'auto'}")

    # Phase 1: Scan
    logger.info("Scanning local directory...")
    scan_results = list(scan_directory(local_root))
    summary = get_scan_summary(scan_results)

    logger.info(f"Scan complete:")
    logger.info(f"  Native files   : {summary.get('native', 0)}")
    logger.info(f"  Convertible    : {summary.get('convertible', 0)}")
    logger.info(f"  Skipped        : {summary.get('skipped', 0)}")

    # Filter out skipped files for the sync phase
    syncable = [r for r in scan_results if r.status != FileStatus.SKIPPED]

    if not syncable:
        logger.info("No files to sync.")
        return 0

    # Phase 2: Sync
    if not dry_run:
        # Check for FFmpeg before connecting
        import shutil
        if not shutil.which("ffmpeg"):
            logger.error(
                "FFmpeg is not installed or not found in PATH. "
                "Install it with: brew install ffmpeg"
            )
            return 1

    try:
        with MTPSyncer(
            local_root=local_root,
            remote_root=remote_root,
            dry_run=dry_run,
            bitrate=bitrate,
            workers=workers,
        ) as syncer:
            syncer.sync(syncable)

        return 0

    except MTPSyncError as e:
        logger.error(f"Sync error: {e}")
        return 1
    except KeyboardInterrupt:
        logger.info("\nSync interrupted by user.")
        return 130
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        if parsed.verbose:
            logger.debug("", exc_info=True)
        return 1