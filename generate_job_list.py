#!/usr/bin/env python3
"""
Generate a job list file for the find_dcm SLURM array job.
This script creates a text file where each line contains a path to process.
Supports filtering by file owner (useful for group directories).
"""

import os
import sys
import argparse
import pwd


def generate_job_list(parent_dir, output_dir, owner=None):
    """
    Generate a list of all directories and files in the parent directory.
    Each item will be processed as a separate job in the SLURM array.

    If owner is given (username string), only items owned by that user are included.
    """
    if not os.path.exists(parent_dir):
        print(f"Error: Parent directory '{parent_dir}' does not exist.")
        sys.exit(1)

    # Resolve owner UID once if filtering
    owner_uid = None
    if owner:
        try:
            owner_uid = pwd.getpwnam(owner).pw_uid
        except KeyError:
            print(f"Error: user '{owner}' not found on this system.")
            sys.exit(1)
    
    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)
    
    job_list_file = os.path.join(output_dir, "job_list.txt")
    items_to_process = []
    
    try:
        # Get all items (files and directories) in the parent directory
        for item in os.listdir(parent_dir):
            item_path = os.path.join(parent_dir, item)
            # Filter by owner if requested
            if owner_uid is not None:
                try:
                    if os.stat(item_path).st_uid != owner_uid:
                        continue
                except OSError:
                    continue  # skip items we can't stat
            # Add both files and directories to the processing list
            items_to_process.append(item_path)
        
        # Sort for consistent ordering
        items_to_process.sort()
        
        # Write the job list file
        with open(job_list_file, 'w') as f:
            for item in items_to_process:
                f.write(f"{item}\n")
        
        print(f"Generated job list with {len(items_to_process)} items")
        print(f"Job list written to: {job_list_file}")
        print(f"\nTo submit the job array, run:")
        print(f"sbatch --array=1-{len(items_to_process)} find_dcm.sbatch {parent_dir} {output_dir}")
        
        # Print first few items for verification
        print(f"\nFirst few items to process:")
        for i, item in enumerate(items_to_process[:5]):
            print(f"  {i+1}: {item}")
        if len(items_to_process) > 5:
            print(f"  ... and {len(items_to_process) - 5} more items")
            
    except Exception as e:
        print(f"Error generating job list: {e}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Generate job list for find_dcm SLURM array job",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example usage:
  python generate_job_list.py /oak/stanford/groups/awagner /scratch/users/hyang336/dcm_results --owner hyang336
  
This will create a job_list.txt file in the output directory, then you can run:
  sbatch --array=1-N find_dcm.sbatch /scratch/users/hyang336/dcm_results/job_list.txt /scratch/users/hyang336/dcm_results
  
where N is the number printed by this script.
        """
    )
    
    parser.add_argument("parent_dir", help="Parent directory to scan for subdirectories and files")
    parser.add_argument("output_dir", help="Directory where job list and results will be written")
    parser.add_argument("--owner", default=None,
                        help="Only include items owned by this username (default: include all)")
    
    args = parser.parse_args()
    
    generate_job_list(args.parent_dir, args.output_dir, owner=args.owner)


if __name__ == "__main__":
    main()