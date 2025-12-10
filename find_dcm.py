import os
import io
import tarfile
import zipfile
import csv

# Optional deps for comprehensive detection
try:
    # pip install libarchive-c
    from libarchive import file_reader as libarchive_file_reader
    _HAS_LIBARCHIVE = True
except Exception:
    libarchive_file_reader = None
    _HAS_LIBARCHIVE = False

try:
    # pip install pydicom
    import pydicom
    from pydicom.errors import InvalidDicomError
    try:
        # Available in pydicom >= 2.x
        from pydicom.misc import is_dicom as pydicom_is_dicom
    except Exception:  # older pydicom
        pydicom_is_dicom = None
    _HAS_PYDICOM = True
except Exception:
    pydicom = None
    InvalidDicomError = Exception
    pydicom_is_dicom = None
    _HAS_PYDICOM = False

_ENTRY_PREFIX_MAX = 262144  # 256 KB cap for archive entry sniffing

# Simple instrumentation to report which paths were used
_stats = {
    'raw_magic_only': 0,
    'raw_pydicom_probe': 0,
    'archive_libarchive': 0,
    'archive_fallback': 0,
    'archive_entries_checked': 0,
}

def _has_dicom_magic_at_128(fileobj):
    """Quick check for DICOM Part 10 preamble+magic without loading entire file."""
    try:
        # Remember original position if possible
        pos = None
        try:
            pos = fileobj.tell()
        except Exception:
            pos = None
        fileobj.seek(128)
        magic = fileobj.read(4)
        # Restore position
        try:
            if pos is not None:
                fileobj.seek(pos)
        except Exception:
            pass
        return magic == b'DICM'
    except Exception:
        return False


def _is_dicom_via_pydicom(prefix_bytes: bytes) -> bool:
    """Use pydicom to validate DICOM from a limited prefix to avoid heavy I/O.

    Strategy:
    - If `pydicom.misc.is_dicom` exists, try it on the prefix stream.
    - Otherwise, attempt `pydicom.dcmread` with `stop_before_pixels=True`,
      `defer_size` small, and `force=True` on a BytesIO built from the prefix.
      Then validate presence of core attributes (e.g., SOPClassUID or File Meta).
    """
    if not _HAS_PYDICOM:
        return False

    bio = io.BytesIO(prefix_bytes)

    # Fast path if helper exists
    if pydicom_is_dicom is not None:
        try:
            # pydicom's helper expects a file-like at position 0
            bio.seek(0)
            return bool(pydicom_is_dicom(bio))
        except Exception:
            pass

    # Fallback: attempt a lightweight parse
    try:
        bio.seek(0)
        ds = pydicom.dcmread(bio, stop_before_pixels=True, defer_size=2048, force=True)
        # Basic sanity checks: dataset should contain some standard tags commonly present
        # Accept if File Meta present or core identity tags exist
        has_file_meta = getattr(ds, 'file_meta', None) is not None and len(ds.file_meta) > 0
        has_core = any(tag in ds for tag in [(0x0008, 0x0016), (0x0008, 0x0018)])  # SOPClassUID or SOPInstanceUID
        return bool(has_file_meta or has_core)
    except InvalidDicomError:
        return False
    except Exception:
        return False


def is_dicom_file(filepath):
    """Robust DICOM detection for raw files with minimal I/O.

    - Quick preamble+magic check for Part 10 files.
    - If pydicom is available, probe the first N bytes via lightweight parse.
    """
    try:
        with open(filepath, 'rb') as f:
            if _has_dicom_magic_at_128(f):
                _stats['raw_magic_only'] += 1
                return True
            # Read a reasonably sized prefix for pydicom probing
            prefix = f.read(131072)  # 128 KB
            if _is_dicom_via_pydicom(prefix):
                _stats['raw_pydicom_probe'] += 1
                return True
            return False
    except Exception:
        return False

def is_dicom_file(filepath):
    try:
        with open(filepath, 'rb') as f:
            return is_dicom_bytes(f)
    except Exception:
        return False

def _check_archive_with_libarchive(archive_path):
    """Return (has_dicom, is_corrupted) by streaming entries with libarchive.

    Reads up to `_ENTRY_PREFIX_MAX` per entry and probes via pydicom and magic.
    No extraction to disk.
    """
    if not _HAS_LIBARCHIVE:
        return False, False

    try:
        # libarchive yields entries; we read blocks from each without extracting
        with libarchive_file_reader(archive_path) as entries:
            for entry in entries:
                # Accumulate a limited prefix for probing
                prefix = bytearray()
                try:
                    for block in entry.get_blocks():
                        if len(prefix) >= _ENTRY_PREFIX_MAX:
                            break
                        need = _ENTRY_PREFIX_MAX - len(prefix)
                        prefix.extend(block[:need])
                        # Early exit if we already have enough for magic check
                        if len(prefix) >= 132:
                            # Quick magic check on available bytes
                            if len(prefix) >= 132 and prefix[128:132] == b'DICM':
                                _stats['archive_libarchive'] += 1
                                _stats['archive_entries_checked'] += 1
                                return True, False
                    # After gathering prefix, try pydicom-based probe
                    if prefix and _is_dicom_via_pydicom(bytes(prefix)):
                        _stats['archive_libarchive'] += 1
                        _stats['archive_entries_checked'] += 1
                        return True, False
                except Exception:
                    # Skip problematic entry but continue archive
                    _stats['archive_entries_checked'] += 1
                    continue
        return False, False
    except Exception as e:
        print(f"Error reading archive {archive_path}: {e}")
        return False, True

def _check_tar_or_zip_fallback(archive_path):
    """Fallback for when libarchive isn't available.

    Supports common tar(.gz/.tgz) and zip via stdlib, reading only needed bytes.
    Returns (has_dicom, is_corrupted).
    """
    # Try tar-like
    try:
        with tarfile.open(archive_path) as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                f = tar.extractfile(member)
                if f is None:
                    continue
                # Probe magic quickly
                if _has_dicom_magic_at_128(f):
                    _stats['archive_fallback'] += 1
                    _stats['archive_entries_checked'] += 1
                    return True, False
                try:
                    f.seek(0)
                except Exception:
                    pass
                prefix = f.read(131072)
                if _is_dicom_via_pydicom(prefix):
                    _stats['archive_fallback'] += 1
                    _stats['archive_entries_checked'] += 1
                    return True, False
        return False, False
    except tarfile.ReadError:
        # Not a tar archive
        pass
    except Exception as e:
        print(f"Error reading tar archive {archive_path}: {e}")
        return False, True

    # Try zip
    try:
        with zipfile.ZipFile(archive_path) as zf:
            for name in zf.namelist():
                with zf.open(name) as f:
                    # For zip files, we can't seek backwards in compressed stream; read prefix
                    prefix = f.read(131072)
                    # Quick magic if we have enough
                    if len(prefix) >= 132 and prefix[128:132] == b'DICM':
                        _stats['archive_fallback'] += 1
                        _stats['archive_entries_checked'] += 1
                        return True, False
                    if _is_dicom_via_pydicom(prefix):
                        _stats['archive_fallback'] += 1
                        _stats['archive_entries_checked'] += 1
                        return True, False
        return False, False
    except zipfile.BadZipFile:
        # Not a zip archive
        return False, False
    except Exception as e:
        print(f"Error reading zip archive {archive_path}: {e}")
        return False, True

def find_dicoms_and_archives_with_dicoms(root_dir):
    """
    Walk the tree and:
      - If a directory contains ONLY .dcm files (and at least one), record the directory path once (aggregated).
      - Otherwise, record individual DICOM file paths (.dcm) found (that are not in an all-dcm directory case).
      - For archives (.tar/.tar.gz/.tgz/.zip) that contain any DICOM, record only the archive path.
    Returns:
      aggregated_dirs: list of directory paths aggregated
      dicom_files: list of individual DICOM file paths
      archives: list of archive paths containing DICOM(s)
      corrupted_archives: list of corrupted archive paths
    """
    aggregated_dirs = []
    dicom_files = []
    archives = []
    corrupted_archives = []

    for dirpath, _, files in os.walk(root_dir):
        # Consider only regular file names (ignore hidden for aggregation test)
        visible_files = [f for f in files if not f.startswith('.')]
        
        if not visible_files:
            continue
            
        # Check archives for DICOM content using libarchive if available; otherwise fallback
        for filename in visible_files:
            filepath = os.path.join(dirpath, filename)
            # Prefer quick extension filter to reduce needless probing, but don't rely solely on it
            lower = filename.lower()
            looks_archive = lower.endswith((
                '.tar', '.tar.gz', '.tgz', '.tbz', '.tbz2', '.txz', '.zip', '.gz', '.bz2', '.xz', '.7z'
            ))

            has_dicom = False
            is_corrupted = False

            if looks_archive or _HAS_LIBARCHIVE:
                if _HAS_LIBARCHIVE:
                    has_dicom, is_corrupted = _check_archive_with_libarchive(filepath)
                else:
                    # Use stdlib fallback for common formats
                    has_dicom, is_corrupted = _check_tar_or_zip_fallback(filepath)

                if is_corrupted:
                    corrupted_archives.append(filepath)
                    continue
                if has_dicom:
                    archives.append(filepath)
                    continue
        
        # Now handle non-archive files
        if visible_files:
            # Check if ALL remaining files are raw DICOM files
            dicom_files_in_dir = []
            non_dicom_files_in_dir = []

            for filename in visible_files:
                filepath = os.path.join(dirpath, filename)
                # If we've already classified it as an archive with dicoms, skip
                if filepath in archives or filepath in corrupted_archives:
                    continue
                if is_dicom_file(filepath):
                    dicom_files_in_dir.append(filepath)
                else:
                    non_dicom_files_in_dir.append(filepath)

            if len(dicom_files_in_dir) > 0 and len(non_dicom_files_in_dir) == 0:
                aggregated_dirs.append(dirpath)
            else:
                dicom_files.extend(dicom_files_in_dir)
    
    return aggregated_dirs, dicom_files, archives, corrupted_archives

def write_results_to_csv(output_file, aggregated_dirs, dicom_files, archives, corrupted_archives):
    """Write the results to a CSV file."""
    with open(output_file, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(['Type', 'Path'])
        
        for dir_path in aggregated_dirs:
            writer.writerow(['aggregated_directory', dir_path])
        
        for file_path in dicom_files:
            writer.writerow(['individual_dicom', file_path])
        
        for archive_path in archives:
            writer.writerow(['archive_with_dicom', archive_path])
        
        for corrupt_path in corrupted_archives:
            writer.writerow(['corrupted_archive', corrupt_path])

def main():
    import sys
    
    if len(sys.argv) != 3:
        print("Usage: python find_dcm.py <root_directory> <output_csv>")
        sys.exit(1)
    
    root_dir = sys.argv[1]
    output_csv = sys.argv[2]
    
    if not os.path.exists(root_dir):
        print(f"Error: Root directory '{root_dir}' does not exist.")
        sys.exit(1)
    
    print(f"Searching for DICOM files in: {root_dir}")
    print(f"Output will be written to: {output_csv}")
    
    # Find DICOM files and archives
    aggregated_dirs, dicom_files, archives, corrupted_archives = find_dicoms_and_archives_with_dicoms(root_dir)
    
    # Write results to CSV
    write_results_to_csv(output_csv, aggregated_dirs, dicom_files, archives, corrupted_archives)
    
    # Print summary
    print(f"Found {len(aggregated_dirs)} aggregated directories with only DICOM files")
    print(f"Found {len(dicom_files)} individual DICOM files")
    print(f"Found {len(archives)} archives containing DICOM files")
    print(f"Found {len(corrupted_archives)} corrupted archives")
    print(f"Results written to: {output_csv}")

    # Report detection modes used
    print("Detection summary:")
    print(f"  pydicom available: {_HAS_PYDICOM}")
    print(f"  libarchive available: {_HAS_LIBARCHIVE}")
    print(f"  raw Part10 magic checks: {_stats['raw_magic_only']}")
    print(f"  raw pydicom lightweight probes: {_stats['raw_pydicom_probe']}")
    print(f"  archive entries checked: {_stats['archive_entries_checked']}")
    print(f"  archive via libarchive detections: {_stats['archive_libarchive']}")
    print(f"  archive via fallback detections: {_stats['archive_fallback']}")

if __name__ == "__main__":
    main()
