import os
import tarfile
import zipfile
import csv

def is_dicom_bytes(fileobj):
    try:
        fileobj.seek(128)
        magic = fileobj.read(4)
        return magic == b'DICM'
    except Exception:
        return False

def is_dicom_file(filepath):
    try:
        with open(filepath, 'rb') as f:
            return is_dicom_bytes(f)
    except Exception:
        return False

def check_tar_archive(archive_path):
    """Return tuple: (has_dicom: bool, is_corrupted: bool)"""
    try:
        with tarfile.open(archive_path) as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                f = tar.extractfile(member)
                if f is None:
                    continue
                if is_dicom_bytes(f):
                    return True, False
        return False, False
    except Exception as e:
        print(f"Error reading tar archive {archive_path}: {e}")
        return False, True

def check_zip_archive(archive_path):
    """Return tuple: (has_dicom: bool, is_corrupted: bool)"""
    try:
        with zipfile.ZipFile(archive_path) as zf:
            for name in zf.namelist():
                with zf.open(name) as f:
                    if is_dicom_bytes(f):
                        return True, False
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
            
        # First, separate out archive files to handle separately
        archive_files = [f for f in visible_files if f.lower().endswith(('.tar', '.tar.gz', '.tgz', '.zip'))]
        non_archive_files = [f for f in visible_files if not f.lower().endswith(('.tar', '.tar.gz', '.tgz', '.zip'))]
        
        # Check archives for DICOM content
        for filename in archive_files:
            filepath = os.path.join(dirpath, filename)
            if filename.lower().endswith(('.tar', '.tar.gz', '.tgz')):
                has_dicom, is_corrupted = check_tar_archive(filepath)
                if is_corrupted:
                    corrupted_archives.append(filepath)
                elif has_dicom:
                    archives.append(filepath)
            elif filename.lower().endswith('.zip'):
                has_dicom, is_corrupted = check_zip_archive(filepath)
                if is_corrupted:
                    corrupted_archives.append(filepath)
                elif has_dicom:
                    archives.append(filepath)
        
        # Now handle non-archive files
        if non_archive_files:
            # Check if ALL non-archive files are DICOM files (using actual DICOM detection)
            dicom_files_in_dir = []
            non_dicom_files_in_dir = []
            
            for filename in non_archive_files:
                filepath = os.path.join(dirpath, filename)
                if is_dicom_file(filepath):
                    dicom_files_in_dir.append(filepath)
                else:
                    non_dicom_files_in_dir.append(filepath)
            
            # If directory contains ONLY DICOM files (and at least one), aggregate it
            if len(dicom_files_in_dir) > 0 and len(non_dicom_files_in_dir) == 0:
                aggregated_dirs.append(dirpath)
            else:
                # Otherwise, add individual DICOM files found
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

if __name__ == "__main__":
    main()
