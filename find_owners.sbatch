#!/bin/bash
#SBATCH --job-name=find_owners
#SBATCH --output=/home/users/hyang336/jobs/find_owners_%A_%a.out
#SBATCH --error=/home/users/hyang336/jobs/find_owners_%A_%a.err
#SBATCH --time=24:00:00
#SBATCH --nodes=1
#SBATCH --mem=4G
#SBATCH --cpus-per-task=1
#SBATCH --partition=awagner,hns,normal

# Find all unique file/folder owners and their file counts in a directory.
#
# Usage:
#   sbatch find_owners.sbatch <target_directory> [output_file]
#
# Example:
#   sbatch find_owners.sbatch /oak/stanford/groups/awagner/AM
#   sbatch find_owners.sbatch /oak/stanford/groups/awagner/AM /home/users/hyang336/scratch/AM_owners.txt

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: sbatch $0 <target_directory> [output_file]"
    exit 1
fi

TARGET_DIR="$1"
OUTPUT="${2:-/home/users/hyang336/scratch/owners_$(basename "$TARGET_DIR").txt}"

echo "Target directory: $TARGET_DIR"
echo "Output file:      $OUTPUT"
echo "Started:          $(date)"
echo ""

# Header
printf "%10s  %-s\n" "FILE_COUNT" "OWNER_ID" > "$OUTPUT"

find "$TARGET_DIR" -printf '%u\n' 2>/dev/null | sort | uniq -c | sort -rn >> "$OUTPUT"

echo "Owner counts for: $TARGET_DIR" 
echo "=========================================="
cat "$OUTPUT"
echo ""
echo "=========================================="
echo "Finished: $(date)"
