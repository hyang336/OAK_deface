#!/bin/bash
#SBATCH --job-name=find_dcm
#SBATCH --output=/home/users/hyang336/jobs/find_dcm_%A_%a.out
#SBATCH --error=/home/users/hyang336/jobs/find_dcm_%A_%a.err
#SBATCH --time=48:00:00
#SBATCH --nodes=1
#SBATCH --mem=16G
#SBATCH --cpus-per-task=1
#SBATCH --partition=awagner,hns,normal

# ---- Array mode ----
# Usage:
#   sbatch --array=1-N find_dcm.sbatch <job_list_file> <output_dir>
#
# job_list_file: text file with one directory path per line
# output_dir:    directory where per-folder CSV results are written
#
# ---- Single mode (backward compatible) ----
# Usage:
#   sbatch find_dcm.sbatch <root_directory> <output_csv>

if [ $# -ne 2 ]; then
    echo "Usage (array): sbatch --array=1-N $0 <job_list_file> <output_dir>"
    echo "Usage (single): sbatch $0 <root_directory> <output_csv>"
    exit 1
fi

source /home/groups/awagner/miniconda3/etc/profile.d/mamba.sh
source /home/groups/awagner/miniconda3/etc/profile.d/conda.sh
mamba activate mri_deface_env

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
    mkdir -p "$OUTPUT_DIR"
else
    # ---- Single mode (backward compatible) ----
    ROOT_DIR=$1
    OUTPUT_CSV=$2
fi

echo "Processing: $ROOT_DIR"
echo "Output will be written to: $OUTPUT_CSV"

python /home/users/hyang336/G_home/find_anat/find_dcm.py "$ROOT_DIR" "$OUTPUT_CSV"