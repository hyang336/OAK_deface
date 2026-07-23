#!/bin/bash
#SBATCH --job-name=deface_del
#SBATCH --output=/home/users/hyang336/jobs/deface_del_%j.out
#SBATCH --error=/home/users/hyang336/jobs/deface_del_%j.err
#SBATCH --time=4:00:00
#SBATCH --nodes=1
#SBATCH --mem=128G
#SBATCH --cpus-per-task=16
#SBATCH --partition=awagner,hns,normal

# deface_delete_job.sbatch
#
# In-place deface NIfTI files and/or delete non-NIfTI anatomical files
# identified by find_anat.py.  Called by deface_delete_from_csv.sh —
# do not submit directly.
#
# Supported actions
# -----------------
#   DEFACE   — reface a single NIfTI (.nii/.nii.gz) file using mri_reface
#              (Apptainer), then overwrite the original with the result.
#   DELETE   — delete a single non-NIfTI file.
#              ANALYZE (.hdr): the paired .img sidecar is also deleted.
#              NRRD (.nhdr):   only the header is deleted; a warning is logged
#                              about any orphaned data file.
#   ARCHIVE  — extract the archive to scratch, apply DEFACE / DELETE to the
#              listed members, repack, and overwrite the original archive.
#
# Positional arguments (set by deface_delete_from_csv.sh):
#   $1  FILEPATH         Absolute path to the file or archive.
#   $2  ACTION           DEFACE | DELETE | ARCHIVE
#   $3  DEFACE_MEMBERS   Semicolon-separated archive member paths to deface.
#                        Empty string for non-archive jobs.
#   $4  DELETE_MEMBERS   Semicolon-separated archive member paths to delete.
#                        Empty string for non-archive jobs.
#   $5  SCRATCH_DIR      Scratch base for temp extraction / mri_reface work.
#   $6  MRI_REFACE_SIF   Apptainer SIF for mri_reface (required for DEFACE /
#                        ARCHIVE with deface members).
#   $7  IMTYPE           mri_reface -imType value  [default: AUTO]
#   $8  PARALLEL_JOBS    Max number of archive members to deface concurrently
#                        within this SLURM task. 0 means auto.

set -euo pipefail

FILEPATH="${1:?FILEPATH required}"
ACTION="${2:?ACTION required}"
DEFACE_MEMBERS="${3}"       # semicolon-separated string, or path to a text file
DELETE_MEMBERS="${4}"       # semicolon-separated string, or path to a text file
SCRATCH_DIR="${5:?SCRATCH_DIR required}"

# Member lists for large ARCHIVE jobs are written to scratch files by the
# orchestrator to avoid shell ARG_MAX limits.  Read them back here if so.
if [[ -n "$DEFACE_MEMBERS" && -f "$DEFACE_MEMBERS" ]]; then
    DEFACE_MEMBERS=$(< "$DEFACE_MEMBERS")
fi
if [[ -n "$DELETE_MEMBERS" && -f "$DELETE_MEMBERS" ]]; then
    DELETE_MEMBERS=$(< "$DELETE_MEMBERS")
fi
MRI_REFACE_SIF="${6:-}"
IMTYPE="${7:-AUTO}"
PARALLEL_JOBS="${8:-0}"
THREADS="${SLURM_CPUS_PER_TASK:-4}"
MAX_SAFE_PARALLEL_JOBS="${MAX_SAFE_PARALLEL_JOBS:-4}"

echo "[INFO]: filepath:        ${FILEPATH}"
echo "[INFO]: action:          ${ACTION}"
echo "[INFO]: deface_members:  ${DEFACE_MEMBERS:-<none>}"
echo "[INFO]: delete_members:  ${DELETE_MEMBERS:-<none>}"
echo "[INFO]: scratch_dir:     ${SCRATCH_DIR}"
echo "[INFO]: mri_reface_sif:  ${MRI_REFACE_SIF:-<not set>}"
echo "[INFO]: imType:          ${IMTYPE}"
echo "[INFO]: parallel_jobs:   ${PARALLEL_JOBS}"
echo "[INFO]: threads:         ${THREADS}"
echo "[INFO]: max_safe_parallel_jobs: ${MAX_SAFE_PARALLEL_JOBS}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

tar_compress_flag() {
    # Return the tar compression flag letter for a given archive filename.
    local lower="${1,,}"
    [[ "$lower" == *.tar.gz  || "$lower" == *.tgz  ]] && { echo "z"; return; }
    [[ "$lower" == *.tar.bz2 || "$lower" == *.tbz2 ]] && { echo "j"; return; }
    [[ "$lower" == *.tar.xz  || "$lower" == *.txz  ]] && { echo "J"; return; }
    echo ""
}

delete_file() {
    # Delete a file and any well-known sidecars.
    local f="$1"
    if [[ ! -f "$f" ]]; then
        echo "[WARN]: Not found, skipping delete: ${f}"
        return 0
    fi
    rm -f "$f"
    echo "[INFO]: Deleted: ${f}"
    local lower_f="${f,,}"
    # ANALYZE: delete paired .img sidecar
    if [[ "$lower_f" == *.hdr ]]; then
        local img="${f%.*}.img"
        if [[ -f "$img" ]]; then
            rm -f "$img"
            echo "[INFO]: Deleted ANALYZE sidecar: ${img}"
        fi
    fi
    # NRRD: warn about potentially orphaned binary data file
    if [[ "$lower_f" == *.nhdr ]]; then
        echo "[WARN]: Deleted NRRD header ${f} — verify that any separate data" \
             "file referenced inside the header has been removed."
    fi
}

run_reface() {
    # Deface a NIfTI file in place using mri_reface (Apptainer).
    # The caller must supply an empty work_dir and is responsible for cleanup.
    # Optional 3rd argument overrides the global IMTYPE for this specific call.
    local input_file="$1"
    local work_dir="$2"
    local local_imtype="${3:-$IMTYPE}"
    local local_threads="${4:-$THREADS}"
    local base input_for_reface input_basename
    base=$(basename "$input_file")

    # Safety guard: AUTO is not reliable and empty means unknown modality.
    # Both cases should have been routed to DELETE by the orchestrator.
    if [[ -z "$local_imtype" || "$local_imtype" == "AUTO" ]]; then
        echo "[ERROR]: run_reface called with imType='${local_imtype}' for ${input_file}." \
             "Files with unknown modality must be routed to DELETE, not DEFACE." >&2
        return 1
    fi

    [[ ! -f "$input_file" ]] && { echo "[ERROR]: File not found: ${input_file}" >&2; return 1; }
    [[ -z "$MRI_REFACE_SIF" ]] && {
        echo "[ERROR]: MRI_REFACE_SIF is required for defacing but was not provided" >&2
        return 1
    }

    mkdir -p "${work_dir}/inputs" "${work_dir}/outputs"

    # mri_reface / ADIR_ReFace can mis-handle .nii.gz names (e.g. .gz.nii).
    # Always feed it an uncompressed .nii in scratch, then convert output back
    # to match the original extension when writing in place.
    if [[ "${base,,}" == *.nii.gz ]]; then
        input_basename="${base%.gz}"  # sub-XXX.nii
        input_for_reface="${work_dir}/inputs/${input_basename}"
        gunzip -c "$input_file" > "$input_for_reface"
    else
        input_basename="$base"
        input_for_reface="${work_dir}/inputs/${input_basename}"
        cp "$input_file" "$input_for_reface"
    fi

    echo "[INFO]: Running mri_reface on ${input_basename}…"
    apptainer exec \
        --bind "${work_dir}:${work_dir}" \
        "${MRI_REFACE_SIF}" \
        run_mri_reface.sh \
            "$input_for_reface" \
            "${work_dir}/outputs" \
            -imType  "${local_imtype}" \
            -threads "${local_threads}"

    local refaced
    refaced=$(find "${work_dir}/outputs" -maxdepth 1 \( -name "*_reface.nii*" -o -name "*_deFaced.nii*" -o -name "*_defaced.nii*" \) | head -1)
    [[ -z "$refaced" ]] && {
        echo "[ERROR]: mri_reface produced no recognised defaced output (*_reface.nii* or *_deFaced.nii*) for ${base}" >&2
        return 1
    }

    if [[ "${base,,}" == *.nii.gz ]]; then
        if [[ "${refaced,,}" == *.nii.gz ]]; then
            cp "${refaced}" "${input_file}"
        else
            gzip -c "${refaced}" > "${input_file}"
        fi
    else
        if [[ "${refaced,,}" == *.nii.gz ]]; then
            gunzip -c "${refaced}" > "${input_file}"
        else
            cp "${refaced}" "${input_file}"
        fi
    fi
    echo "[INFO]: Replaced in place with defaced version: ${input_file}"
}

# ---------------------------------------------------------------------------
# ACTION: DEFACE — deface a single regular NIfTI file in place
# ---------------------------------------------------------------------------
if [[ "$ACTION" == "DEFACE" ]]; then

    [[ ! -f "$FILEPATH" ]] && { echo "[ERROR]: File not found: ${FILEPATH}" >&2; exit 1; }

    work_dir=$(mktemp -d "${SCRATCH_DIR}/deface_work_XXXXXX")
    trap "echo '[INFO]: Cleaning work dir'; rm -rf '${work_dir}'" EXIT

    run_reface "$FILEPATH" "$work_dir" "$IMTYPE" "$THREADS"

# ---------------------------------------------------------------------------
# ACTION: DELETE — delete a single non-NIfTI file
# ---------------------------------------------------------------------------
elif [[ "$ACTION" == "DELETE" ]]; then

    delete_file "$FILEPATH"

# ---------------------------------------------------------------------------
# ACTION: ARCHIVE — extract, deface/delete members, repack in place
# ---------------------------------------------------------------------------
elif [[ "$ACTION" == "ARCHIVE" ]]; then

    [[ ! -f "$FILEPATH" ]] && { echo "[ERROR]: Archive not found: ${FILEPATH}" >&2; exit 1; }

    extract_dir=$(mktemp -d "${SCRATCH_DIR}/deface_extract_XXXXXX")
    work_base=$(mktemp   -d "${SCRATCH_DIR}/deface_work_XXXXXX")
    tmp_archive=$(mktemp    "${SCRATCH_DIR}/deface_repack_XXXXXX")
    trap "echo '[INFO]: Cleaning scratch dirs'
          rm -rf '${extract_dir}' '${work_base}'
          rm -f  '${tmp_archive}'" EXIT

    # --- Extract ---
    echo "[INFO]: Extracting archive ($(du -sh "$FILEPATH" | cut -f1) compressed)…"
    archive_lower="${FILEPATH,,}"
    if [[ "$archive_lower" == *.zip ]]; then
        unzip -q "$FILEPATH" -d "$extract_dir"
    else
        cflag=$(tar_compress_flag "$FILEPATH")
        tar -x${cflag}f "$FILEPATH" -C "$extract_dir"
    fi

    # --- Deface NIfTI members ---
    if [[ -n "$DEFACE_MEMBERS" ]]; then
        IFS=';' read -ra deface_arr <<< "$DEFACE_MEMBERS"

        # Auto parallelism: use at most half the allocated CPUs and never more
        # than MAX_SAFE_PARALLEL_JOBS. mri_reface launches a heavyweight
        # MATLAB Runtime + ANTs process per worker, so CPU count alone is not
        # a safe proxy for concurrency. Empirically, one worker per CPU can
        # crash with segfaults / killed processes even when threads/member=1.
        if ! [[ "$PARALLEL_JOBS" =~ ^[0-9]+$ ]]; then
            echo "[WARN]: Invalid PARALLEL_JOBS='${PARALLEL_JOBS}', defaulting to auto." >&2
            PARALLEL_JOBS=0
        fi
        if ! [[ "$MAX_SAFE_PARALLEL_JOBS" =~ ^[0-9]+$ ]] || (( MAX_SAFE_PARALLEL_JOBS < 1 )); then
            echo "[WARN]: Invalid MAX_SAFE_PARALLEL_JOBS='${MAX_SAFE_PARALLEL_JOBS}', defaulting to 4." >&2
            MAX_SAFE_PARALLEL_JOBS=4
        fi
        if (( PARALLEL_JOBS <= 0 )); then
            PARALLEL_JOBS=$(( THREADS > 1 ? THREADS / 2 : 1 ))
            if (( PARALLEL_JOBS > MAX_SAFE_PARALLEL_JOBS )); then
                PARALLEL_JOBS=$MAX_SAFE_PARALLEL_JOBS
            fi
        fi

        # Count actionable members and cap worker count accordingly.
        n_deface=0
        for member_entry in "${deface_arr[@]}"; do
            [[ -z "$member_entry" ]] || n_deface=$((n_deface + 1))
        done
        if (( n_deface == 0 )); then
            PARALLEL_JOBS=1
        elif (( PARALLEL_JOBS > n_deface )); then
            PARALLEL_JOBS=$n_deface
        fi

        if (( PARALLEL_JOBS > MAX_SAFE_PARALLEL_JOBS )); then
            echo "[WARN]: Requested PARALLEL_JOBS=${PARALLEL_JOBS} exceeds safe cap ${MAX_SAFE_PARALLEL_JOBS}; clamping." >&2
            PARALLEL_JOBS=$MAX_SAFE_PARALLEL_JOBS
        fi

        threads_per_member=$(( THREADS / PARALLEL_JOBS ))
        (( threads_per_member < 1 )) && threads_per_member=1
        echo "[INFO]: Archive deface parallelism=${PARALLEL_JOBS}, threads/member=${threads_per_member}, members=${n_deface}"

        idx=0
        pids=()
        for member_entry in "${deface_arr[@]}"; do
            [[ -z "$member_entry" ]] && continue

            # Members are encoded as "path/to/file.nii.gz|T1" to carry per-file imType
            member="${member_entry%|*}"
            member_imtype="${member_entry##*|}"
            [[ "$member_imtype" == "$member_entry" ]] && member_imtype="$IMTYPE"

            extracted_file="${extract_dir}/${member}"
            if [[ ! -f "$extracted_file" ]]; then
                echo "[ERROR]: Member not found in extracted tree: ${extracted_file}" >&2
                exit 1
            fi

            member_idx=$idx
            member_work="${work_base}/deface_${member_idx}"
            mkdir -p "$member_work"

            (
                echo "[INFO]: --- Defacing member: ${member} (imType=${member_imtype}, worker=${member_idx}) ---"
                run_reface "$extracted_file" "$member_work" "$member_imtype" "$threads_per_member"
            ) &
            pids+=("$!")
            idx=$((idx + 1))

            if (( ${#pids[@]} >= PARALLEL_JOBS )); then
                batch_failed=0
                for pid in "${pids[@]}"; do
                    wait "$pid" || batch_failed=1
                done
                pids=()
                if (( batch_failed )); then
                    echo "[ERROR]: One or more parallel deface workers failed." >&2
                    exit 1
                fi
            fi
        done

        # Wait for final partial batch
        if (( ${#pids[@]} > 0 )); then
            batch_failed=0
            for pid in "${pids[@]}"; do
                wait "$pid" || batch_failed=1
            done
            if (( batch_failed )); then
                echo "[ERROR]: One or more parallel deface workers failed." >&2
                exit 1
            fi
        fi
    fi

    # --- Delete non-NIfTI members ---
    if [[ -n "$DELETE_MEMBERS" ]]; then
        IFS=';' read -ra delete_arr <<< "$DELETE_MEMBERS"
        for member in "${delete_arr[@]}"; do
            [[ -z "$member" ]] && continue
            echo "[INFO]: --- Deleting member: ${member} ---"
            delete_file "${extract_dir}/${member}"
        done
    fi

    # --- Repack ---
    echo "[INFO]: Repacking modified extraction tree…"
    if [[ "$archive_lower" == *.zip ]]; then
        (cd "$extract_dir" && zip -qr "$tmp_archive" .)
    else
        cflag=$(tar_compress_flag "$FILEPATH")
        tar -c${cflag}f "$tmp_archive" -C "$extract_dir" .
    fi

    # Atomically replace the original archive: stage next to the target so the
    # final mv is on the same filesystem (POSIX rename is atomic).
    orig_dir=$(dirname "$FILEPATH")
    orig_base=$(basename "$FILEPATH")
    staging="${orig_dir}/.${orig_base}.deface_staging"
    cp "$tmp_archive" "$staging"
    mv "$staging" "$FILEPATH"
    echo "[INFO]: Original archive replaced: ${FILEPATH}  ($(du -sh "$FILEPATH" | cut -f1))"

else
    echo "[ERROR]: Unknown ACTION: ${ACTION}" >&2
    exit 1
fi

echo "[INFO]: Job complete."
