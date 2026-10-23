#!/bin/bash
#SBATCH --job-name=ar_detect
#SBATCH --time=12:00:00
#SBATCH --nodes=1
#SBATCH --mem=8G
#SBATCH --cpus-per-task=1
#SBATCH --partition=awagner,hns,normal

# ar_detect.sbatch — inventory anatomical scans, then submit the reface stage.
# Submitted by `active_reface.sh start`; do not submit directly.
#   $1 RUN_DIR

set -euo pipefail
RUN_DIR="${1:?RUN_DIR required}"
source "${RUN_DIR}/config.sh"
source /home/groups/awagner/miniconda3/etc/profile.d/conda.sh
conda activate mri_deface_env

flags=()
(( SKIP_ARCHIVES )) && flags+=(--skip-archives)
python3 "${AR_HOME}/ar_detect.py" "$TOP_DIR" "$RUN_DIR" ${flags[@]+"${flags[@]}"}

bash "${AR_HOME}/active_reface.sh" submit-reface --run_dir "$RUN_DIR"
