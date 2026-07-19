#!/usr/bin/env python3
"""
MP4 repair tool - Recover truncated/corrupted MP4 files with missing moov atom.

Detects video frames via H.264 AUD NAL markers (00 00 00 02 09).
Detects chunk boundaries via large inter-frame gaps (>100KB).
Extracts PCM audio from chunk boundaries.
Outputs raw .h264 and .pcm files for ffmpeg remuxing.

Usage:
    python repair_mp4.py corrupted.MP4 reference.MP4
    python repair_mp4.py corrupted.MP4 reference.MP4 --remux
    python repair_mp4.py corrupted.MP4 reference.MP4 -o output.MP4 --remux
"""

import argparse
import struct
import sys
import os

START_CODE = bytes([0x00, 0x00, 0x00, 0x01])

# Default audio format (Sony XAVC: PCM s16be, 48000Hz, stereo)
DEFAULT_AUDIO_SAMPLE_RATE = 48000
DEFAULT_AUDIO_CHANNELS = 2
DEFAULT_AUDIO_BITS = 16

# NAL parsing limits
MAX_NAL_SIZE = 15 * 1024 * 1024  # 15MB - generous for 4K H.264 NALs

# Chunk boundary detection
MIN_GAP_THRESHOLD = 100_000  # bytes - gaps larger than this are chunk boundaries


def read_be32(data: bytes, offset: int) -> int:
    """Read a big-endian 32-bit unsigned integer."""
    return struct.unpack('>I', data[offset:offset + 4])[0]


def find_box_positions(filepath: str) -> dict:
    """Parse top-level MP4 box positions and sizes from a file."""
    boxes = {}
    with open(filepath, 'rb') as f:
        f.seek(0, 2)
        file_size = f.tell()
        pos = 0
        while pos < file_size - 8:
            f.seek(pos)
            size_data = f.read(8)
            if len(size_data) < 8:
                break
            box_size = struct.unpack('>I', size_data[0:4])[0]
            box_type = size_data[4:8]
            header_size = 8
            if box_size == 1:  # 64-bit extended size
                f.seek(pos + 8)
                box_size = struct.unpack('>Q', f.read(8))[0]
                header_size = 16
            if box_size == 0:  # box extends to end of file
                box_size = file_size - pos
            boxes[box_type] = (pos, header_size, box_size)
            if pos + box_size <= pos:  # overflow guard
                break
            pos += box_size
    return boxes


def get_avcc_data(moov_data: bytes) -> tuple:
    """Extract SPS/PPS NAL units from avcC box in moov atom.
    Returns (nal_length_size, [sps_nal, pps_nal, ...])."""
    idx = moov_data.find(b'avcC')
    if idx < 0:
        return None, []

    size = read_be32(moov_data, idx - 4)
    avcc_payload = moov_data[idx + 4:idx - 4 + size]
    nal_len_size = (avcc_payload[4] & 0x03) + 1

    sps_pps_nals = []
    pos = 5  # skip configurationVersion + profile + compat + level + lengthSize
    num_sps = avcc_payload[pos] & 0x1f
    pos += 1
    for _ in range(num_sps):
        sps_len = struct.unpack('>H', avcc_payload[pos:pos + 2])[0]
        sps_pps_nals.append(avcc_payload[pos + 2:pos + 2 + sps_len])
        pos += 2 + sps_len

    num_pps = avcc_payload[pos]
    pos += 1
    for _ in range(num_pps):
        pps_len = struct.unpack('>H', avcc_payload[pos:pos + 2])[0]
        sps_pps_nals.append(avcc_payload[pos + 2:pos + 2 + pps_len])
        pos += 2 + pps_len

    return nal_len_size, sps_pps_nals


def detect_audio_format(moov_data: bytes) -> dict:
    """Detect audio format from reference file's moov atom.
    Returns dict with sample_rate, channels, bits_per_sample, chunk_size."""
    info = {
        'sample_rate': DEFAULT_AUDIO_SAMPLE_RATE,
        'channels': DEFAULT_AUDIO_CHANNELS,
        'bits': DEFAULT_AUDIO_BITS,
        'chunk_size': 96096,  # fallback: bytes of audio per interleave chunk
    }

    # Find audio track's mdhd to get sample rate (more reliable than twos entry)
    # Audio is typically track 2 (second mdhd in moov)
    mdhd_positions = []
    pos = 0
    while True:
        idx = moov_data.find(b'mdhd', pos)
        if idx < 0:
            break
        mdhd_positions.append(idx)
        pos = idx + 4

    if len(mdhd_positions) >= 2:
        # Second mdhd = audio track
        mdhd = moov_data[mdhd_positions[1]:]
        ver = mdhd[4]  # version byte (first byte after 'mdhd')
        if ver == 1:
            timescale = read_be32(mdhd, 24)  # version 1: timescale at offset 20 from data start
        else:
            timescale = read_be32(mdhd, 16)  # version 0: timescale at offset 12 from data start
        if timescale > 0:
            info['sample_rate'] = timescale

    # Detect audio chunk size from reference track tables
    # Strategy: find all stsz boxes, use the second one (audio track)
    stsz_positions = []
    pos = 0
    while True:
        idx = moov_data.find(b'stsz', pos)
        if idx < 0:
            break
        stsz_positions.append(idx)
        pos = idx + 4

    if len(stsz_positions) >= 2:
        # Second stsz should be the audio track
        audio_stsz = stsz_positions[1]
        sample_count = read_be32(moov_data, audio_stsz + 12)
        uniform_size = read_be32(moov_data, audio_stsz + 8)

        if uniform_size > 0 and sample_count > 0:
            # Find corresponding stco (chunk offset table) in the same track
            stco_idx = moov_data.find(b'stco', audio_stsz)
            if stco_idx < 0:
                stco_idx = moov_data.find(b'co64', audio_stsz)
            if stco_idx >= 0:
                entry_count = read_be32(moov_data, stco_idx + 12)
                if entry_count > 0:
                    samples_per_chunk = sample_count // entry_count
                    detected_size = samples_per_chunk * uniform_size
                    if detected_size > 0:
                        info['chunk_size'] = detected_size

    return info


def parse_frame_nals(mdat: bytes, aud_off: int, frame_end: int) -> tuple:
    """Parse all NAL units within a single video frame.
    Frame spans from aud_off (AUD NAL start) to frame_end (next AUD start).
    Returns (nals_list, last_byte_offset)."""
    nals = []
    pos = aud_off
    last_end = aud_off

    while pos < frame_end - 4:
        nal_len = read_be32(mdat, pos)
        if nal_len <= 0 or nal_len > MAX_NAL_SIZE:
            break
        nal_start = pos + 4
        if nal_start + nal_len > frame_end:
            break
        nal_byte = mdat[nal_start]
        if (nal_byte >> 7) & 1:  # forbidden_zero_bit must be 0
            break
        nal_type = nal_byte & 0x1f
        if nal_type == 0 or nal_type > 31:
            break

        nals.append((nal_start, nal_len, nal_type))
        last_end = nal_start + nal_len
        pos = last_end

    return nals, last_end


def find_aud_positions(data: bytes, max_gap: int = 500_000_000) -> list:
    """Find all AUD NAL positions in mdat data.
    AUD pattern: 00 00 00 02 09 (NAL length=2, type=9=Access Unit Delimiter)."""
    aud_pattern = bytes([0x00, 0x00, 0x00, 0x02, 0x09])
    positions = []
    pos = 0
    while True:
        idx = data.find(aud_pattern, pos)
        if idx < 0:
            break
        positions.append(idx)
        pos = idx + 1
    return positions


def run_ffmpeg(video_file: str, audio_file: str, output_file: str,
               sample_rate: int, channels: int) -> bool:
    """Run ffmpeg to mux video and audio into an MP4 file."""
    import subprocess
    cmd = [
        'ffmpeg', '-y',
        '-f', 'h264', '-r', '24000/1001', '-i', video_file,
        '-f', 's16be', '-ar', str(sample_rate), '-ac', str(channels),
        '-i', audio_file,
        '-c', 'copy', '-map', '0:v', '-map', '1:a',
        output_file,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            print(f"ffmpeg error:\n{result.stderr[-500:]}", file=sys.stderr)
            return False
        return True
    except FileNotFoundError:
        print("Error: ffmpeg not found. Install ffmpeg or run the command manually.",
              file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print("Error: ffmpeg timed out.", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(
        description='Repair truncated MP4 files with missing moov atom.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''Examples:
  python repair_mp4.py corrupted.MP4 reference.MP4
  python repair_mp4.py corrupted.MP4 reference.MP4 -o fixed.MP4 --remux
  python repair_mp4.py corrupted.MP4 reference.MP4 --no-audio
        ''',
    )
    parser.add_argument('corrupted', help='Corrupted/truncated MP4 file')
    parser.add_argument('reference', help='Reference (healthy) MP4 file from same camera')
    parser.add_argument('-o', '--output', default='repaired.MP4',
                        help='Output MP4 file (default: repaired.MP4)')
    parser.add_argument('--video-out', default='extracted.h264',
                        help='Intermediate raw H.264 output (default: extracted.h264)')
    parser.add_argument('--audio-out', default='extracted.pcm',
                        help='Intermediate raw PCM output (default: extracted.pcm)')
    parser.add_argument('--remux', action='store_true',
                        help='Automatically run ffmpeg to mux video+audio into MP4')
    parser.add_argument('--no-audio', action='store_true',
                        help='Skip audio extraction (video only)')
    parser.add_argument('--keep-temp', action='store_true',
                        help='Keep intermediate .h264 and .pcm files')
    args = parser.parse_args()

    # Validate input files
    for fpath, label in [(args.corrupted, 'Corrupted'), (args.reference, 'Reference')]:
        if not os.path.exists(fpath):
            print(f"Error: {label} file not found: {fpath}", file=sys.stderr)
            sys.exit(1)

    corrupt_file = args.corrupted
    ref_file = args.reference
    video_out = args.video_out
    audio_out = args.audio_out
    mp4_out = args.output

    print("=" * 60)
    print("MP4 Repair Tool")
    print("=" * 60)

    # Step 1: Get codec and audio config from reference
    print(f"\n[1/5] Reading codec config from: {ref_file}")
    try:
        ref_boxes = find_box_positions(ref_file)
    except Exception as e:
        print(f"Error reading reference file: {e}", file=sys.stderr)
        sys.exit(1)

    if b'moov' not in ref_boxes:
        print("Error: No moov atom found in reference file! Is it a valid MP4?",
              file=sys.stderr)
        sys.exit(1)

    with open(ref_file, 'rb') as f:
        f.seek(ref_boxes[b'moov'][0])
        ref_moov = f.read(ref_boxes[b'moov'][2])

    nal_len_size, sps_pps = get_avcc_data(ref_moov)
    if nal_len_size is None:
        print("Error: avcC box not found in reference moov!", file=sys.stderr)
        sys.exit(1)

    print(f"  NAL length size: {nal_len_size}")
    print(f"  SPS/PPS NALs: {len(sps_pps)}")

    audio_info = detect_audio_format(ref_moov)
    audio_sample_rate = audio_info['sample_rate']
    audio_channels = audio_info['channels']
    audio_bits = audio_info['bits']
    audio_bytes_per_sample = audio_channels * (audio_bits // 8)
    audio_chunk_size = audio_info['chunk_size']
    print(f"  Audio: {audio_channels}ch, {audio_sample_rate}Hz, "
          f"{audio_bits}-bit, chunk={audio_chunk_size} bytes")

    # Step 2: Read corrupted file's mdat
    print(f"\n[2/5] Reading corrupted file: {corrupt_file}")
    try:
        corrupt_boxes = find_box_positions(corrupt_file)
    except Exception as e:
        print(f"Error reading corrupted file: {e}", file=sys.stderr)
        sys.exit(1)

    if b'mdat' not in corrupt_boxes:
        print("Error: No mdat atom found in corrupted file!", file=sys.stderr)
        sys.exit(1)

    mdat_off, mdat_hdr_sz, _ = corrupt_boxes[b'mdat']
    mdat_content_start = mdat_off + mdat_hdr_sz

    with open(corrupt_file, 'rb') as f:
        f.seek(mdat_content_start)
        mdat = f.read()

    print(f"  mdat size: {len(mdat):,} bytes ({len(mdat) / 1024 / 1024:.1f} MB)")

    # Step 3: Find frames and chunk boundaries
    print("\n[3/5] Finding frames and chunk boundaries...")

    aud_positions = find_aud_positions(mdat)
    print(f"  AUD markers: {len(aud_positions)}")

    if not aud_positions:
        print("Error: No AUD markers found! File may not contain H.264 video.",
              file=sys.stderr)
        sys.exit(1)

    frame_info = []
    for i, aud_off in enumerate(aud_positions):
        frame_end = aud_positions[i + 1] if i + 1 < len(aud_positions) else len(mdat)
        nals, last_end = parse_frame_nals(mdat, aud_off, frame_end)
        frame_info.append((aud_off, nals, last_end))

    # Detect chunk boundaries (large gaps between frames indicate interleaved audio/data)
    chunk_boundaries = set()
    for i in range(len(frame_info) - 1):
        gap = aud_positions[i + 1] - frame_info[i][2]
        if gap > MIN_GAP_THRESHOLD:
            chunk_boundaries.add(i)

    # Also auto-detect audio chunk size from the first gap
    if chunk_boundaries and audio_info['chunk_size'] == 96096:
        first_boundary = min(chunk_boundaries)
        first_gap = aud_positions[first_boundary + 1] - frame_info[first_boundary][2]
        # Gap = audio_chunk + data_chunk(61440). Extract audio chunk size.
        detected_audio_chunk = first_gap - 61440
        if detected_audio_chunk > 0:
            audio_chunk_size = detected_audio_chunk
            print(f"  Auto-detected audio chunk size: {audio_chunk_size} bytes")

    print(f"  Frames: {len(frame_info)}")
    print(f"  Chunk boundaries: {len(chunk_boundaries)}")

    # Step 4: Extract video and audio
    print("\n[4/5] Extracting video and audio...")

    total_video_nals = len(sps_pps)
    frames_written = 0
    audio_segments = []
    total_audio_bytes = 0

    with open(video_out, 'wb') as video_out_f:
        # Write SPS/PPS at the beginning
        for nal in sps_pps:
            video_out_f.write(START_CODE)
            video_out_f.write(nal)

        for i, (aud_off, nals, last_end) in enumerate(frame_info):
            # Write video NALs for this frame
            frame_nal_count = 0
            for nal_start, nal_len, _ in nals:
                video_out_f.write(START_CODE)
                video_out_f.write(mdat[nal_start:nal_start + nal_len])
                frame_nal_count += 1

            if frame_nal_count > 0:
                frames_written += 1
            total_video_nals += frame_nal_count

            # Extract audio at chunk boundaries
            if not args.no_audio and i in chunk_boundaries:
                audio_start = last_end
                audio_end = min(audio_start + audio_chunk_size, len(mdat))

                if audio_end > audio_start:
                    audio_data = mdat[audio_start:audio_end]
                    audio_segments.append(audio_data)
                    total_audio_bytes += len(audio_data)

        # Handle trailing audio (partial chunk at end of file)
        if not args.no_audio and frame_info:
            last_frame_end = frame_info[-1][2]
            remaining = len(mdat) - last_frame_end
            if remaining > 100:
                partial_size = min(remaining, audio_chunk_size)
                audio_data = mdat[last_frame_end:last_frame_end + partial_size]
                audio_segments.append(audio_data)
                total_audio_bytes += len(audio_data)
                print(f"  Trailing audio: {len(audio_data)} bytes")

    # Write audio file
    with open(audio_out, 'wb') as audio_out_f:
        for seg in audio_segments:
            audio_out_f.write(seg)

    video_size = os.path.getsize(video_out)
    audio_size = os.path.getsize(audio_out) if not args.no_audio else 0
    audio_duration = (total_audio_bytes / (audio_sample_rate * audio_bytes_per_sample)
                      if audio_bytes_per_sample > 0 else 0)

    print(f"  Video: {video_out} ({video_size:,} bytes, "
          f"{total_video_nals} NALs, {frames_written} frames)")
    print(f"  Audio: {audio_out} ({audio_size:,} bytes, "
          f"{len(audio_segments)} segments, ~{audio_duration:.1f}s)")

    # Step 5: Remux or show commands
    print(f"\n[5/5] Output")
    if args.remux:
        print(f"  Remuxing to {mp4_out}...")
        success = run_ffmpeg(video_out, audio_out, mp4_out,
                             audio_sample_rate, audio_channels)
        if success:
            out_size = os.path.getsize(mp4_out)
            print(f"  Done: {mp4_out} ({out_size:,} bytes)")
            if not args.keep_temp:
                for tmp in [video_out, audio_out]:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                        print(f"  Removed temp file: {tmp}")
    else:
        print(f"  To remux, run:")
        if args.no_audio:
            print(f"  ffmpeg -y -f h264 -r 24000/1001 -i {video_out} -c copy {mp4_out}")
        else:
            print(f"  ffmpeg -y -f h264 -r 24000/1001 -i {video_out} \\")
            print(f"         -f s16be -ar {audio_sample_rate} -ac {audio_channels} "
                  f"-i {audio_out} \\")
            print(f"         -c copy -map 0:v -map 1:a {mp4_out}")
        if not args.keep_temp:
            print(f"  After remux, clean up with: rm {video_out} {audio_out}")

    print(f"\n{'=' * 60}")
    print(f"Extraction complete.")
    print(f"Video: {video_out} ({video_size / 1024 / 1024:.1f} MB)")
    if not args.no_audio:
        print(f"Audio: {audio_out} ({audio_size / 1024 / 1024:.1f} MB)")
    print(f"{'=' * 60}")

    # Quick sanity check
    if frames_written == 0:
        print("WARNING: No valid video frames extracted!", file=sys.stderr)
        sys.exit(1)
    if not args.no_audio and len(audio_segments) == 0:
        print("WARNING: No audio segments extracted!", file=sys.stderr)


if __name__ == '__main__':
    main()
