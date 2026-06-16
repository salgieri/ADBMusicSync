"""Converter module - handles audio format conversion using FFmpeg."""

import os
import tempfile
from pathlib import Path
from typing import Optional

import ffmpeg

from .utils import setup_logging, get_target_extension

logger = setup_logging()


class ConversionError(Exception):
    """Raised when an audio conversion fails."""
    pass


def convert_to_aac(
    input_path: Path,
    output_path: Optional[Path] = None,
    bitrate: str = "320k",
) -> Path:
    """Convert an audio file to AAC (.m4a) format at the specified bitrate.

    Metadata (artist, album, title, cover art, genre, year, track number, etc.)
    is preserved automatically via FFmpeg's metadata pass-through.

    Args:
        input_path:  Path to the source audio file (FLAC, APE, OGG, etc.).
        output_path: Where to write the converted file. If None, a temporary
                     file with the same stem + .m4a is created.
        bitrate:     Target audio bitrate for the AAC encoding (default "320k").

    Returns:
        Path to the converted .m4a file.

    Raises:
        ConversionError: If FFmpeg fails to convert the file.
    """
    if output_path is None:
        suffix = get_target_extension(input_path)
        output_path = input_path.with_suffix(suffix)

    try:
        # probe the input to validate it's a readable media file
        probe = ffmpeg.probe(str(input_path))
        audio_stream = next(
            (s for s in probe["streams"] if s["codec_type"] == "audio"), None
        )
        if audio_stream is None:
            raise ConversionError(
                f"No audio stream found in {input_path}"
            )

        # Build FFmpeg pipeline:
        # -i input          → read source file
        # -c:a aac          → encode audio as AAC
        # -b:a bitrate      → target bitrate (max 320k for AAC)
        # -movflags +faststart → optimize for streaming (optional)
        # -map_metadata 0   → copy all metadata from input
        # -map 0:a          → only map audio streams (skip video/cover art
        #                     which would otherwise be transcoded; cover art
        #                     is still embedded via metadata pass-through)
        # -y                → overwrite output if it exists
        (
            ffmpeg
            .input(str(input_path))
            .output(
                str(output_path),
                acodec="aac",
                **{"b:a": bitrate},
                movflags="+faststart",
                map_metadata="0",
                map="0:a",
            )
            .overwrite_output()
            .run(capture_stdout=True, capture_stderr=True)
        )

        logger.debug(f"Converted: {input_path.name} → {output_path.name}")
        return output_path

    except ffmpeg.Error as e:
        stderr = e.stderr.decode("utf8", errors="replace") if e.stderr else ""
        raise ConversionError(
            f"FFmpeg failed to convert {input_path.name}: {stderr.strip()}"
        )
    except FileNotFoundError:
        raise ConversionError(
            "FFmpeg is not installed or not found in PATH. "
            "Please install FFmpeg (e.g., 'brew install ffmpeg' on macOS)."
        )


def convert_to_temp_aac(input_path: Path, bitrate: str = "320k") -> Path:
    """Convert to AAC and write the result to a temporary file.

    The caller is responsible for deleting the temp file after transfer.
    """
    suffix = get_target_extension(input_path)
    stem = input_path.stem
    _dir = tempfile.gettempdir()
    temp_out = Path(_dir) / f"mtpsync_{os.getpid()}_{stem}_{id(input_path)}{suffix}"
    return convert_to_aac(input_path, temp_out, bitrate=bitrate)