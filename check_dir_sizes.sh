#!/bin/bash
# check_dir_sizes.sh
#
# Enumerate every entry (directory or file) at the top level of TARGET_DIR,
# then submit one sbatch job per entry to report its total disk usage via du.
# Each job writes its result to OUTPUT_DIR/<entry_name>.size.txt.
# A consolidated summary is assembled in OUTPUT_DIR/summary.txt once all jobs
# have run (summary is written by a final sync job that waits for all du jobs).
#
# Usage:
#   bash check_dir_sizes.sh <target_dir> [output_dir]
#
# Example:
#   bash check_dir_sizes.sh /oak/stanford/groups/awagner/HY
#   bash check_dir_sizes.sh /scratch/users/hyang336  /home/users/hyang336/jobs/du_results

set -euo pipefail

TARGET_DIR="${1:?Usage: $0 <target_dir> [output_dir]}"
OUTPUT_DIR="${2:-/home/users/hyang336/jobs/du_$(basename "${TARGET_DIR}")_$(date +%Y%m%d_%H%M%S)}"

# Resolve to absolute path
TARGET_DIR="$(realpath "${TARGET_DIR}")"

mkdir -p "${OUTPUT_DIR}"
echo "[INFO]: Checking sizes under: ${TARGET_DIR}"
echo "[INFO]: Per-entry results → ${OUTPUT_DIR}"

job_ids=()

for entry in "${TARGET_DIR}"/*; do
    # Skip if glob matched nothing
    [[ -e "${entry}" ]] || continue

    entry_name="$(basename "${entry}")"

    # SLURM job names may not contain slashes or spaces; replace with underscores
    # and truncate to 32 chars so the name stays readable in squeue output
    safe_name="$(echo "${entry_name}" | tr ' /' '__' | cut -c1-32)"
    job_name="du_${safe_name}"

    out_file="${OUTPUT_DIR}/${entry_name}.size.txt"

    job_id=$(sbatch \
        --job-name="${job_name}" \
        --output="${out_file}" \
        --error="${OUTPUT_DIR}/${entry_name}.err" \
        --time=8:00:00 \
        --mem=4G \
        --cpus-per-task=1 \
        --partition=awagner,hns,normal \
        --wrap="echo 'Entry: ${entry}'; du -sh '${entry}'; echo '---'; du -sh '${entry}'/* 2>/dev/null || true" \
        | awk '{print $4}')

    echo "[INFO]: Submitted job ${job_id} (${job_name}) for: ${entry}"
    job_ids+=("${job_id}")
done

if [[ ${#job_ids[@]} -eq 0 ]]; then
    echo "[WARN]: No entries found in ${TARGET_DIR}"
    exit 0
fi

# Build a colon-separated dependency string for the sync job
dep_list=$(IFS=:; echo "${job_ids[*]}")

# Final sync job: concatenate all per-entry results into one summary file
summary_file="${OUTPUT_DIR}/summary.txt"
sync_job_id=$(sbatch \
    --job-name="du_summary" \
    --dependency=afterany:"${dep_list}" \
    --output="${OUTPUT_DIR}/summary_job.out" \
    --error="${OUTPUT_DIR}/summary_job.err" \
    --time=0:10:00 \
    --mem=1G \
    --cpus-per-task=1 \
    --partition=awagner,hns,normal \
    --wrap="echo 'Disk usage summary for: ${TARGET_DIR}' > '${summary_file}'; \
            echo 'Generated: \$(date)' >> '${summary_file}'; \
            echo '========================================' >> '${summary_file}'; \
            cat '${OUTPUT_DIR}'/*.size.txt >> '${summary_file}' 2>/dev/null; \
            echo '========================================' >> '${summary_file}'; \
            echo 'Summary written to: ${summary_file}'" \
    | awk '{print $4}')

echo "[INFO]: Submitted ${#job_ids[@]} du jobs. Job IDs: ${job_ids[*]}"
echo "[INFO]: Summary sync job: ${sync_job_id} → ${summary_file}"
