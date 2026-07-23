#!/bin/bash
#SBATCH --job-name=deface_array
#SBATCH --output=/home/users/hyang336/jobs/deface_array_%A_%a.out
#SBATCH --error=/home/users/hyang336/jobs/deface_array_%A_%a.err
#SBATCH --time=4:00:00
#SBATCH --nodes=1
#SBATCH --mem=12G
#SBATCH --cpus-per-task=1
#SBATCH --partition=awagner,hns,normal

set -euo pipefail

STATE_DIR="${1:?STATE_DIR required}"
MRI_REFACE_SIF="${2:?MRI_REFACE_SIF required}"
CHUNK_SIZE="${3:-1}"

TASK_ID="${SLURM_ARRAY_TASK_ID:?SLURM_ARRAY_TASK_ID required}"
THREADS="${SLURM_CPUS_PER_TASK:-1}"
MANIFEST="${STATE_DIR}/manifests/deface_manifest.tsv"
EXTRACT_DIR="${STATE_DIR}/extract"
WORK_DIR="${STATE_DIR}/worker_${TASK_ID}"
MCR_CACHE_ROOT="${WORK_DIR}/mcr_cache"
TMP_WORK="${WORK_DIR}/tmp"

run_reface() {
    local input_file="$1"
    local work_dir="$2"
    local local_imtype="$3"
    local local_threads="$4"
    local base member_run_dir input_dir output_dir member_cache_root member_tmp_work
    local input_for_reface input_basename refaced reface_status

    base=$(basename "$input_file")
    [[ -z "$local_imtype" || "$local_imtype" == "AUTO" ]] && {
        echo "[ERROR]: Invalid imType '${local_imtype}' for ${input_file}" >&2
        return 1
    }
    [[ ! -f "$input_file" ]] && { echo "[ERROR]: File not found: ${input_file}" >&2; return 1; }

    member_run_dir=$(mktemp -d "${work_dir}/run_XXXXXX")
    input_dir="${member_run_dir}/inputs"
    output_dir="${member_run_dir}/outputs"
    member_cache_root="${member_run_dir}/mcr_cache"
    member_tmp_work="${member_run_dir}/tmp"

    mkdir -p "${input_dir}" "${output_dir}" "${member_cache_root}" "${member_tmp_work}"

    # Use a canonical in-container input filename to avoid parser/path edge
    # cases with names like "t1.nii_brain.nii.gz".
    if [[ "${base,,}" == *.nii.gz ]]; then
        input_basename="input.nii"
        input_for_reface="${input_dir}/${input_basename}"
        gunzip -c "$input_file" > "$input_for_reface"
    else
        input_basename="input.nii"
        input_for_reface="${input_dir}/${input_basename}"
        cp "$input_file" "$input_for_reface"
    fi

    echo "[INFO]: Running mri_reface on ${input_basename} (task=${TASK_ID}, threads=${local_threads})…"
    # Give each worker its own MATLAB Runtime cache and temp directory.
    # Shared MCR cache paths can trigger instability when many workers start
    # simultaneously.
    set +e
    apptainer exec \
        --cleanenv \
        --env "MCR_CACHE_ROOT=${member_cache_root},TMPDIR=${member_tmp_work}" \
        --bind "${member_run_dir}:${member_run_dir}" \
        "${MRI_REFACE_SIF}" \
        run_mri_reface.sh \
            "$input_for_reface" \
            "${output_dir}" \
            -imType  "${local_imtype}" \
            -threads "${local_threads}"
    reface_status=$?
    set -e

    refaced=$(find "${output_dir}" -maxdepth 1 \( -name "*_reface.nii*" -o -name "*_deFaced.nii*" -o -name "*_defaced.nii*" \) | head -1)
    [[ -z "$refaced" ]] && {
        echo "[ERROR]: mri_reface produced no recognised defaced output for ${base} (exit=${reface_status})" >&2
        return 1
    }

    # Some failures occur only during QC render generation after defaced NIfTI
    # output has already been written. In that case, continue using the output.
    if (( reface_status != 0 )); then
        echo "[WARN]: mri_reface exited non-zero (exit=${reface_status}) but defaced output exists; continuing for ${base}." >&2
    fi

    if [[ "${base,,}" == *.nii.gz ]]; then
        if [[ "${refaced,,}" == *.nii.gz ]]; then
            cp "$refaced" "$input_file"
        else
            gzip -c "$refaced" > "$input_file"
        fi
    else
        if [[ "${refaced,,}" == *.nii.gz ]]; then
            gunzip -c "$refaced" > "$input_file"
        else
            cp "$refaced" "$input_file"
        fi
    fi

    rm -rf "$member_run_dir"
}

trap "echo '[INFO]: Cleaning worker dir'; rm -rf '${WORK_DIR}'" EXIT

[[ "$CHUNK_SIZE" =~ ^[0-9]+$ ]] || { echo "[ERROR]: Invalid CHUNK_SIZE='${CHUNK_SIZE}'" >&2; exit 1; }
(( CHUNK_SIZE > 0 )) || { echo "[ERROR]: CHUNK_SIZE must be >= 1" >&2; exit 1; }

start_line=$(( TASK_ID * CHUNK_SIZE + 1 ))
end_line=$(( start_line + CHUNK_SIZE - 1 ))

echo "[INFO]: state_dir:   ${STATE_DIR}"
echo "[INFO]: task_id:     ${TASK_ID}"
echo "[INFO]: chunk_size:  ${CHUNK_SIZE}"
echo "[INFO]: line_range:  ${start_line}-${end_line}"
echo "[INFO]: threads:     ${THREADS}"
echo "[INFO]: mcr_cache:   ${MCR_CACHE_ROOT}"
echo "[INFO]: tmpdir:      ${TMP_WORK}"

# mktemp templates below are created under ${WORK_DIR}/run_XXXXXX, so ensure
# the parent exists for every array task.
mkdir -p "${WORK_DIR}"

mapfile -t chunk_lines < <(sed -n "${start_line},${end_line}p" "$MANIFEST")
(( ${#chunk_lines[@]} > 0 )) || { echo "[ERROR]: No manifest entries for task ${TASK_ID}" >&2; exit 1; }

for line in "${chunk_lines[@]}"; do
    [[ -z "$line" ]] && continue
    IFS=$'\t' read -r member member_imtype <<< "$line"
    target_file="${EXTRACT_DIR}/${member}"
    echo "[INFO]: member:      ${member}"
    echo "[INFO]: imType:      ${member_imtype}"
    echo "[INFO]: target_file: ${target_file}"
    run_reface "$target_file" "$WORK_DIR" "$member_imtype" "$THREADS"
done

echo "[INFO]: Worker complete."