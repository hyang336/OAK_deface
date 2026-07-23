#!/bin/bash
#SBATCH --job-name=find_anat
#SBATCH --output=/home/users/hyang336/jobs/find_anat_%A_%a.out
#SBATCH --error=/home/users/hyang336/jobs/find_anat_%A_%a.err
#SBATCH --time=48:00:00
#SBATCH --nodes=1
#SBATCH --mem=16G
#SBATCH --cpus-per-task=1
#SBATCH --partition=awagner,hns,normal

# ---- Array mode ----
# Usage:
#   sbatch --array=1-N find_anat.sbatch <job_list_file> <output_dir> [extra_flags]
#
# job_list_file: text file with one directory path per line
#                (same format used by find_dcm / generate_job_list.py)
# output_dir:    directory where per-folder CSV results are written
# extra_flags:   optional flags forwarded to find_anat.py, e.g.:
#                  --include-unclassified
#                  --include-non-structural
#                  --skip-nibabel
#
# Example (array over a job list):
#   N=$(wc -l < /scratch/users/hyang336/dirs_to_scan.txt)
#   sbatch --array=1-$N find_anat.sbatch \
#       /scratch/users/hyang336/dirs_to_scan.txt \
#       /scratch/users/hyang336/find_anat_results
#
# ---- Single mode ----
# Usage:
#   sbatch find_anat.sbatch <root_directory> <output_csv> [extra_flags]
#
# Example (single directory):
#   sbatch find_anat.sbatch \
#       /oak/stanford/groups/awagner/HY/some_dir \
#       /scratch/users/hyang336/find_anat_results/some_dir.csv

if [ $# -lt 2 ]; then
    echo "Usage (array):  sbatch --array=1-N $0 <job_list_file> <output_dir> [flags]"
    echo "Usage (single): sbatch $0 <root_directory> <output_csv> [flags]"
    exit 1
fi

source /home/groups/awagner/miniconda3/etc/profile.d/mamba.sh
source /home/groups/awagner/miniconda3/etc/profile.d/conda.sh
mamba activate mri_deface_env

# Any arguments beyond the first two are forwarded to find_anat.py
EXTRA_FLAGS="${@:3}"

if [ -n "$SLURM_ARRAY_TASK_ID" ]; then
    # ---- Array mode: read the Nth line from the job list ----
    JOB_LIST=$1
    OUTPUT_DIR=$2
    ROOT_DIR=$(sed -n "${SLURM_ARRAY_TASK_ID}p" "$JOB_LIST")
    if [ -z "$ROOT_DIR" ]; then
        echo "Error: no entry at line $SLURM_ARRAY_TASK_ID in $JOB_LIST"
        exit 1
    fi
    DIRNAME=$(basename "$ROOT_DIR")
    OUTPUT_CSV="${OUTPUT_DIR}/${DIRNAME}.csv"
    PERM_REPORT="${OUTPUT_DIR}/${DIRNAME}_permissions.txt"
    mkdir -p "$OUTPUT_DIR"
else
    # ---- Single mode ----
    ROOT_DIR=$1
    OUTPUT_CSV=$2
    PERM_REPORT="${OUTPUT_CSV%.csv}_permissions.txt"
fi

echo "Processing:  $ROOT_DIR"
echo "Output CSV:  $OUTPUT_CSV"
echo "Perm report: $PERM_REPORT"
[ -n "$EXTRA_FLAGS" ] && echo "Extra flags: $EXTRA_FLAGS"

python /home/users/hyang336/G_home/find_anat/find_anat.py \
    "$ROOT_DIR" "$OUTPUT_CSV" \
    --permission-report "$PERM_REPORT" \
    $EXTRA_FLAGS
