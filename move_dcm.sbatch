#!/bin/bash
#SBATCH --job-name=move_dcm
#SBATCH --output=/home/users/hyang336/jobs/move_dcm_%A_%a.out
#SBATCH --error=/home/users/hyang336/jobs/move_dcm_%A_%a.err
#SBATCH --time=48:00:00
#SBATCH --nodes=1
#SBATCH --mem=8G
#SBATCH --cpus-per-task=1
#SBATCH --partition=awagner,hns,normal

# Move detected DICOM items from find_dcm.py CSVs using rsync.
#
# ---- Array mode ----
#   sbatch --array=1-N move_dcm.sbatch <csv_list_file> <destination> [--dry-run]
#
# ---- Single mode ----
#   sbatch move_dcm.sbatch <single_csv> <destination> [--dry-run]
#
# The CSV must have columns: Type,Root,RelativePath
# Only aggregated_directory, individual_dicom, and all_dicom_archive rows are moved.

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: sbatch [--array=1-N] $0 <csv_list_or_csv> [destination] [--dry-run]"
    exit 1
fi

INPUT=$1
DESTINATION="${2:-$GROUP_SCRATCH/dcm_archive}"
DRY_RUN=""
if [ "${3:-}" = "--dry-run" ]; then
    DRY_RUN="--dry-run"
    echo "=== DRY RUN ==="
fi

# Resolve which CSV to process
if [ -n "${SLURM_ARRAY_TASK_ID:-}" ]; then
    CSV_FILE=$(sed -n "${SLURM_ARRAY_TASK_ID}p" "$INPUT")
    if [ -z "$CSV_FILE" ]; then
        echo "Error: no entry at line $SLURM_ARRAY_TASK_ID in $INPUT"
        exit 1
    fi
else
    CSV_FILE=$INPUT
fi

echo "CSV:         $CSV_FILE"
echo "Destination: $DESTINATION"

# --- Extract ROOT (first data row, column 2) ---
ROOT=$(awk -F',' 'NR==2 {print $2}' "$CSV_FILE")
if [ -z "$ROOT" ]; then
    echo "Error: could not extract Root from $CSV_FILE (empty or malformed)"
    exit 1
fi
echo "Source root:  $ROOT"

# --- Derive per-source subdirectory under DESTINATION ---
# E.g. ROOT=/oak/stanford/groups/awagner/AM → DESTINATION=$DESTINATION/AM
SOURCE_NAME=$(basename "$ROOT")
DESTINATION="$DESTINATION/$SOURCE_NAME"
echo "Final dest:   $DESTINATION"

# --- Build file list for rsync ---
# Extract RelativePath (col 3) for movable types (col 1)
# Use $LOCAL_SCRATCH (node-local SSD) if available, fall back to $SCRATCH
TMPDIR_BASE="${LOCAL_SCRATCH:-${SCRATCH:-/tmp}}"
TMPLIST=$(mktemp "${TMPDIR_BASE}/move_dcm_list.XXXXXX")
trap 'rm -f "$TMPLIST"' EXIT

awk -F',' '
    NR > 1 && ($1 == "aggregated_directory" || $1 == "individual_dicom" || $1 == "all_dicom_archive") {
        print $3
    }
' "$CSV_FILE" > "$TMPLIST"

COUNT=$(wc -l < "$TMPLIST")
echo "Items to move: $COUNT"

if [ "$COUNT" -eq 0 ]; then
    echo "Nothing to move."
    exit 0
fi

# --- rsync ---
# -a            : archive mode (preserves permissions, timestamps, etc.)
# -r            : recursive (required with --files-from for directories)
# --files-from  : read list of relative paths
# --remove-source-files : delete source FILES after successful transfer
# Note: --remove-source-files only removes files, not directories.
mkdir -p "$DESTINATION"

rsync -a -r \
    --files-from="$TMPLIST" \
    --remove-source-files \
    --itemize-changes \
    $DRY_RUN \
    "$ROOT/" \
    "$DESTINATION/"

RSYNC_EXIT=$?
echo "rsync exit code: $RSYNC_EXIT"

# --- Clean up empty source directories left behind ---
if [ -z "$DRY_RUN" ] && [ "$RSYNC_EXIT" -eq 0 ]; then
    echo "Cleaning up empty source directories..."
    # Read the aggregated_directory entries and remove if now empty
    awk -F',' 'NR > 1 && $1 == "aggregated_directory" { print $3 }' "$CSV_FILE" | \
    while IFS= read -r reldir; do
        srcdir="$ROOT/$reldir"
        if [ -d "$srcdir" ]; then
            # Remove directory only if empty (including nested empties)
            find "$srcdir" -depth -type d -empty -delete 2>/dev/null || true
        fi
    done
    echo "Cleanup done."
fi

echo "=== Finished ==="
