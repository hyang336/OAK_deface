#!/bin/bash
#SBATCH --job-name=reface
#SBATCH --output=/home/users/hyang336/jobs/reface_%j.out
#SBATCH --error=/home/users/hyang336/jobs/reface_%j.err
#SBATCH --time=4:00:00
#SBATCH --nodes=1
#SBATCH --mem=32G
#SBATCH --cpus-per-task=4
#SBATCH --partition=awagner,hns,normal

# reface_job.sbatch
#
# Reface one or more NIfTI / MGZ / MGH files using mri_reface (Apptainer).
# Called by reface_from_csv.sh — do not submit directly.
#
# When multiple members from the same archive need refacing they are passed
# as a single job (semicolon-separated ARCHIVE_MEMBERS list) so the archive
# is extracted and repacked only once, avoiding concurrent repack races.
#
# Positional arguments (set by reface_from_csv.sh):
#   $1  FILEPATH         Absolute path to the file or archive.
#   $2  ARCHIVE_MEMBERS  Semicolon-separated list of member paths inside the
#                        archive.  Empty string → FILEPATH is a regular file.
#   $3  OUTPUT_DIR       Destination for output:
#                          regular file  → directory where mri_reface writes
#                          archive       → directory for the repacked archive
#   $4  SCRATCH_DIR      Scratch base for all temp work.
#   $5  MRI_REFACE_SIF   Apptainer SIF (or sandbox dir) for mri_reface.
#   $6  IMTYPE           mri_reface -imType value  (default: AUTO).
#   $7  FS_IMG           FreeSurfer Apptainer SIF — required for MGZ/MGH inputs.
#
# MGZ/MGH handling:
#   mri_reface only accepts NIfTI.  When the input is .mgz or .mgh this script
#   converts it to NIfTI via FreeSurfer mri_convert (inside FS_IMG), runs
#   mri_reface, then converts the refaced NIfTI back to the original format.
#   Only whole-head volumes (orig.mgz, rawavg.mgz, T1.mgz …) carry face info;
#   skull-stripped derivatives (brain.mgz, aseg.mgz …) do not and should be
#   filtered out before calling this script.

set -euo pipefail

FILEPATH="${1:?FILEPATH required}"
ARCHIVE_MEMBERS="${2}"           # semicolon-separated; empty = regular file
OUTPUT_DIR="${3:?OUTPUT_DIR required}"
SCRATCH_DIR="${4:?SCRATCH_DIR required}"
MRI_REFACE_SIF="${5:?MRI_REFACE_SIF required}"
IMTYPE="${6:-AUTO}"
FS_IMG="${7:-}"
THREADS="${SLURM_CPUS_PER_TASK:-4}"

echo "[INFO]: filepath:        ${FILEPATH}"
echo "[INFO]: archive_members: ${ARCHIVE_MEMBERS:-<none — regular file>}"
echo "[INFO]: output_dir:      ${OUTPUT_DIR}"
echo "[INFO]: scratch_dir:     ${SCRATCH_DIR}"
echo "[INFO]: imType:          ${IMTYPE}"
echo "[INFO]: threads:         ${THREADS}"
[[ -n "$FS_IMG" ]] && echo "[INFO]: fs_img: ${FS_IMG}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

get_fmt() {
    # Return 'nifti', 'mgz_mgh', or 'unsupported'
    local f="${1,,}"
    [[ "$f" == *.nii.gz || "$f" == *.nii ]] && { echo "nifti";   return; }
    [[ "$f" == *.mgz    || "$f" == *.mgh ]] && { echo "mgz_mgh"; return; }
    echo "unsupported"
}

to_nifti() {
    # Convert MGZ/MGH → NIfTI using FreeSurfer mri_convert inside Apptainer
    local input="$1"; local output="$2"
    [[ -z "$FS_IMG" ]] && { echo "[ERROR]: FS_IMG required for MGZ/MGH conversion" >&2; exit 1; }
    apptainer exec --bind "${SCRATCH_DIR}:${SCRATCH_DIR}" \
        "${FS_IMG}" mri_convert "${input}" "${output}"
    echo "[INFO]: mri_convert: $(basename "$input") → $(basename "$output")"
}

from_nifti() {
    # Convert refaced NIfTI → MGZ or MGH using FreeSurfer mri_convert
    local input="$1"; local output="$2"
    [[ -z "$FS_IMG" ]] && { echo "[ERROR]: FS_IMG required for MGZ/MGH conversion" >&2; exit 1; }
    apptainer exec --bind "${SCRATCH_DIR}:${SCRATCH_DIR}" \
        "${FS_IMG}" mri_convert "${input}" "${output}"
    echo "[INFO]: mri_convert back: $(basename "$input") → $(basename "$output")"
}

# Global set by reface_one — path to the final refaced file
REFACED_FILE=""

reface_one() {
    # Reface one file (NIfTI or MGZ/MGH).
    # Writes all mri_reface outputs to WORK_DIR/outputs/.
    # Sets REFACED_FILE to the path of the defaced structural image.
    local input_file="$1"
    local work_dir="$2"

    local fmt; fmt=$(get_fmt "$input_file")
    [[ "$fmt" == "unsupported" ]] && {
        echo "[ERROR]: Unsupported format: ${input_file}" >&2; exit 1; }

    mkdir -p "${work_dir}/inputs" "${work_dir}/outputs"

    # Prepare NIfTI input for mri_reface
    local nii_input
    if [[ "$fmt" == "mgz_mgh" ]]; then
        local stem; stem=$(basename "${input_file%.*}")
        nii_input="${work_dir}/inputs/${stem}.nii.gz"
        to_nifti "$input_file" "$nii_input"
    else
        cp "$input_file" "${work_dir}/inputs/$(basename "$input_file")"
        nii_input="${work_dir}/inputs/$(basename "$input_file")"
    fi

    echo "[INFO]: Running mri_reface on $(basename "$nii_input")…"
    apptainer exec \
        --bind "${work_dir}:${work_dir}" \
        "${MRI_REFACE_SIF}" \
        run_mri_reface.sh \
            "${nii_input}" \
            "${work_dir}/outputs" \
            -imType  "${IMTYPE}" \
            -threads "${THREADS}"

    # Locate the refaced NIfTI (*_reface.nii or *_reface.nii.gz)
    local refaced_nii
    refaced_nii=$(find "${work_dir}/outputs" -maxdepth 1 -name "*_reface.nii*" | head -1)
    [[ -z "$refaced_nii" ]] && {
        echo "[ERROR]: mri_reface produced no *_reface.nii* in ${work_dir}/outputs" >&2; exit 1; }

    # Convert refaced NIfTI back to original format if needed
    if [[ "$fmt" == "mgz_mgh" ]]; then
        local orig_ext="${input_file##*.}"
        local stem_nii; stem_nii=$(basename "${refaced_nii%.nii.gz}"); stem_nii="${stem_nii%.nii}"
        local refaced_mgz="${work_dir}/outputs/${stem_nii}.${orig_ext}"
        from_nifti "$refaced_nii" "$refaced_mgz"
        REFACED_FILE="$refaced_mgz"
    else
        REFACED_FILE="$refaced_nii"
    fi
    echo "[INFO]: Refaced file: ${REFACED_FILE}"
}

tar_compress_flag() {
    local lower="${1,,}"
    [[ "$lower" == *.tar.gz  || "$lower" == *.tgz  ]] && { echo "z"; return; }
    [[ "$lower" == *.tar.bz2 || "$lower" == *.tbz2 ]] && { echo "j"; return; }
    [[ "$lower" == *.tar.xz  || "$lower" == *.txz  ]] && { echo "J"; return; }
    echo ""
}

# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

if [[ -z "$ARCHIVE_MEMBERS" ]]; then

    # -----------------------------------------------------------------------
    # Regular file
    # -----------------------------------------------------------------------
    [[ ! -f "$FILEPATH" ]] && { echo "[ERROR]: File not found: ${FILEPATH}" >&2; exit 1; }

    work_dir=$(mktemp -d "${SCRATCH_DIR}/mri_reface_work_XXXXXX")
    trap "echo '[INFO]: Cleaning work dir'; rm -rf '${work_dir}'" EXIT

    reface_one "$FILEPATH" "$work_dir"

    mkdir -p "$OUTPUT_DIR"
    cp "${work_dir}/outputs/"* "${OUTPUT_DIR}/"
    echo "[INFO]: Output written to: ${OUTPUT_DIR}"
    ls -lh "${OUTPUT_DIR}"

else

    # -----------------------------------------------------------------------
    # Archive — extract once, reface each member, repack once
    # -----------------------------------------------------------------------
    [[ ! -f "$FILEPATH" ]] && { echo "[ERROR]: Archive not found: ${FILEPATH}" >&2; exit 1; }

    IFS=';' read -ra members <<< "$ARCHIVE_MEMBERS"
    echo "[INFO]: Processing ${#members[@]} member(s) from $(basename "$FILEPATH")"

    extract_dir=$(mktemp -d "${SCRATCH_DIR}/mri_reface_extract_XXXXXX")
    work_base=$(mktemp -d "${SCRATCH_DIR}/mri_reface_work_XXXXXX")
    trap "echo '[INFO]: Cleaning scratch dirs'; rm -rf '${extract_dir}' '${work_base}'" EXIT

    # Extract the full archive to scratch
    echo "[INFO]: Extracting archive ($(du -sh "$FILEPATH" | cut -f1) compressed)…"
    archive_lower="${FILEPATH,,}"
    if [[ "$archive_lower" == *.zip ]]; then
        unzip -q "$FILEPATH" -d "$extract_dir"
    else
        cflag=$(tar_compress_flag "$FILEPATH")
        tar -x${cflag}f "$FILEPATH" -C "$extract_dir"
    fi

    # Reface each member and replace it in the extraction tree
    for member in "${members[@]}"; do
        echo "[INFO]: --- Refacing: ${member} ---"
        extracted_file="${extract_dir}/${member}"
        [[ ! -f "$extracted_file" ]] && {
            echo "[ERROR]: Member not found after extraction: ${extracted_file}" >&2; exit 1; }

        member_work="${work_base}/$(echo "$member" | tr '/' '_')"
        mkdir -p "$member_work"

        reface_one "$extracted_file" "$member_work"

        # Replace the member in the extraction tree with the refaced version.
        # REFACED_FILE has the same format as the original member (MGZ→MGZ etc.)
        cp "$REFACED_FILE" "$extracted_file"
        echo "[INFO]: Replaced ${member} with refaced version"

        # Copy all mri_reface outputs (QC renders etc.) to OUTPUT_DIR as well
        mkdir -p "${OUTPUT_DIR}/$(dirname "$member")"
        cp "${member_work}/outputs/"* "${OUTPUT_DIR}/$(dirname "$member")/" 2>/dev/null || true
    done

    # Repack the modified extraction tree as a new archive
    mkdir -p "$OUTPUT_DIR"
    output_archive="${OUTPUT_DIR}/$(basename "$FILEPATH")"
    echo "[INFO]: Repacking → ${output_archive}…"
    if [[ "$archive_lower" == *.zip ]]; then
        (cd "$extract_dir" && zip -qr "$output_archive" .)
    else
        cflag=$(tar_compress_flag "$FILEPATH")
        tar -c${cflag}f "$output_archive" -C "$extract_dir" .
    fi
    echo "[INFO]: Repacked archive: ${output_archive}  ($(du -sh "$output_archive" | cut -f1))"

fi

echo "[INFO]: Job complete."


# reface_job.sbatch
#
# Reface a single NIfTI file using mri_reface (Apptainer).
# Called by reface_from_csv.sh — do not submit directly.
#
# Positional arguments (passed by reface_from_csv.sh):
#   $1  FILEPATH        Absolute path to the NIfTI file, or to the archive that
#                       contains it (when ARCHIVE_MEMBER is non-empty).
#   $2  ARCHIVE_MEMBER  Path of the target file inside the archive.
#                       Empty string for non-archived files.
#   $3  OUTPUT_DIR      Directory where mri_reface writes its output.
#   $4  SCRATCH_DIR     Scratch base used for temp extraction and mri_reface work.
#   $5  MRI_REFACE_SIF  Apptainer SIF built from the mri_reface Docker image.
#   $6  IMTYPE          mri_reface -imType value (AUTO|T1|T2|FLAIR|FDG|TAU|…).
#
# Behaviour:
#   • For a regular file:  input is copied to a scratch work dir, refaced in
#     the container, and output is written to OUTPUT_DIR.
#   • For an archive member: the specific member is extracted from the archive
#     into a scratch extract dir, then the same reface logic runs.  Both the
#     extract dir and the work dir are cleaned up on exit regardless of success.

set -euo pipefail

FILEPATH="${1:?FILEPATH required}"
ARCHIVE_MEMBER="${2}"          # may be empty
OUTPUT_DIR="${3:?OUTPUT_DIR required}"
SCRATCH_DIR="${4:?SCRATCH_DIR required}"
MRI_REFACE_SIF="${5:?MRI_REFACE_SIF required}"
IMTYPE="${6:-AUTO}"
THREADS="${SLURM_CPUS_PER_TASK:-4}"

echo "[INFO]: filepath:       ${FILEPATH}"
echo "[INFO]: archive_member: ${ARCHIVE_MEMBER:-<none>}"
echo "[INFO]: output_dir:     ${OUTPUT_DIR}"
echo "[INFO]: scratch_dir:    ${SCRATCH_DIR}"
echo "[INFO]: imType:         ${IMTYPE}"
echo "[INFO]: threads:        ${THREADS}"

# ---------------------------------------------------------------------------
# Helper: run mri_reface on a single NIfTI file via Apptainer
# ---------------------------------------------------------------------------
run_reface() {
    local input_nii="$1"
    local final_output_dir="$2"

    # Create an isolated work dir on scratch (inputs/ + outputs/ subdirs).
    # The entire tree is bind-mounted into the container so nothing spills
    # outside it.  The trap removes it even on failure.
    local work_dir
    work_dir=$(mktemp -d "${SCRATCH_DIR}/mri_reface_work_XXXXXX")
    trap "echo '[INFO]: Cleaning up work dir ${work_dir}'; rm -rf '${work_dir}'" EXIT

    mkdir -p "${work_dir}/inputs" "${work_dir}/outputs"
    cp "${input_nii}" "${work_dir}/inputs/"

    local container_input="${work_dir}/inputs/$(basename "${input_nii}")"
    local container_output="${work_dir}/outputs"

    echo "[INFO]: Running mri_reface on $(basename "${input_nii}")…"
    apptainer exec \
        --bind "${work_dir}:${work_dir}" \
        "${MRI_REFACE_SIF}" \
        run_mri_reface.sh \
            "${container_input}" \
            "${container_output}" \
            -imType  "${IMTYPE}" \
            -threads "${THREADS}"

    # Copy outputs to the permanent destination
    mkdir -p "${final_output_dir}"
    cp -r "${work_dir}/outputs/." "${final_output_dir}/"
    echo "[INFO]: Output written to: ${final_output_dir}"
    echo "[INFO]: Output files:"
    ls -lh "${final_output_dir}"
}

# ---------------------------------------------------------------------------
# Helper: extract a single member from an archive into a directory
# Returns the path of the extracted file.
# ---------------------------------------------------------------------------
extract_member() {
    local archive="$1"
    local member="$2"
    local dest_dir="$3"

    mkdir -p "${dest_dir}/$(dirname "${member}")"

    local lower="${archive,,}"
    if [[ "$lower" == *.zip ]]; then
        unzip -q "$archive" "$member" -d "$dest_dir"
    else
        # tar handles .tar, .tar.gz/.tgz, .tar.bz2/.tbz2, .tar.xz/.txz
        tar -xf "$archive" -C "$dest_dir" "$member"
    fi

    echo "${dest_dir}/${member}"
}

# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------
if [[ -z "$ARCHIVE_MEMBER" ]]; then
    # ---- Regular file: reface directly ----
    if [[ ! -f "$FILEPATH" ]]; then
        echo "[ERROR]: Input file not found: ${FILEPATH}" >&2
        exit 1
    fi
    run_reface "$FILEPATH" "$OUTPUT_DIR"

else
    # ---- Archive member: extract to scratch, then reface ----
    if [[ ! -f "$FILEPATH" ]]; then
        echo "[ERROR]: Archive not found: ${FILEPATH}" >&2
        exit 1
    fi

    # Create an extract dir on scratch; it is removed on exit via trap.
    extract_dir=$(mktemp -d "${SCRATCH_DIR}/mri_reface_extract_XXXXXX")
    # Note: the work dir trap inside run_reface will be registered later;
    # this trap removes the extract dir.  Both traps fire on EXIT.
    trap "echo '[INFO]: Cleaning up extract dir ${extract_dir}'; rm -rf '${extract_dir}'" EXIT

    echo "[INFO]: Extracting '${ARCHIVE_MEMBER}' from archive…"
    extracted_file=$(extract_member "$FILEPATH" "$ARCHIVE_MEMBER" "$extract_dir")

    if [[ ! -f "$extracted_file" ]]; then
        echo "[ERROR]: Extraction produced no file at expected path: ${extracted_file}" >&2
        exit 1
    fi
    echo "[INFO]: Extracted to: ${extracted_file}"

    run_reface "$extracted_file" "$OUTPUT_DIR"
fi

echo "[INFO]: Job complete."
