#!/usr/bin/env python
"""Quick diagnostic: what does libarchive see inside the .tar files?"""
import os
import sys

try:
    from libarchive import file_reader as libarchive_file_reader
    print("libarchive-c available")
except ImportError:
    print("libarchive-c NOT available")
    sys.exit(1)

tar_dir = "/home/users/hyang336/OAK_HY/deface_testing/lvl2_folder/all_dicom_tar_folder"
targz_dir = "/home/users/hyang336/OAK_HY/deface_testing/lvl2_folder/all_dicom_tar_zip_folder"

for d in [tar_dir, targz_dir]:
    if not os.path.isdir(d):
        print(f"SKIP (not found): {d}")
        continue
    for fname in sorted(os.listdir(d)):
        fpath = os.path.join(d, fname)
        if not os.path.isfile(fpath):
            continue
        print(f"\n{'='*70}")
        print(f"FILE: {fpath}")
        print(f"  size on disk: {os.path.getsize(fpath)} bytes")
        try:
            with libarchive_file_reader(fpath) as entries:
                count = 0
                for entry in entries:
                    name = getattr(entry, 'pathname', '???')
                    size = getattr(entry, 'size', -1)
                    isdir = getattr(entry, 'isdir', None)
                    isfile = getattr(entry, 'isfile', None)
                    filetype = getattr(entry, 'filetype', None)

                    # Read the first few bytes
                    first_bytes = b''
                    total_data = 0
                    for block in entry.get_blocks():
                        if not first_bytes:
                            first_bytes = bytes(block[:256])
                        total_data += len(block)
                        if total_data > 10_000_000:  # stop after 10MB
                            break

                    print(f"  ENTRY[{count}]: name={name!r}, size={size}, isdir={isdir}, "
                          f"isfile={isfile}, filetype={filetype}, "
                          f"data_read={total_data}, first_16={first_bytes[:16].hex()}")
                    count += 1
                    if count > 20:
                        print("  ... (stopping after 20 entries)")
                        break
                print(f"  TOTAL entries seen: {count}")
        except Exception as e:
            print(f"  ERROR: {e}")

print("\nDone.")
