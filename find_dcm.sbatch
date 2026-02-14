#!/bin/bash
#SBATCH --job-name=find_dcm
#SBATCH --output=/home/users/hyang336/jobs/find_dcm_%j.out
#SBATCH --error=/home/users/hyang336/jobs/find_dcm_%j.err
#SBATCH --time=48:00:00
#SBATCH --nodes=1
#SBATCH --mem=16G
#SBATCH --cpus-per-task=1
#SBATCH --partition=awagner,hns,normal

# Usage: sbatch find_dcm.sbatch <root_directory> <output_csv>

if [ $# -ne 2 ]; then
    echo "Usage: sbatch $0 <root_directory> <output_csv>"
    exit 1
fi

ROOT_DIR=$1
OUTPUT_CSV=$2

echo "Processing: $ROOT_DIR"
echo "Output will be written to: $OUTPUT_CSV"

source /home/groups/awagner/miniconda3/etc/profile.d/mamba.sh
source /home/groups/awagner/miniconda3/etc/profile.d/conda.sh
mamba activate mri_deface_env

python /home/users/hyang336/G_home/find_anat/find_dcm.py "$ROOT_DIR" "$OUTPUT_CSV"