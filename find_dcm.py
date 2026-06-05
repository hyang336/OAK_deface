import os
import io
import tarfile
import zipfile
import csv
import stat as stat_mod
from collections import defaultdict

# Unix-only modules for resolving owner/group names
try:
    import pwd
    import grp
    _HAS_UNIX_OWNER = True
except ImportError:
    _HAS_UNIX_OWNER = False

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
_NESTED_ARCHIVE_MAX = 10 * 1024 * 1024 * 1024  # 10 GB cap for reading nested archive entries
_NESTED_EXTS = ('.zip', '.tar', '.tar.gz', '.tgz', '.tbz', '.tbz2', '.txz')

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

def _is_entry_dicom_or_dicom_archive(data: bytes, entry_name: str) -> bool:
    """Return True if the entry is either a raw DICOM file or a nested archive
    containing exclusively DICOM files.

    Returns False if the entry is not DICOM and not a DICOM-only archive.
    """
    if not data:
        return False

    # 1. Check if this is a raw DICOM file
    if len(data) >= 132 and data[128:132] == b'DICM':
        return True
    if _is_dicom_via_pydicom(data[:_ENTRY_PREFIX_MAX]):
        return True

    # 2. Check if this is a nested archive containing exclusively DICOM
    entry_lower = entry_name.lower()
    is_potential_nested = any(entry_lower.endswith(ext) for ext in _NESTED_EXTS)
    if is_potential_nested:
        return _nested_archive_is_all_dicom(data)

    return False


def _nested_archive_is_all_dicom(data: bytes) -> bool:
    """Check if raw bytes represent a nested archive containing EXCLUSIVELY DICOM.

    Returns True only if every file entry in the archive is DICOM.
    Returns False if:
      - the data is not a valid archive
      - any entry is not DICOM
      - the archive is empty
    """
    if not data:
        return False

    bio = io.BytesIO(data)
    found_any_file = False

    # Try as zip first (most common nested format for Flywheel/SCItran DICOM)
    try:
        with zipfile.ZipFile(bio) as zf:
            for name in zf.namelist():
                # Skip directory entries
                if name.endswith('/'):
                    continue
                found_any_file = True
                try:
                    with zf.open(name) as f:
                        prefix = f.read(131072)
                        is_dcm = (
                            (len(prefix) >= 132 and prefix[128:132] == b'DICM')
                            or _is_dicom_via_pydicom(prefix)
                        )
                        if not is_dcm:
                            return False  # non-DICOM entry found
                except Exception:
                    return False  # can't read entry, treat as non-DICOM
            return found_any_file  # True only if at least one file and all were DICOM
    except zipfile.BadZipFile:
        pass
    except Exception:
        pass

    # Try as tar
    bio.seek(0)
    found_any_file = False
    try:
        with tarfile.open(fileobj=bio) as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                found_any_file = True
                f = tar.extractfile(member)
                if f is None:
                    return False
                prefix = f.read(131072)
                is_dcm = (
                    (len(prefix) >= 132 and prefix[128:132] == b'DICM')
                    or _is_dicom_via_pydicom(prefix)
                )
                if not is_dcm:
                    # Could itself be a nested archive — check recursively
                    member_lower = member.name.lower()
                    if any(member_lower.endswith(ext) for ext in _NESTED_EXTS):
                        try:
                            f.seek(0)
                            nested_data = f.read(_NESTED_ARCHIVE_MAX)
                            if not _nested_archive_is_all_dicom(nested_data):
                                return False
                        except Exception:
                            return False
                    else:
                        return False  # non-DICOM, non-archive entry
            return found_any_file
    except tarfile.ReadError:
        pass
    except Exception:
        pass

    return False


def _check_archive_with_libarchive(archive_path):
    """Return (all_dicom, is_corrupted) by streaming entries with libarchive.

    Returns all_dicom=True only if the archive is non-empty and EVERY file entry
    is either a raw DICOM or a nested archive containing exclusively DICOM.
    """
    if not _HAS_LIBARCHIVE:
        return False, False

    try:
        found_any_file = False
        with libarchive_file_reader(archive_path) as entries:
            for entry in entries:
                entry_name = getattr(entry, 'pathname', '') or ''
                entry_lower = entry_name.lower()
                is_potential_nested = any(entry_lower.endswith(ext) for ext in _NESTED_EXTS)

                if is_potential_nested:
                    entry_size = getattr(entry, 'size', 0) or 0
                    if entry_size > _NESTED_ARCHIVE_MAX:
                        _stats['archive_entries_checked'] += 1
                        return False, False  # entry too large to verify → not safe to list
                    max_read = entry_size if entry_size > 0 else _NESTED_ARCHIVE_MAX
                else:
                    max_read = _ENTRY_PREFIX_MAX

                # Accumulate entry bytes
                buf = bytearray()
                try:
                    for block in entry.get_blocks():
                        if len(buf) >= max_read:
                            break
                        need = max_read - len(buf)
                        buf.extend(block[:need])
                except Exception:
                    _stats['archive_entries_checked'] += 1
                    return False, False  # can't read entry → not safe

                data = bytes(buf)
                if not data:
                    # Skip zero-length entries (e.g. directory entries)
                    continue

                found_any_file = True
                _stats['archive_entries_checked'] += 1

                if not _is_entry_dicom_or_dicom_archive(data, entry_name):
                    return False, False  # non-DICOM entry found → reject

        if found_any_file:
            _stats['archive_libarchive'] += 1
        return found_any_file, False
    except Exception as e:
        err_msg = str(e)
        if 'Unrecognized archive format' in err_msg:
            return False, False
        print(f"Error reading archive {archive_path}: {e}")
        return False, True

def _check_tar_or_zip_fallback(archive_path):
    """Fallback for when libarchive isn't available.

    Returns (all_dicom, is_corrupted).
    all_dicom is True only if every file entry is DICOM or a DICOM-only nested archive.
    """
    # Try tar-like
    try:
        found_any_file = False
        with tarfile.open(archive_path) as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                found_any_file = True
                f = tar.extractfile(member)
                if f is None:
                    return False, False  # can't read → not safe
                _stats['archive_entries_checked'] += 1

                # Check if raw DICOM
                is_dcm = False
                if _has_dicom_magic_at_128(f):
                    is_dcm = True
                if not is_dcm:
                    try:
                        f.seek(0)
                    except Exception:
                        pass
                    prefix = f.read(131072)
                    if _is_dicom_via_pydicom(prefix):
                        is_dcm = True

                if not is_dcm:
                    # Check if entry is a nested archive containing exclusively DICOM
                    member_lower = member.name.lower()
                    if any(member_lower.endswith(ext) for ext in _NESTED_EXTS) and member.size <= _NESTED_ARCHIVE_MAX:
                        try:
                            f.seek(0)
                            full_data = f.read(member.size)
                            if _nested_archive_is_all_dicom(full_data):
                                is_dcm = True
                        except Exception:
                            pass

                if not is_dcm:
                    return False, False  # non-DICOM entry → reject archive

        if found_any_file:
            _stats['archive_fallback'] += 1
        return found_any_file, False
    except tarfile.ReadError:
        pass
    except Exception as e:
        print(f"Error reading tar archive {archive_path}: {e}")
        return False, True

    # Try zip
    try:
        found_any_file = False
        with zipfile.ZipFile(archive_path) as zf:
            for name in zf.namelist():
                if name.endswith('/'):
                    continue  # directory entry
                found_any_file = True
                with zf.open(name) as f:
                    prefix = f.read(131072)
                    _stats['archive_entries_checked'] += 1

                    is_dcm = (
                        (len(prefix) >= 132 and prefix[128:132] == b'DICM')
                        or _is_dicom_via_pydicom(prefix)
                    )

                    if not is_dcm:
                        # Check if entry is a nested archive containing exclusively DICOM
                        name_lower = name.lower()
                        if any(name_lower.endswith(ext) for ext in _NESTED_EXTS):
                            try:
                                rest = f.read(_NESTED_ARCHIVE_MAX - len(prefix))
                                full_data = prefix + rest
                                if _nested_archive_is_all_dicom(full_data):
                                    is_dcm = True
                            except Exception:
                                pass

                    if not is_dcm:
                        return False, False  # non-DICOM entry → reject archive

        if found_any_file:
            _stats['archive_fallback'] += 1
        return found_any_file, False
    except zipfile.BadZipFile:
        return False, False
    except Exception as e:
        print(f"Error reading zip archive {archive_path}: {e}")
        return False, True
    

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



def find_dicoms_and_archives_with_dicoms(root_dir):
    """
    Walk the tree and classify files for safe DICOM-only deletion:
      - aggregated_dirs: directories where ALL visible files are raw DICOM.
      - dicom_files: individual raw DICOM files in mixed (non-all-DICOM) directories.
      - archives: archive files whose contents are EXCLUSIVELY DICOM
        (including nested archives like tar → zip → .dcm).
      - corrupted_archives: archives that could not be read.

    An archive is only listed if EVERY file entry inside it (recursively through
    nested archives) is a valid DICOM file.  Archives containing any non-DICOM
    data are silently skipped to prevent accidental data loss.
    """
    aggregated_dirs = []
    dicom_files = []
    archives = []
    corrupted_archives = []
    permission_errors = []

    def _walk_onerror(err):
        """Callback for os.walk when it cannot list a directory."""
        if isinstance(err, PermissionError):
            permission_errors.append(err.filename or str(err))
            print(f"Permission denied: {err.filename}")
        else:
            print(f"Error walking directory: {err}")

    for dirpath, _, files in os.walk(root_dir, onerror=_walk_onerror):
        # Consider only regular file names (ignore hidden for aggregation test)
        visible_files = [f for f in files if not f.startswith('.')]
        
        if not visible_files:
            continue
            
        # Check archives for DICOM content using libarchive if available; otherwise fallback
        for filename in visible_files:
            filepath = os.path.join(dirpath, filename)
            # Only attempt archive detection on files whose extension looks like an archive
            lower = filename.lower()
            # Exclude domain-specific compressed formats that aren't true archives
            # .nii.gz          — NIfTI neuroimaging
            # .fib.gz          — DSI Studio fiber file
            # .src.gz          — DSI Studio source file
            # .qc.txt          — (not .gz, won't match, but listed for docs)
            is_neuroimaging_compressed = lower.endswith((
                '.nii.gz', '.nii.bz2', '.nii.xz',
                '.fib.gz', '.src.gz',
            ))
            looks_archive = (
                lower.endswith((
                    '.tar', '.tar.gz', '.tgz', '.tbz', '.tbz2', '.txz',
                    '.zip', '.gz', '.bz2', '.xz', '.7z',
                ))
                and not is_neuroimaging_compressed
            )

            has_dicom = False
            is_corrupted = False

            if looks_archive:
                try:
                    if _HAS_LIBARCHIVE:
                        has_dicom, is_corrupted = _check_archive_with_libarchive(filepath)
                    else:
                        # Use stdlib fallback for common formats
                        has_dicom, is_corrupted = _check_tar_or_zip_fallback(filepath)
                except PermissionError:
                    permission_errors.append(filepath)
                    print(f"Permission denied: {filepath}")
                    continue

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
                try:
                    is_dcm = is_dicom_file(filepath)
                except PermissionError:
                    permission_errors.append(filepath)
                    print(f"Permission denied: {filepath}")
                    non_dicom_files_in_dir.append(filepath)  # treat as non-DICOM to be safe
                    continue
                if is_dcm:
                    dicom_files_in_dir.append(filepath)
                else:
                    non_dicom_files_in_dir.append(filepath)

            if len(dicom_files_in_dir) > 0 and len(non_dicom_files_in_dir) == 0:
                aggregated_dirs.append(dirpath)
            else:
                dicom_files.extend(dicom_files_in_dir)
    
    return aggregated_dirs, dicom_files, archives, corrupted_archives, permission_errors

def write_results_to_csv(output_file, root_dir, aggregated_dirs, dicom_files, archives, corrupted_archives, permission_errors):
    """Write the results to a CSV file.

    Each row contains the type, the root directory, and the path relative to
    root_dir so that a downstream script can reconstruct the full path as
    os.path.join(root, relative_path) and preserve folder structure when moving.
    """
    with open(output_file, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(['Type', 'Root', 'RelativePath'])
        
        for dir_path in aggregated_dirs:
            writer.writerow(['aggregated_directory', root_dir, os.path.relpath(dir_path, root_dir)])
        
        for file_path in dicom_files:
            writer.writerow(['individual_dicom', root_dir, os.path.relpath(file_path, root_dir)])
        
        for archive_path in archives:
            writer.writerow(['all_dicom_archive', root_dir, os.path.relpath(archive_path, root_dir)])
        
        for corrupt_path in corrupted_archives:
            writer.writerow(['corrupted_archive', root_dir, os.path.relpath(corrupt_path, root_dir)])
        
        for perm_path in permission_errors:
            writer.writerow(['permission_denied', root_dir, os.path.relpath(perm_path, root_dir)])


def _resolve_owner(path):
    """Best-effort resolution of the owner and group of *path*.

    Falls back to the parent directory if *path* itself is inaccessible
    (which is the common case for permission-denied entries).
    Returns (username, groupname, uid, gid, octal_mode).
    """
    for target in (path, os.path.dirname(path)):
        try:
            st = os.stat(target)
        except (PermissionError, FileNotFoundError, OSError):
            continue
        uid, gid = st.st_uid, st.st_gid
        mode = stat_mod.filemode(st.st_mode)
        if _HAS_UNIX_OWNER:
            try:
                user = pwd.getpwuid(uid).pw_name
            except KeyError:
                user = str(uid)
            try:
                group = grp.getgrgid(gid).gr_name
            except KeyError:
                group = str(gid)
        else:
            user, group = str(uid), str(gid)
        return user, group, uid, gid, mode
    return 'unknown', 'unknown', -1, -1, '??????????'


def write_permission_report(report_path, root_dir, permission_errors):
    """Write a human-readable permission report grouped by owner.

    The file is intended to be forwarded to file owners or sysadmins so
    they can adjust permissions (e.g. ``chmod g+rX`` or ACL changes).
    It also writes a companion CSV (<report_path>.csv) for programmatic use.
    """
    if not permission_errors:
        return

    # Resolve ownership for each path
    by_owner = defaultdict(list)  # (user,group) -> [(path, mode)]
    records = []  # for CSV
    for p in sorted(permission_errors):
        user, group, uid, gid, mode = _resolve_owner(p)
        rel = os.path.relpath(p, root_dir)
        by_owner[(user, group)].append((rel, mode))
        records.append((user, group, uid, gid, mode, rel))

    # ---- human-readable report ----
    with open(report_path, 'w') as f:
        f.write('=' * 72 + '\n')
        f.write('PERMISSION-DENIED SUMMARY REPORT\n')
        f.write(f'Root searched : {root_dir}\n')
        f.write(f'Total blocked : {len(permission_errors)} path(s)\n')
        f.write(f'Unique owners : {len(by_owner)}\n')
        f.write('=' * 72 + '\n\n')

        for (user, group), entries in sorted(by_owner.items(),
                                              key=lambda kv: (-len(kv[1]), kv[0])):
            f.write(f'--- Owner: {user}  Group: {group}  '
                    f'({len(entries)} path(s)) ---\n')
            for rel, mode in entries:
                f.write(f'  {mode}  {rel}\n')
            f.write('\n')

        f.write('-' * 72 + '\n')
        f.write('Suggested fix (run as owner or root):\n')
        f.write('  chmod -R g+rX <path>   # grant group read+traverse\n')
        f.write('  # or use setfacl for finer-grained control:\n')
        f.write('  # setfacl -R -m g:<your_group>:rX <path>\n')

    # ---- companion CSV ----
    csv_path = report_path + '.csv'
    with open(csv_path, 'w', newline='') as cf:
        writer = csv.writer(cf)
        writer.writerow(['Owner', 'Group', 'UID', 'GID', 'Mode', 'RelativePath'])
        for rec in records:
            writer.writerow(rec)

    return report_path, csv_path


def main():
    import sys
    
    if len(sys.argv) < 3 or len(sys.argv) > 4:
        print("Usage: python find_dcm.py <root_directory> <output_csv> [permission_report]")
        print("  permission_report : optional path for the permission-denied summary")
        print("                     (defaults to <output_csv_stem>_permissions.txt)")
        sys.exit(1)
    
    root_dir = sys.argv[1]
    output_csv = sys.argv[2]
    if len(sys.argv) == 4:
        perm_report = sys.argv[3]
    else:
        stem, _ = os.path.splitext(output_csv)
        perm_report = stem + '_permissions.txt'
    
    if not os.path.exists(root_dir):
        print(f"Error: Root directory '{root_dir}' does not exist.")
        sys.exit(1)
    
    print(f"Searching for DICOM files in: {root_dir}")
    print(f"Output will be written to: {output_csv}")
    
    # Find DICOM files and archives
    aggregated_dirs, dicom_files, archives, corrupted_archives, permission_errors = find_dicoms_and_archives_with_dicoms(root_dir)
    
    # Write results to CSV
    write_results_to_csv(output_csv, root_dir, aggregated_dirs, dicom_files, archives, corrupted_archives, permission_errors)
    
    # Write permission report
    if permission_errors:
        txt_path, csv_perm_path = write_permission_report(
            perm_report, root_dir, permission_errors)
        print(f"Permission report written to: {txt_path}")
        print(f"Permission CSV written to  : {csv_perm_path}")

    # Print summary
    print(f"Found {len(aggregated_dirs)} aggregated directories with only DICOM files")
    print(f"Found {len(dicom_files)} individual DICOM files")
    print(f"Found {len(archives)} archives containing DICOM files")
    print(f"Found {len(corrupted_archives)} corrupted archives")
    print(f"Found {len(permission_errors)} paths with permission denied")
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
