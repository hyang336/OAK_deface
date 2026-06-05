#!/bin/bash
# build_move_list.sh
#
# Scan CSVs produced by find_dcm and build a filtered job list for move_dcm.sbatch.
# Only includes CSVs that have at least one movable row (aggregated_directory,
# individual_dicom, or all_dicom_archive). Excludes hidden/dot-prefix CSVs,
# permission-only CSVs, and empty CSVs.
#
# Usage:
#   bash build_move_list.sh <csv_dir> [output_job_list]
#
# Example:
#   bash build_move_list.sh $SCRATCH/dcm_result20260323
#   # produces: $SCRATCH/dcm_result20260323/move_job_list.txt
#
# Then submit:
#   N=$(wc -l < $SCRATCH/dcm_result20260323/move_job_list.txt)
#   sbatch --array=1-$N G_home/find_anat/move_dcm.sbatch \
#       $SCRATCH/dcm_result20260323/move_job_list.txt \
#       $GROUP_SCRATCH/dcm_archive

set -euo pipefail

CSV_DIR="${1:?Usage: $0 <csv_dir> [output_job_list]}"
OUTPUT="${2:-${CSV_DIR}/move_job_list.txt}"

# Counters
total=0
skipped_hidden=0
skipped_empty=0
skipped_perm_only=0
included=0

# Temp file for the list
> "$OUTPUT"

for csv in "$CSV_DIR"/*.csv; do
    [ -f "$csv" ] || continue
    total=$((total + 1))

    basename_csv=$(basename "$csv")

    # Skip hidden / dot-prefix / macOS resource-fork files
    if [[ "$basename_csv" == .* ]]; then
        skipped_hidden=$((skipped_hidden + 1))
        continue
    fi

    # Count total data rows (lines after header)
    data_rows=$(awk -F',' 'NR > 1 && NF >= 3 && $1 != "" { count++ } END { print count+0 }' "$csv")

    if [ "$data_rows" -eq 0 ]; then
        skipped_empty=$((skipped_empty + 1))
        continue
    fi

    # Count movable rows
    movable=$(awk -F',' '
        NR > 1 && ($1 == "aggregated_directory" || $1 == "individual_dicom" || $1 == "all_dicom_archive") {
            count++
        }
        END { print count+0 }
    ' "$csv")

    if [ "$movable" -eq 0 ]; then
        skipped_perm_only=$((skipped_perm_only + 1))
        # Count permission_denied rows for reporting
        perm_rows=$(awk -F',' 'NR > 1 && $1 == "permission_denied" { count++ } END { print count+0 }' "$csv")
        echo "  SKIP (permission only) : $basename_csv  ($perm_rows permission_denied rows)"
        continue
    fi

    # This CSV qualifies
    echo "$csv" >> "$OUTPUT"
    included=$((included + 1))

    # Also count permission_denied for info
    perm_rows=$(awk -F',' 'NR > 1 && $1 == "permission_denied" { count++ } END { print count+0 }' "$csv")
    echo "  OK   : $basename_csv  ($movable movable, $perm_rows permission_denied)"
done

echo ""
echo "============================================"
echo "Summary"
echo "============================================"
echo "Total CSVs scanned    : $total"
echo "Skipped (hidden/dot)  : $skipped_hidden"
echo "Skipped (empty)       : $skipped_empty"
echo "Skipped (perm only)   : $skipped_perm_only"
echo "Included (have data)  : $included"
echo "Job list written to   : $OUTPUT"
echo ""

if [ "$included" -gt 0 ]; then
    echo "Next steps:"
    echo "  # 1. Dry-run first to verify:"
    echo "  N=$included"
    echo "  sbatch --array=1-\$N $HOME/G_home/find_anat/move_dcm.sbatch \\"
    echo "      $OUTPUT \\"
    echo "      \$GROUP_SCRATCH/dcm_archive --dry-run"
    echo ""
    echo "  # 2. Real run:"
    echo "  sbatch --array=1-\$N $HOME/G_home/find_anat/move_dcm.sbatch \\"
    echo "      $OUTPUT \\"
    echo "      \$GROUP_SCRATCH/dcm_archive"
else
    echo "No CSVs with movable items found."
fi
