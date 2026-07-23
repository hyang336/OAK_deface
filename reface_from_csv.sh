#!/bin/bash
# reface_from_csv.sh
#
# Read a find_anat.py CSV and submit sbatch refacing jobs via reface_job.sbatch.
#
# Supported formats: NIfTI (.nii/.nii.gz), MGH (.mgh), MGZ (.mgz).
# MGZ/MGH requires the FreeSurfer Apptainer SIF (--fs_img) for mri_convert.
#
# Archive handling: all members from the same archive that need refacing are
# batched into a SINGLE job so the archive is extracted and repacked only once,
# avoiding concurrent repack races.  The repacked archive is written to
# OUTPUT_DIR/archive_parent_dir/archive_basename.
#
# Rows with unsupported formats (MINC, NRRD, ANALYZE) are logged and skipped.
#
# PREREQUISITE — build the Apptainer SIF once from the Docker image:
#   apptainer build ~/G_home/mri_reface_docker/mri_reface.sif \
#       docker-archive://~/G_home/mri_reface_docker/mri_reface_docker_image
#
# USAGE:
#   bash reface_from_csv.sh \
#       --csv /scratch/users/hyang336/find_anat_results/annakhaz.csv \
#       --output_dir /scratch/users/hyang336/refaced/annakhaz \
#       --mri_reface_sif ~/G_home/mri_reface_docker/mri_reface.sif \
#       [--fs_img /home/groups/awagner/containers/freesurfer_8.2.0.sif]
#
# OPTIONS:
#   --csv FILE             find_anat CSV produced by find_anat.py / find_anat.sbatch
#   --output_dir DIR       root directory for refaced output (created if absent)
#   --mri_reface_sif FILE  Apptainer SIF built from the mri_reface Docker image
#   --fs_img FILE          FreeSurfer Apptainer SIF for MGZ/MGH mri_convert
#                          [default: /home/groups/awagner/containers/freesurfer_8.2.0.sif]
#   --scratch_dir DIR      scratch base for temp extraction / mri_reface work
#                          [default: $SCRATCH if set, else /scratch/users/hyang336]
#   --imType TYPE          mri_reface -imType value (T1|T2|PD|T2ST|FLAIR|FDG|PIB|
#                          FBP|TAU|CT|AUTO)  [default: AUTO]
#   --previous_job_id ID   SLURM job ID to depend on (afterok) before submitting
#   --help, -h             Show this help and exit

set -euo pipefail

# ---- Defaults ----------------------------------------------------------------
csv_file=""
output_dir=""
mri_reface_sif=""
fs_img="/home/groups/awagner/containers/freesurfer_8.2.0.sif"
scratch_dir="${SCRATCH:-/scratch/users/hyang336}"
imtype="AUTO"
previous_job_id=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --csv)             csv_file="$2";        shift 2 ;;
        --output_dir)      output_dir="$2";      shift 2 ;;
        --mri_reface_sif)  mri_reface_sif="$2";  shift 2 ;;
        --fs_img)          fs_img="$2";          shift 2 ;;
        --scratch_dir)     scratch_dir="$2";     shift 2 ;;
        --imType)          imtype="$2";          shift 2 ;;
        --previous_job_id) previous_job_id="$2"; shift 2 ;;
        --help|-h)
            sed -n '/^# USAGE/,/^[^#]/{ /^#/{ s/^# \{0,1\}//; p }; /^[^#]/q }' "$0"
            exit 0 ;;
        *) echo "[ERROR]: Unknown option: $1" >&2; exit 1 ;;
    esac
done

# ---- Validate ----------------------------------------------------------------
[[ -z "$csv_file" ]]         && { echo "[ERROR]: --csv required" >&2; exit 1; }
[[ -z "$output_dir" ]]       && { echo "[ERROR]: --output_dir required" >&2; exit 1; }
[[ -z "$mri_reface_sif" ]]   && { echo "[ERROR]: --mri_reface_sif required" >&2; exit 1; }
[[ ! -f "$csv_file" ]]       && { echo "[ERROR]: CSV not found: $csv_file" >&2; exit 1; }
[[ ! -f "$mri_reface_sif" ]] && { echo "[ERROR]: SIF not found: $mri_reface_sif" >&2; exit 1; }

mkdir -p "$output_dir"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

dep_flag=""
[[ -n "$previous_job_id" ]] && dep_flag="--dependency=afterok:${previous_job_id}"

echo "[INFO]: CSV:        $csv_file"
echo "[INFO]: Output dir: $output_dir"
echo "[INFO]: SIF:        $mri_reface_sif"
echo "[INFO]: FS SIF:     ${fs_img:-<not set>}"
echo "[INFO]: Scratch:    $scratch_dir"
echo "[INFO]: imType:     $imtype"

job_ids=()
n_skip=0

# ---- Parse CSV and submit jobs -----------------------------------------------
# Python parses the CSV and emits one record per job using ASCII delimiters:
#   FS (\x1c) separates fields, RS (\x1d) terminates each record
# Record format: ACTION FS root FS rel_path FS members FS type FS format
#   ACTION  = JOB | SKIP
#   members = semicolon-joined list of archive member paths (empty for regular files)
# Archive members from the same archive are grouped into a single record so the
# archive is extracted and repacked exactly once per job.
while IFS=$'\x1c' read -r -d $'\x1d' action root rel_path archive_members rec_type rec_format; do

    filepath="${root%/}/${rel_path}"

    if [[ "$action" == "SKIP" ]]; then
        echo "[SKIP]: format=${rec_format} not supported — ${filepath}${archive_members:+ (${archive_members})}"
        n_skip=$((n_skip + 1))
        continue
    fi

    # ------------------------------------------------------------------
    # Output directory:
    #   regular file  → output_dir/dirname(rel_path)
    #                   mri_reface writes here directly
    #   archive file  → output_dir/dirname(rel_path)
    #                   the repacked archive lands here as basename(rel_path)
    # ------------------------------------------------------------------
    file_output_dir="${output_dir}/$(dirname "${rel_path}")"

    # Derive a safe SLURM job name from the archive or file basename
    _base="$(basename "${rel_path%.gz}")"
    _base="${_base%.nii}.${_base##*.}"  # keep extension for clarity
    job_name="reface_$(echo "${_base}" | tr ' /.' '___' | cut -c1-40)"

    job_id=$(sbatch \
        $dep_flag \
        --job-name="${job_name}" \
        --output="/home/users/hyang336/jobs/${job_name}_%j.out" \
        --error="/home/users/hyang336/jobs/${job_name}_%j.err" \
        "${SCRIPT_DIR}/reface_job.sbatch" \
        "$filepath" \
        "$archive_members" \
        "$file_output_dir" \
        "$scratch_dir" \
        "$mri_reface_sif" \
        "$imtype" \
        "${fs_img}" \
        | awk '{print $4}')

    echo "[INFO]: Submitted ${job_id} (${job_name}) — ${filepath}${archive_members:+ members=[${archive_members}]}"
    job_ids+=("${job_id}")

done < <(python3 - "$csv_file" << 'PYEOF'
import csv, sys, collections

FS = '\x1c'   # ASCII field separator
RS = '\x1d'   # ASCII record separator

ACTIONABLE        = {'structural_high', 'structural_medium', 'pet_high', 'pet_medium'}
# NIfTI: direct input to mri_reface.
# MGH / MGZ: converted via mri_convert (FreeSurfer) before/after reface.
SUPPORTED_FORMATS = {'NIfTI', 'MGH', 'MGZ'}

regular  = []  # list of (action, root, rel, type, fmt)
archives = collections.defaultdict(list)  # (root, rel) -> [(action, t, fmt, member), ...]

with open(sys.argv[1], newline='') as f:
    reader = csv.DictReader(f)
    for row in reader:
        t    = row.get('Type',          '').strip()
        fmt  = row.get('Format',        '').strip()
        root = row.get('Root',          '').strip()
        rel  = row.get('RelativePath',  '').strip()
        mem  = row.get('ArchiveMember', '').strip()

        if t not in ACTIONABLE:
            continue

        action = 'JOB' if fmt in SUPPORTED_FORMATS else 'SKIP'

        if mem:
            archives[(root, rel)].append((action, t, fmt, mem))
        else:
            regular.append((action, root, rel, t, fmt))

# Emit regular files
for (action, root, rel, t, fmt) in regular:
    sys.stdout.write(f"{action}{FS}{root}{FS}{rel}{FS}{FS}{t}{FS}{fmt}{RS}")

# Emit one record per archive, grouping all members into a single job.
# Skip-only members are reported as SKIP; if any member is a JOB the whole
# archive record is JOB (with only the job-eligible members listed).
for (root, rel), members in archives.items():
    job_members  = [(t, fmt, m) for (a, t, fmt, m) in members if a == 'JOB']
    skip_members = [(fmt, m)    for (a, t, fmt, m) in members if a == 'SKIP']

    for (fmt, m) in skip_members:
        sys.stdout.write(f"SKIP{FS}{root}{FS}{rel}{FS}{m}{FS}{FS}{fmt}{RS}")

    if job_members:
        members_str = ';'.join(m for (_, _, m) in job_members)
        t0, fmt0, _ = job_members[0]
        sys.stdout.write(f"JOB{FS}{root}{FS}{rel}{FS}{members_str}{FS}{t0}{FS}{fmt0}{RS}")
PYEOF
)

echo ""
echo "[INFO]: Submitted ${#job_ids[@]} refacing jobs, skipped ${n_skip} (unsupported format)."
[[ ${#job_ids[@]} -gt 0 ]] && echo "[INFO]: Job IDs: ${job_ids[*]}"
