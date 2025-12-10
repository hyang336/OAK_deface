#!/usr/bin/env python3
import os
import argparse
import pandas as pd
import subprocess
from pydeface.utils import check_image

def find_anat_files(root_dir, extensions=(".nii", ".nii.gz")):
    """Recursively find anatomical MRI files based on filename patterns."""
    anat_files = []
    for dirpath, _, filenames in os.walk(root_dir):
        for fname in filenames:
            if fname.endswith(extensions) and ("T1" in fname or "T2" in fname or "anat" in fname):
                anat_files.append(os.path.join(dirpath, fname))
    return anat_files

def deface_file(filepath, overwrite=False):
    """Run pydeface on a file."""
    outpath = filepath if overwrite else filepath.replace(".nii", "_defaced.nii")
    cmd = ["pydeface", filepath, "--outfile", outpath]
    subprocess.run(cmd, check=True)
    return outpath

def main():
    parser = argparse.ArgumentParser(description="Check and deface anatomical MRI scans.")
    parser.add_argument("root_dir", help="Root directory to search.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite originals when defacing.")
    parser.add_argument("--auto", action="store_true", help="Auto-deface without prompting.")
    parser.add_argument("--csv", default="anat_deface_report.csv", help="Output CSV report.")
    args = parser.parse_args()

    files = find_anat_files(args.root_dir)
    results = []

    print(f"Found {len(files)} anatomical files. Checking deface status (this may take time)...")

    for f in files:
        try:
            already_defaced = check_image(f)
        except Exception as e:
            print(f"Error checking {f}: {e}")
            already_defaced = None

        results.append({
            "file": f,
            "status": "Already defaced" if already_defaced else "Not defaced" if already_defaced is False else "Check failed",
            "new_file": ""
        })

    # Separate lists
    not_defaced = [r for r in results if r["status"] == "Not defaced"]

    print(f"\nSummary:")
    print(f"  Already defaced: {sum(1 for r in results if r['status'] == 'Already defaced')}")
    print(f"  Not defaced: {len(not_defaced)}")
    print(f"  Check failed: {sum(1 for r in results if r['status'] == 'Check failed')}")

    # Option to deface not defaced
    if not_defaced:
        if args.auto:
            confirm = "y"
        else:
            confirm = input(f"\nDeface {len(not_defaced)} not defaced files? [y/N]: ").strip().lower()

        if confirm == "y":
            for r in not_defaced:
                try:
                    newf = deface_file(r["file"], overwrite=args.overwrite)
                    r["status"] = "Defaced now"
                    r["new_file"] = newf
                except Exception as e:
                    print(f"Error defacing {r['file']}: {e}")
                    r["status"] = "Deface failed"

    # Save report
    df = pd.DataFrame(results)
    df.to_csv(args.csv, index=False)
    print(f"\nReport saved to {args.csv}")

if __name__ == "__main__":
    main()
