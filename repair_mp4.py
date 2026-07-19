#!/usr/bin/env python3
"""
MP4 repair tool v7 - Extract raw H.264 video + PCM audio from truncated MP4.

Detects video frames via AUD NAL markers (00 00 00 02 09).
Detects chunk boundaries via large inter-frame gaps (>100KB).
Extracts PCM audio (s16be, 48000Hz, stereo) from chunk boundaries.
Outputs raw .h264 and .pcm files for ffmpeg remuxing.
"""
import struct
import sys
import os
import subprocess

REF_FILE = "ref.MP4"
CORRUPT_FILE = "6ca87107bf38c60c9770b742dfd953f9.MP4"
RAW_VIDEO_OUT = "extracted.h264"
RAW_AUDIO_OUT = "extracted.pcm"
MP4_OUTPUT = "repaired.MP4"

START_CODE = bytes([0x00, 0x00, 0x00, 0x01])

# Audio format (from reference file analysis)
AUDIO_SAMPLE_RATE = 48000
AUDIO_CHANNELS = 2
AUDIO_BITS = 16
AUDIO_BYTES_PER_SAMPLE = AUDIO_CHANNELS * (AUDIO_BITS // 8)  # 4 bytes per sample
AUDIO_CHUNK_SIZE = 96096  # bytes of audio per chunk (from reference)
DATA_CHUNK_SIZE = 61440   # bytes of rtmd data per chunk
CHUNK_GAP_SIZE = AUDIO_CHUNK_SIZE + DATA_CHUNK_SIZE  # 157536
FRAMES_PER_CHUNK = 12     # frames per video chunk (from reference)
MIN_GAP_THRESHOLD = 100000  # threshold to detect chunk boundaries


def read_be32(data, offset):
    return struct.unpack('>I', data[offset:offset+4])[0]


def find_box_positions(filepath):
    """Get top-level MP4 box positions."""
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
            if box_size == 1:
                f.seek(pos + 8)
                box_size = struct.unpack('>Q', f.read(8))[0]
                header_size = 16
            if box_size == 0:
                box_size = file_size - pos
            boxes[box_type] = (pos, header_size, box_size)
            if pos + box_size <= pos:
                break
            pos += box_size
    return boxes


def get_avcc_data(moov_data):
    """Extract SPS/PPS NALs from avcC box."""
    idx = moov_data.find(b'avcC')
    if idx < 0:
        return None, []

    size = read_be32(moov_data, idx - 4)
    avcc = moov_data[idx + 4:idx - 4 + size]
    nal_len_size = (avcc[4] & 0x03) + 1

    sps_pps_nals = []
    pos = 5
    num_sps = avcc[pos] & 0x1f
    pos += 1
    for _ in range(num_sps):
        sps_len = struct.unpack('>H', avcc[pos:pos+2])[0]
        sps_pps_nals.append(avcc[pos+2:pos+2+sps_len])
        pos += 2 + sps_len

    num_pps = avcc[pos]
    pos += 1
    for _ in range(num_pps):
        pps_len = struct.unpack('>H', avcc[pos:pos+2])[0]
        sps_pps_nals.append(avcc[pos+2:pos+2+pps_len])
        pos += 2 + pps_len

    return nal_len_size, sps_pps_nals


def parse_frame_nals(mdat, aud_off, frame_end):
    """Parse all NAL units within a single frame [aud_off, frame_end).
    Returns list of (nal_start, nal_len, nal_type) and the end of the last NAL."""
    nals = []
    pos = aud_off
    last_end = aud_off

    while pos < frame_end - 4:
        nal_len = read_be32(mdat, pos)
        if nal_len <= 0 or nal_len > 15 * 1024 * 1024:
            break
        nal_start = pos + 4
        if nal_start + nal_len > frame_end:
            break
        nal_byte = mdat[nal_start]
        if (nal_byte >> 7) & 1:  # forbidden_zero_bit
            break
        nal_type = nal_byte & 0x1f
        if nal_type == 0 or nal_type > 31:
            break

        nals.append((nal_start, nal_len, nal_type))
        last_end = nal_start + nal_len
        pos = last_end

    return nals, last_end


def main():
    print("=" * 60)
    print("MP4 Repair Tool v7 - Video + Audio extraction")
    print("=" * 60)

    # Step 1: Get codec config from reference
    print("\n[1/5] Reading codec config from reference...")
    ref_boxes = find_box_positions(REF_FILE)
    with open(REF_FILE, 'rb') as f:
        f.seek(ref_boxes[b'moov'][0])
        ref_moov = f.read(ref_boxes[b'moov'][2])
    nal_len_size, sps_pps = get_avcc_data(ref_moov)
    print(f"  NAL length size: {nal_len_size}")
    print(f"  SPS/PPS NALs: {len(sps_pps)}")

    # Step 2: Read corrupted mdat and find AUD markers
    print("\n[2/5] Finding frame markers in corrupted file...")
    corrupt_boxes = find_box_positions(CORRUPT_FILE)
    mdat_off, mdat_hdr_sz, _ = corrupt_boxes[b'mdat']

    with open(CORRUPT_FILE, 'rb') as f:
        f.seek(mdat_off + mdat_hdr_sz)
        mdat = f.read()

    print(f"  mdat size: {len(mdat):,} bytes")

    # Find AUD positions (frame starts)
    aud_pattern = bytes([0x00, 0x00, 0x00, 0x02, 0x09])
    aud_positions = []
    pos = 0
    while True:
        idx = mdat.find(aud_pattern, pos)
        if idx < 0:
            break
        aud_positions.append(idx)
        pos = idx + 1

    print(f"  AUD markers (frames): {len(aud_positions)}")

    # Step 3: Parse all frames and find chunk boundaries
    print("\n[3/5] Parsing frames and detecting chunk boundaries...")

    frame_info = []  # (aud_off, nals, last_nal_end)
    chunk_boundaries = []  # indices after which chunk boundaries occur

    for i, aud_off in enumerate(aud_positions):
        if i + 1 < len(aud_positions):
            frame_end = aud_positions[i + 1]
        else:
            frame_end = len(mdat)

        nals, last_end = parse_frame_nals(mdat, aud_off, frame_end)
        frame_info.append((aud_off, nals, last_end))

    # Detect chunk boundaries: large gaps between last_nal_end and next aud
    for i in range(len(frame_info) - 1):
        gap = aud_positions[i + 1] - frame_info[i][2]
        if gap > MIN_GAP_THRESHOLD:
            chunk_boundaries.append(i)

    print(f"  Frames parsed: {len(frame_info)}")
    print(f"  Chunk boundaries detected: {len(chunk_boundaries)}")

    # Step 4: Write video and extract audio
    print("\n[4/5] Writing video and extracting audio...")

    total_video_nals = len(sps_pps)
    frames_written = 0
    total_audio_bytes = 0
    audio_segments = []

    with open(RAW_VIDEO_OUT, 'wb') as video_out:
        # Write SPS/PPS first
        for nal in sps_pps:
            video_out.write(START_CODE)
            video_out.write(nal)

        for i, (aud_off, nals, last_end) in enumerate(frame_info):
            # Write video NALs for this frame
            frame_nal_count = 0
            for nal_start, nal_len, _ in nals:
                video_out.write(START_CODE)
                video_out.write(mdat[nal_start:nal_start + nal_len])
                frame_nal_count += 1

            if frame_nal_count > 0:
                frames_written += 1
            total_video_nals += frame_nal_count

            # If this frame is followed by a chunk boundary, extract audio
            if i in chunk_boundaries:
                # Audio data is at the beginning of the gap
                audio_start = last_end
                audio_end = audio_start + AUDIO_CHUNK_SIZE

                if audio_end <= len(mdat):
                    audio_data = mdat[audio_start:audio_end]
                    audio_segments.append(audio_data)
                    total_audio_bytes += len(audio_data)

        # Handle partial audio at end of file (after last frame)
        if frame_info:
            last_frame_end = frame_info[-1][2]
            remaining = len(mdat) - last_frame_end
            if remaining > 0:
                # Check if there's partial audio
                partial_audio = min(remaining, AUDIO_CHUNK_SIZE)
                audio_data = mdat[last_frame_end:last_frame_end + partial_audio]
                if len(audio_data) > 100:  # At least some meaningful audio
                    audio_segments.append(audio_data)
                    total_audio_bytes += len(audio_data)
                    print(f"  Partial audio at end: {len(audio_data)} bytes")

    # Write audio file
    with open(RAW_AUDIO_OUT, 'wb') as audio_out:
        for seg in audio_segments:
            audio_out.write(seg)

    audio_duration = total_audio_bytes / (AUDIO_SAMPLE_RATE * AUDIO_BYTES_PER_SAMPLE)

    video_size = os.path.getsize(RAW_VIDEO_OUT)
    audio_size = os.path.getsize(RAW_AUDIO_OUT)
    print(f"  Video: {RAW_VIDEO_OUT} ({video_size:,} bytes, {total_video_nals} NALs, {frames_written} frames)")
    print(f"  Audio: {RAW_AUDIO_OUT} ({audio_size:,} bytes, {len(audio_segments)} segments, ~{audio_duration:.1f}s)")

    # Step 5: Show ffmpeg commands
    print(f"\n[5/5] Remux with ffmpeg:")
    print(f"  ffmpeg -y -f h264 -r 24000/1001 -i {RAW_VIDEO_OUT} \\")
    print(f"         -f s16be -ar {AUDIO_SAMPLE_RATE} -ac {AUDIO_CHANNELS} -i {RAW_AUDIO_OUT} \\")
    print(f"         -c copy -map 0:v -map 1:a {MP4_OUTPUT}")
    print(f"\n{'=' * 60}")
    print(f"Extraction complete.")
    print(f"Video: {RAW_VIDEO_OUT} ({video_size/1024/1024:.1f} MB)")
    print(f"Audio: {RAW_AUDIO_OUT} ({audio_size/1024/1024:.1f} MB)")
    print(f"{'=' * 60}")


if __name__ == '__main__':
    main()
