#!/bin/bash
#SBATCH --job-name=ar_py
#SBATCH --time=1:00:00
#SBATCH --nodes=1
#SBATCH --mem=8G
#SBATCH --cpus-per-task=1
#SBATCH --partition=awagner,hns,normal

# ar_py.sbatch — run one of the active_reface Python stages in mri_deface_env.
# Submitted by active_reface.sh (report / commit / restore); do not submit directly.
#   $1 RUN_DIR   $2 script.py   $3.. script arguments

set -euo pipefail
RUN_DIR="${1:?RUN_DIR required}"; SCRIPT="${2:?script required}"; shift 2
source "${RUN_DIR}/config.sh"
source /home/groups/awagner/miniconda3/etc/profile.d/conda.sh
conda activate mri_deface_env

python3 "${AR_HOME}/${SCRIPT}" "$@"
