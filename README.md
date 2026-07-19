# mp4-repair

Repair truncated/corrupted MP4 files with missing `moov` atom — typically caused by interrupted recording, power loss, or incomplete file transfer.

## When to use this

Your MP4 file won't play and `ffprobe` shows:

```
[mov,mp4,m4a,3gp,3g2,mj2 @ ...] moov atom not found
```

This means the file's index (`moov` atom) was never written because recording stopped abruptly. The actual video/audio data is still there — this tool reconstructs the index so the file becomes playable again.

## Requirements

- **Python 3.9+** (standard library only)
- **FFmpeg** (for the final remux step)
- A **reference video** shot with the **same camera and settings** as the corrupted file (any length, even 2 seconds works)

## Quick start

```bash
# Extract video + audio, show ffmpeg command
python repair_mp4.py corrupted.MP4 reference.MP4

# Extract and auto-remux in one step
python repair_mp4.py corrupted.MP4 reference.MP4 --remux

# Custom output path
python repair_mp4.py corrupted.MP4 reference.MP4 -o fixed.MP4 --remux

# Video only (skip audio)
python repair_mp4.py corrupted.MP4 reference.MP4 --no-audio --remux
```

## Options

```
usage: repair_mp4.py [-h] [-o OUTPUT] [--video-out VIDEO_OUT]
                     [--audio-out AUDIO_OUT] [--remux] [--no-audio]
                     [--keep-temp]
                     corrupted reference

positional arguments:
  corrupted             Corrupted/truncated MP4 file
  reference             Reference (healthy) MP4 file from same camera

options:
  -o, --output          Output MP4 file (default: repaired.MP4)
  --video-out           Intermediate raw H.264 output (default: extracted.h264)
  --audio-out           Intermediate raw PCM output (default: extracted.pcm)
  --remux               Auto-run ffmpeg to mux video+audio into MP4
  --no-audio            Video-only recovery (skip audio extraction)
  --keep-temp           Keep intermediate .h264 and .pcm files
```

## How it works

1. **Reads the reference file** to extract codec configuration (SPS/PPS) and audio format (sample rate, channels, interleave chunk size).

2. **Scans the corrupted file** for H.264 Access Unit Delimiter (AUD) markers (`00 00 00 02 09`) — each marks the start of a video frame.

3. **Detects chunk boundaries** by finding large gaps (>100KB) between consecutive frames. These gaps contain interleaved audio (PCM) and metadata chunks.

4. **Extracts video NALs** from each frame, converts them from MP4 length-prefix format to Annex B start-code format, and writes to a raw `.h264` file.

5. **Extracts audio samples** from chunk boundary gaps as raw PCM data.

6. **Remuxes with ffmpeg** into a standards-compliant MP4 file.

## Supported formats

Currently tested with **Sony XAVC** footage:
- Video: H.264 High Profile, up to 4K
- Audio: PCM signed 16-bit big-endian (`twos`)

The tool auto-detects audio parameters from the reference file and should work with other cameras that use similar MP4 interleaving patterns.

## Limitations

- Requires a reference video from the **same camera** with the **same settings**
- Only tested with Sony XAVC footage — other camera brands may need adjustments
- Metadata tracks (timecode, GPS, etc.) are not recovered
- The last few frames of audio may be slightly truncated

## License

MIT
