# MTPSync

Sync music to an Android device via ADB with on-the-fly parallel audio format conversion.

## Features

- **One-way sync** from a local directory to an Android device via ADB
- **Parallel conversion pipeline**: FLAC, APE, OGG → AAC (.m4a) using multi-process workers
- **Producer/consumer architecture**: Converts files in parallel while a single consumer thread pushes via ADB concurrently
- **Remote filesystem detection**: Scans the device first to skip files already synced
- **Metadata preservation**: ID3 tags, album art, and other metadata are kept during conversion
- **Special character support**: Folder names with parentheses, spaces, etc. handled correctly
- **Dry-run mode**: Preview what would happen without transferring anything

## Supported Formats

| Source Format | Action      | Target Format |
|--------------|-------------|---------------|
| FLAC         | Converted   | AAC (.m4a)    |
| APE          | Converted   | AAC (.m4a)    |
| OGG / OGA    | Converted   | AAC (.m4a)    |
| MP3          | Copied as-is| MP3           |
| AAC / M4A    | Copied as-is| AAC / M4A     |

## Requirements

- Python 3.12+
- FFmpeg (for audio conversion)
- Android SDK Platform-Tools (ADB)

### macOS Installation

```bash
brew install ffmpeg
# Install Android SDK Command Line Tools from:
# https://developer.android.com/studio/#command-line-tools-only
```

### Linux Installation

```bash
# Debian/Ubuntu
sudo apt install ffmpeg adb

# Fedora
sudo dnf install ffmpeg adb
```

## Setup

```bash
# Create and activate a virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

## Prerequisites

1. **USB Debugging** must be enabled on your Android device (Developer Options → USB debugging)
2. Connect your device via USB
3. Accept the RSA key fingerprint prompt on your phone
4. Verify with: `adb devices` — your device should appear as `device`

## Usage

```bash
python main.py <local_music_dir> --target <remote_target_dir> [options]
```

### Arguments

| Argument            | Description                                          |
|---------------------|------------------------------------------------------|
| `local_music_dir`   | Path to your local music directory (required)        |
| `--target`          | Target directory on the device (required, e.g., `/sdcard/Music`) |
| `--dry-run`         | Preview what would happen without transferring       |
| `--bitrate BITRATE` | AAC encoding bitrate for converted files (default: 320k) |
| `--workers N`, `-j N` | Parallel conversion workers (default: auto-detect CPU count) |
| `--verbose`, `-v`   | Enable verbose/debug logging                         |

### Examples

```bash
# Preview sync (no actual transfer)
python main.py ~/Music --target /sdcard/Music --dry-run

# Full sync with verbose output
python main.py ~/Music --target /sdcard/Music -v

# Sync a single album
python main.py "~/Music/Artist/Album" --target /sdcard/Music

# Sync with 8 parallel conversion workers
python main.py ~/Music --target /sdcard/Music -j 8

# Sync with custom bitrate
python main.py ~/Music --target /sdcard/Music --bitrate 256k
```

## Architecture

```
[Scan local dir] → [Filter out existing files from remote cache]
                      │
                      ├── Native files (MP3/AAC) ──→ [ADB Pusher (single thread)]
                      │
                      └── Convertible files (FLAC/APE/OGG)
                              │
                              ▼
                    [ProcessPoolExecutor (N workers)]
                              │
                              ▼
                    [queue.Queue] ────────────────→ [ADB Pusher (single thread)]
```

- **Remote cache**: Scans the device once at start to detect existing files
- **Producers** (N parallel processes): Convert files concurrently using FFmpeg
- **Consumer** (single thread): Pushes ready files via ADB, cleans up temp files
- Pipeline overlap: While file N is being converted, file N-1 is already being pushed

## Project Structure

```
MTPSync/
├── main.py              # Entry point
├── requirements.txt     # Python dependencies
├── README.md            # This file
└── mtpsync/
    ├── __init__.py      # Package init
    ├── cli.py           # CLI argument parsing & orchestration
    ├── scanner.py       # Local directory scanning & format classification
    ├── converter.py     # Audio conversion via FFmpeg
    ├── syncer.py        # ADB pipeline (producer/consumer)
    └── utils.py         # Helper functions (logging, path mapping, etc.)
```

## Notes

- **Why ADB instead of MTP?** macOS has a built-in kernel driver that claims the Android device's USB interface, which prevents Python MTP libraries from connecting. ADB works reliably across all platforms.
- On re-run, files already on the device are detected and skipped automatically (no wasted conversion or transfer).

## License

MIT