#!/usr/bin/env python3
"""
MP4 repair tool v6 - Extract raw H.264 Annex B stream.

Converts each frame (between AUD markers) to Annex B format
(start codes instead of NAL length prefixes), then lets ffmpeg
remux into a proper MP4.
"""
import struct
import sys
import os
import subprocess

REF_FILE = "ref.MP4"
CORRUPT_FILE = "6ca87107bf38c60c9770b742dfd953f9.MP4"
RAW_OUTPUT = "extracted.h264"
MP4_OUTPUT = "repaired.MP4"

START_CODE = bytes([0x00, 0x00, 0x00, 0x01])

def read_be32(data, offset):
    return struct.unpack('>I', data[offset:offset+4])[0]


def find_box_positions(filepath):
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
    """Extract SPS/PPS from avcC for writing as separate NALs at stream start."""
    idx = moov_data.find(b'avcC')
    if idx < 0:
        return None, []

    size = read_be32(moov_data, idx - 4)
    # avcC box payload starts after 'avcC' (4 bytes) = idx + 4
    avcc = moov_data[idx + 4:idx - 4 + size]
    nal_len_size = (avcc[4] & 0x03) + 1

    sps_pps_nals = []
    pos = 5  # skip confVersion(1)+profile(1)+compat(1)+level(1)+lengthSize(1)
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


def main():
    print("=" * 60)
    print("MP4 Repair Tool v6 - Raw H.264 extraction")
    print("=" * 60)

    # Step 1: Get codec config (SPS/PPS)
    print("\n[1/4] Getting codec config from reference...")
    ref_boxes = find_box_positions(REF_FILE)
    with open(REF_FILE, 'rb') as f:
        f.seek(ref_boxes[b'moov'][0])
        ref_moov = f.read(ref_boxes[b'moov'][2])
    nal_len_size, sps_pps = get_avcc_data(ref_moov)
    print(f"  NAL length size: {nal_len_size}")
    print(f"  SPS/PPS NALs: {len(sps_pps)}")

    # Step 2: Read corrupted mdat and find AUDs
    print("\n[2/4] Finding frames via AUD markers...")
    corrupt_boxes = find_box_positions(CORRUPT_FILE)
    mdat_off, mdat_hdr_sz, _ = corrupt_boxes[b'mdat']

    with open(CORRUPT_FILE, 'rb') as f:
        f.seek(mdat_off + mdat_hdr_sz)
        mdat = f.read()

    print(f"  mdat: {len(mdat):,} bytes")

    # Find AUD positions
    aud_pattern = bytes([0x00, 0x00, 0x00, 0x02, 0x09])
    aud_positions = []
    pos = 0
    while True:
        idx = mdat.find(aud_pattern, pos)
        if idx < 0:
            break
        aud_positions.append(idx)
        pos = idx + 1

    print(f"  AUD markers: {len(aud_positions)}")

    # Step 3: Extract frames as raw H.264
    print(f"\n[3/4] Extracting {len(aud_positions)} frames as raw H.264...")

    with open(RAW_OUTPUT, 'wb') as out:
        # Write SPS and PPS first (with start codes)
        for nal in sps_pps:
            out.write(START_CODE)
            out.write(nal)

        total_nals = len(sps_pps)
        frames_written = 0

        for i, aud_off in enumerate(aud_positions):
            if i + 1 < len(aud_positions):
                frame_end = aud_positions[i + 1]
            else:
                frame_end = len(mdat)

            # Parse NALs within this frame [aud_off, frame_end)
            pos = aud_off
            frame_nals = 0

            while pos < frame_end - 4:
                nal_len = read_be32(mdat, pos)
                if nal_len <= 0 or nal_len > 15 * 1024 * 1024:
                    break
                nal_start = pos + 4
                if nal_start + nal_len > frame_end:
                    break
                nal_byte = mdat[nal_start]
                if (nal_byte >> 7) & 1:
                    break
                nal_type = nal_byte & 0x1f
                if nal_type == 0 or nal_type > 31:
                    break

                # Write start code + NAL data
                out.write(START_CODE)
                out.write(mdat[nal_start:nal_start + nal_len])
                frame_nals += 1
                pos = nal_start + nal_len

            if frame_nals > 0:
                frames_written += 1
            total_nals += frame_nals

    raw_size = os.path.getsize(RAW_OUTPUT)
    print(f"  Written: {RAW_OUTPUT}")
    print(f"  Size: {raw_size:,} bytes ({raw_size/1024/1024:.1f} MB)")
    print(f"  Total NALs: {total_nals}")
    print(f"  Frames with NALs: {frames_written}/{len(aud_positions)}")

    # Step 4: Remux with ffmpeg (run this command manually)
    print(f"\n[4/4] To remux to MP4, run:")
    print(f"  ffmpeg -y -f h264 -r 24000/1001 -i {RAW_OUTPUT} -c copy {MP4_OUTPUT}")
    print(f"  Then verify with: ffprobe {MP4_OUTPUT}")
    print(f"\n{'=' * 60}")
    print(f"Raw H.264 extracted to: {RAW_OUTPUT}")
    print(f"{'=' * 60}")


if __name__ == '__main__':
    main()
