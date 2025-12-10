#!/bin/bash
#SBATCH --job-name=find_dcm_array
#SBATCH --output=/home/users/hyang336/jobs/find_dcm_array_%A_%a.out
#SBATCH --error=/home/users/hyang336/jobs/find_dcm_array_%A_%a.err
#SBATCH --time=48:00:00
#SBATCH --nodes=1
#SBATCH --mem=16G
#SBATCH --cpus-per-task=1
#SBATCH --partition=awagner,hns,normal

# Usage: sbatch --array=1-N find_dcm.sbatch <parent_directory> <output_directory>
# where N is the number of items to process (will be determined by generate_job_list.py)

if [ $# -ne 2 ]; then
    echo "Usage: sbatch --array=1-N $0 <parent_directory> <output_directory>"
    exit 1
fi

PARENT_DIR=$1
OUTPUT_DIR=$2
JOB_LIST_FILE="${OUTPUT_DIR}/job_list.txt"

# Check if job list file exists
if [ ! -f "$JOB_LIST_FILE" ]; then
    echo "Error: Job list file $JOB_LIST_FILE not found!"
    echo "Please run: python generate_job_list.py $PARENT_DIR $OUTPUT_DIR first"
    exit 1
fi

# Get the specific directory/file for this array task
TARGET=$(sed -n "${SLURM_ARRAY_TASK_ID}p" "$JOB_LIST_FILE")

if [ -z "$TARGET" ]; then
    echo "Error: No target found for array task ID $SLURM_ARRAY_TASK_ID"
    exit 1
fi

# Create output filename based on the target path
TARGET_BASENAME=$(basename "$TARGET")
OUTPUT_CSV="${OUTPUT_DIR}/find_dcm_results_${TARGET_BASENAME}_${SLURM_ARRAY_TASK_ID}.csv"

echo "Processing: $TARGET"
echo "Output will be written to: $OUTPUT_CSV"

source /home/groups/awagner/miniconda3/etc/profile.d/mamba.sh
source /home/groups/awagner/miniconda3/etc/profile.d/conda.sh
mamba activate mri_deface_env

python find_dcm.py "$TARGET" "$OUTPUT_CSV"