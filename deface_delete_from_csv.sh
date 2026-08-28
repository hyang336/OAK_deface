#!/bin/bash
# deface_delete_from_csv.sh
#
# Read a find_anat.py CSV and submit SLURM jobs to:
#   - DEFACE anatomical NIfTI (.nii/.nii.gz) files IN PLACE using mri_reface
#   - DELETE anatomical non-NIfTI files (MINC, MGH, MGZ, NRRD, ANALYZE) from disk
#
# Archive handling: each archive is processed as a 3-stage pipeline:
#   1) extract once into scratch and build manifests
#   2) deface archived NIfTI members via a SLURM array job
#   3) delete archived non-NIfTI members, repack once, replace original archive
# This avoids concurrent repack races while letting defacing scale across
# multiple SLURM tasks instead of many background MATLAB Runtime processes
# inside one shell.
#
# PREREQUISITE — build the Apptainer SIF once from the Docker image:
#   apptainer build ~/G_home/mri_reface_docker/mri_reface.sif \
#       docker-archive://~/G_home/mri_reface_docker/mri_reface_docker_image
#
# USAGE:
#   bash deface_delete_from_csv.sh \
#       --csv /scratch/users/hyang336/find_anat_results/annakhaz.csv \
#       --mri_reface_sif ~/G_home/mri_reface_docker/mri_reface.sif \
#       [--working_dir /scratch/users/hyang336/deface_working] \
#       [--scratch_dir /scratch/users/hyang336/deface_tmp] \
#       [--imType AUTO] \
#       [--parallel_jobs 0] \
#       [--array_chunk_size 4] \
#       [--time 24:00:00] \
#       [--rsync_time 24:00:00] \
#       [--previous_job_id 12345] \
#       [--dry_run]
#
# OPTIONS:
#   --csv FILE             find_anat CSV produced by find_anat.py / find_anat.sbatch
#   --mri_reface_sif FILE  Apptainer SIF built from the mri_reface Docker image
#   --working_dir DIR      If provided, rsync each unique Root directory from the
#                          CSV to this directory before processing.  All deface /
#                          delete jobs operate on the copies; the originals are
#                          left untouched.  One rsync sbatch job is submitted per
#                          unique root, and all deface/delete jobs depend on them.
#                          If omitted, files are modified in place — ensure you
#                          have backups.
#   --scratch_dir DIR      Scratch base for temp extraction / mri_reface work
#                          [default: /scratch/users/hyang336/deface_tmp]
#   --imType TYPE          mri_reface -imType value (T1|T2|PD|T2ST|FLAIR|FDG|PIB|
#                          FBP|TAU|CT|AUTO)  [default: AUTO]
#   --parallel_jobs N      Max number of concurrent archive-array tasks. 0 means
#                          auto [default: 0 -> 16 concurrent tasks]
#   --array_chunk_size N   Number of deface members handled per array task.
#                          Helps keep array size below cluster MaxArraySize.
#                          [default: 4]
#   --time HH:MM:SS        Wall-time limit for each deface/delete job.
#                          Estimate ~10 min per NIfTI scan to be defaced.
#                          [default: 24:00:00]
#   --rsync_time HH:MM:SS  Wall-time limit for the optional per-root rsync
#                          staging jobs when --working_dir is used.
#                          [default: same as --time]
#   --previous_job_id ID   SLURM job ID to depend on (afterok) before submitting
#   --dry_run              Print what would be submitted without running sbatch
#   --help, -h             Show this help and exit

set -euo pipefail

# ---- Defaults ----------------------------------------------------------------
csv_file=""
mri_reface_sif=""
working_dir=""        # if set, rsync roots here and operate on the copies
scratch_dir="/scratch/users/hyang336/deface_tmp"
imtype="AUTO"
parallel_jobs="0"
array_chunk_size="4"
time_limit="24:00:00"
rsync_time=""
previous_job_id=""
dry_run=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --csv)             csv_file="$2";        shift 2 ;;
        --mri_reface_sif)  mri_reface_sif="$2";  shift 2 ;;
        --working_dir)     working_dir="$2";     shift 2 ;;
        --scratch_dir)     scratch_dir="$2";     shift 2 ;;
        --imType)          imtype="$2";          shift 2 ;;
        --parallel_jobs)   parallel_jobs="$2";   shift 2 ;;
        --array_chunk_size) array_chunk_size="$2"; shift 2 ;;
        --time)            time_limit="$2";      shift 2 ;;
        --rsync_time)      rsync_time="$2";      shift 2 ;;
        --previous_job_id) previous_job_id="$2"; shift 2 ;;
        --dry_run)         dry_run=true;         shift ;;
        --help|-h)
            sed -n '/^# USAGE/,/^[^#]/{ /^#/{ s/^# \{0,1\}//; p }; /^[^#]/q }' "$0"
            exit 0 ;;
        *) echo "[ERROR]: Unknown option: $1" >&2; exit 1 ;;
    esac
done

mkdir -p "$scratch_dir"

# ---- Validate ----------------------------------------------------------------
[[ -z "$csv_file" ]]         && { echo "[ERROR]: --csv required" >&2; exit 1; }
[[ -z "$mri_reface_sif" ]]   && { echo "[ERROR]: --mri_reface_sif required" >&2; exit 1; }
[[ ! -f "$csv_file" ]]       && { echo "[ERROR]: CSV not found: $csv_file" >&2; exit 1; }
[[ ! -f "$mri_reface_sif" ]] && { echo "[ERROR]: SIF not found: $mri_reface_sif" >&2; exit 1; }
[[ ! "$parallel_jobs" =~ ^[0-9]+$ ]] && {
    echo "[ERROR]: --parallel_jobs must be an integer >= 0 (got '$parallel_jobs')." >&2
    echo "        If this happened while adding --dry_run, ensure there is a space before it." >&2
    exit 1
}
[[ ! "$array_chunk_size" =~ ^[0-9]+$ || "$array_chunk_size" == "0" ]] && {
    echo "[ERROR]: --array_chunk_size must be an integer >= 1 (got '$array_chunk_size')." >&2
    exit 1
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "$rsync_time" ]]; then
    rsync_time="$time_limit"
fi

dep_flag=""
[[ -n "$previous_job_id" ]] && dep_flag="--dependency=afterok:${previous_job_id}"

echo "[INFO]: CSV:         $csv_file"
echo "[INFO]: SIF:         $mri_reface_sif"
[[ -n "$working_dir" ]] && echo "[INFO]: Working dir: $working_dir  (copy-first mode — originals untouched)"
echo "[INFO]: Scratch:     $scratch_dir"
echo "[INFO]: imType:      $imtype"
echo "[INFO]: parallel:    $parallel_jobs"
echo "[INFO]: chunk_size:  $array_chunk_size"
[[ -n "$working_dir" ]] && echo "[INFO]: rsync_time:   $rsync_time"
$dry_run && echo "[INFO]: DRY RUN — no jobs will be submitted"

count_member_entries() {
    python3 - "$1" <<'PYEOF'
import pathlib, sys
text = pathlib.Path(sys.argv[1]).read_text().strip()
if not text:
    print(0)
else:
    print(sum(1 for entry in text.split(';') if entry))
PYEOF
}

archive_array_concurrency() {
    local requested="$1"
    if [[ "$requested" =~ ^[0-9]+$ ]] && (( requested > 0 )); then
        echo "$requested"
    else
        echo "16"
    fi
}

# ---- Optional: rsync each unique root to working_dir before processing ------
# One rsync sbatch job is submitted per unique root.  All deface/delete jobs
# are given an afterok dependency on the rsync jobs so they only start once
# the full copy is available.  An associative array maps each original root
# path to its rsync job ID for logging.
declare -A root_rsync_jid

if [[ -n "$working_dir" ]]; then
    mkdir -p "$working_dir"

    # Extract all unique actionable roots from the CSV
    mapfile -t unique_roots < <(python3 - "$csv_file" << 'PYEOF'
import csv, sys
ACTIONABLE = {'structural_high', 'structural_medium', 'pet_high', 'pet_medium'}
roots = set()
with open(sys.argv[1], newline='') as f:
    for row in csv.DictReader(f):
        if row.get('Type', '').strip() in ACTIONABLE:
            r = row.get('Root', '').strip()
            if r:
                roots.add(r)
for r in sorted(roots):
    print(r)
PYEOF
    )

    rsync_dep_ids=()
    for root_path in "${unique_roots[@]:-}"; do
        [[ -z "$root_path" ]] && continue
        root_basename=$(basename "$root_path")
        dest="${working_dir%/}/${root_basename}"
        mkdir -p "$dest"

        if $dry_run; then
            echo "[DRY-RUN]: Would rsync ${root_path}/ → ${dest}/"
            continue
        fi

        rsync_jid=$(sbatch \
            --job-name="rsync_${root_basename}" \
            --output="/home/users/hyang336/jobs/rsync_${root_basename}_%j.out" \
            --error="/home/users/hyang336/jobs/rsync_${root_basename}_%j.err" \
            --time="${rsync_time}" --mem=4G --cpus-per-task=2 \
            --partition=awagner,hns,normal \
            --wrap="rsync -a --info=progress2 '${root_path}/' '${dest}/'" \
            | awk '{print $4}')
        echo "[INFO]: Submitted rsync job ${rsync_jid}: ${root_path}/ → ${dest}/"
        root_rsync_jid["$root_path"]="$rsync_jid"
        rsync_dep_ids+=("$rsync_jid")
    done

    # Extend dep_flag so every deface/delete job waits for all rsync jobs
    if [[ ${#rsync_dep_ids[@]} -gt 0 ]]; then
        rsync_after="afterok:$(IFS=':'; echo "${rsync_dep_ids[*]}")"
        if [[ -n "$dep_flag" ]]; then
            dep_flag="${dep_flag},${rsync_after}"
        else
            dep_flag="--dependency=${rsync_after}"
        fi
    fi
fi

job_ids=()
n_skip=0

# Regular (non-archive) files are grouped by root so we can submit one deface
# array per root and one delete batch for the whole CSV.
declare -A regular_deface_lists=()
declare -a regular_deface_roots=()
global_delete_list=""
csv_tag="$(basename "${csv_file%.csv}")"

# ---- Parse CSV and submit jobs -----------------------------------------------
# Python parses the CSV and emits one record per job using ASCII delimiters:
#   FS (\x1c) separates fields, RS (\x1d) terminates each record
# Record format (7 fields):
#   ACTION FS root FS rel_path FS deface_members FS delete_members FS type FS format
#   ACTION         = DEFACE | DELETE | ARCHIVE | SKIP
#   deface_members = semicolon-joined archive member paths to deface (empty for plain files)
#   delete_members = semicolon-joined archive member paths to delete (empty for plain files)
# Archive members from the same archive are grouped into a single record.
while IFS=$'\x1c' read -r -d $'\x1d' action root rel_path deface_members delete_members rec_type rec_format file_imtype; do

    # Resolve filepath: use working_dir copy when --working_dir was provided
    if [[ -n "$working_dir" ]]; then
        root_basename=$(basename "$root")
        filepath="${working_dir%/}/${root_basename}/${rel_path}"
    else
        filepath="${root%/}/${rel_path}"
    fi

    if [[ "$action" == "SKIP" ]]; then
        echo "[SKIP]: type=${rec_type} format=${rec_format} — ${filepath}"
        n_skip=$((n_skip + 1))
        continue
    fi

    # ARCHIVE member lists can be very long (one entry per archive member) and
    # exceed the OS ARG_MAX limit when passed directly on the command line.
    # Write them to files in scratch and pass the file paths instead.
    if [[ "$action" == "ARCHIVE" ]]; then
        _base="$(basename "${rel_path%.gz}")"
        _base="${_base%.tar}"
        job_name="ddel_$(echo "${_base}" | tr ' /.' '___' | cut -c1-38)"

        if $dry_run; then
            echo "[DRY-RUN]: ${job_name}  action=${action}"
            echo "           file:   ${filepath}"
            [[ -n "$deface_members" ]] && echo "           deface: ${deface_members}"
            [[ -n "$delete_members" ]] && echo "           delete: ${delete_members}"
            continue
        fi

        arg_deface="$deface_members"
        arg_delete="$delete_members"
        arg_deface="${scratch_dir}/${job_name}_deface.txt"
        arg_delete="${scratch_dir}/${job_name}_delete.txt"
        printf '%s' "$deface_members" > "$arg_deface"
        printf '%s' "$delete_members" > "$arg_delete"

        archive_state_dir=$(mktemp -d "${scratch_dir}/${job_name}_state_XXXXXX")
        deface_count=$(count_member_entries "$arg_deface")
        array_limit=$(archive_array_concurrency "$parallel_jobs")
        task_count=$(( (deface_count + array_chunk_size - 1) / array_chunk_size ))

        extract_jid=$(sbatch \
            $dep_flag \
            --job-name="${job_name}_x" \
            --time="${time_limit}" \
            --output="/home/users/hyang336/jobs/${job_name}_extract_%j.out" \
            --error="/home/users/hyang336/jobs/${job_name}_extract_%j.err" \
            "${SCRIPT_DIR}/deface_archive_extract.sbatch" \
            "$filepath" \
            "$arg_deface" \
            "$arg_delete" \
            "$archive_state_dir" \
            | awk '{print $4}')

        final_dep="afterok:${extract_jid}"
        if (( deface_count > 0 )); then
            worker_jid=$(sbatch \
                --dependency="afterok:${extract_jid}" \
                --job-name="${job_name}_a" \
                --time="${time_limit}" \
                --array="0-$((task_count - 1))%${array_limit}" \
                --output="/home/users/hyang336/jobs/${job_name}_array_%A_%a.out" \
                --error="/home/users/hyang336/jobs/${job_name}_array_%A_%a.err" \
                "${SCRIPT_DIR}/deface_archive_worker.sbatch" \
                "$archive_state_dir" \
                "$mri_reface_sif" \
                "$array_chunk_size" \
                | awk '{print $4}')
            final_dep="afterok:${worker_jid}"
            job_ids+=("${worker_jid}")
        else
            worker_jid=""
        fi

        finalize_jid=$(sbatch \
            --dependency="${final_dep}" \
            --job-name="${job_name}_f" \
            --time="${time_limit}" \
            --output="/home/users/hyang336/jobs/${job_name}_finalize_%j.out" \
            --error="/home/users/hyang336/jobs/${job_name}_finalize_%j.err" \
            "${SCRIPT_DIR}/deface_archive_finalize.sbatch" \
            "$filepath" \
            "$archive_state_dir" \
            "$arg_delete" \
            | awk '{print $4}')

        echo "[INFO]: Submitted ${extract_jid} (${job_name}_x) — extract/archive stage  ${filepath}"
        [[ -n "$worker_jid" ]] && echo "[INFO]: Submitted ${worker_jid} (${job_name}_a) — array deface stage (${task_count} tasks x chunk ${array_chunk_size}, %${array_limit})"
        echo "[INFO]: Submitted ${finalize_jid} (${job_name}_f) — finalize/repack stage"
        [[ -n "$deface_members" ]] && echo "        deface: ${deface_members}"
        [[ -n "$delete_members" ]] && echo "        delete: ${delete_members}"
        job_ids+=("${extract_jid}" "${finalize_jid}")
        continue
    fi

    # Regular (non-archive) files are grouped by root.
    root_slug="$(echo "${root}" | tr ' /.' '___' | cut -c1-60)"
    root_job_name="ddel_${root_slug}"

    if [[ "$action" == "DEFACE" ]]; then
        if [[ -z "${regular_deface_lists[$root]:-}" ]]; then
            regular_deface_lists["$root"]="${scratch_dir}/${root_job_name}_deface_list.txt"
            : > "${regular_deface_lists[$root]}"
            regular_deface_roots+=("$root")
        fi
        printf '%s\t%s\n' "$filepath" "${file_imtype:-$imtype}" >> "${regular_deface_lists[$root]}"
        continue
    fi

    if [[ "$action" == "DELETE" ]]; then
        if [[ -z "$global_delete_list" ]]; then
            global_delete_list="${scratch_dir}/${csv_tag}_delete_all.txt"
            : > "$global_delete_list"
        fi
        printf '%s\n' "$filepath" >> "$global_delete_list"
        continue
    fi

    echo "[ERROR]: Unsupported action for regular file: ${action}" >&2
    exit 1

done < <(python3 - "$csv_file" << 'PYEOF'
import csv, sys, collections, os, re

FS = '\x1c'   # ASCII field separator
RS = '\x1d'   # ASCII record separator

# Row types that require de-identification action
ACTIONABLE    = {'structural_high', 'structural_medium', 'pet_high', 'pet_medium'}
# Only NIfTI is defaced; all other recognised formats are deleted
NIFTI_FORMATS = {'NIfTI'}

def _get_imtype(basename, confidence):
    """Map a filename to an mri_reface -imType value.

    Returns one of: T1 | T2 | PD | T2ST | FLAIR | FDG | PIB | FBP | TAU
    Returns '' (empty) if the modality cannot be determined — the caller
    must route such files to DELETE instead of DEFACE.  AUTO is never
    returned: mri_reface AUTO simply re-checks the filename for recognised
    suffixes and errors if none match, so it provides no benefit over our
    own detection.
    """
    b = basename.lower()
    if re.search(r'_t2starw|_t2star', b):                     return 'T2ST'
    if re.search(r'_flair(?:\.|$)', b):                       return 'FLAIR'
    if re.search(r'_pdw(?:\.|$)', b):                         return 'PD'
    if re.search(r'_t1w|_t1rho|_inplanet1'
                 r'|mprage|mp2rage|memp2rage|memprage'
                 r'|\bspgr\b|\bflash\b|\bgre\b|\bbravo\b|ir-fspgr', b):
        return 'T1'
    if re.search(r'_t2w|_inplanet2', b):                      return 'T2'
    if confidence in ('pet_high', 'pet_medium'):
        if re.search(r'\bfdg\b', b):                          return 'FDG'
        if re.search(r'\bpib\b', b):                          return 'PIB'
        if re.search(r'florbetapir|florbetaben|flutemetamol|\bav.?45\b', b):
            return 'FBP'
        if re.search(r'flortaucipir|mk.?6240|av.?1451', b):   return 'TAU'
    if re.search(r'\bt1\b', b):                               return 'T1'
    if re.search(r'\bt2\b', b):                               return 'T2'
    if re.search(r'\bflair\b', b):                            return 'FLAIR'
    if re.search(r'\bpd\b', b):                               return 'PD'
    return ''  # unknown — route to DELETE

regular  = []   # list of (action, root, rel, t, fmt, imtype)
archives = collections.defaultdict(lambda: {'deface': [], 'delete': []})

with open(sys.argv[1], newline='') as f:
    reader = csv.DictReader(f)
    for row in reader:
        t      = row.get('Type',          '').strip()
        fmt    = row.get('Format',        '').strip()
        root   = row.get('Root',          '').strip()
        rel    = row.get('RelativePath',  '').strip()
        mem    = row.get('ArchiveMember', '').strip()
        # Use ImType from CSV if present and specific; otherwise derive from filename
        imtype = row.get('ImType', '').strip()
        if not imtype or imtype == 'AUTO':
            fname  = os.path.basename(mem if mem else rel)
            imtype = _get_imtype(fname, t)

        if t not in ACTIONABLE:
            continue

        # Route to DEFACE only when the format is NIfTI AND we know a
        # supported mri_reface imType.  Anything else (non-NIfTI format, or
        # NIfTI whose modality we cannot determine) goes to DELETE — passing
        # AUTO to mri_reface is not reliable; it just checks the filename.
        action = 'DEFACE' if (fmt in NIFTI_FORMATS and imtype) else 'DELETE'

        if mem:
            if action == 'DEFACE':
                # Encode imtype alongside the member path: "path/to/file.nii.gz|T1"
                archives[(root, rel)]['deface'].append(f"{mem}|{imtype}")
            else:
                archives[(root, rel)]['delete'].append(mem)
        else:
            regular.append((action, root, rel, t, fmt, imtype))

# Emit one record per regular file (8 fields)
for (action, root, rel, t, fmt, imtype) in regular:
    sys.stdout.write(f"{action}{FS}{root}{FS}{rel}{FS}{FS}{FS}{t}{FS}{fmt}{FS}{imtype}{RS}")

# Emit one record per archive (all members batched into a single job)
for (root, rel), ops in archives.items():
    deface_members = ';'.join(ops['deface'])  # each entry encoded as "path|imtype"
    delete_members = ';'.join(ops['delete'])
    if not deface_members and not delete_members:
        continue
    sys.stdout.write(
        f"ARCHIVE{FS}{root}{FS}{rel}{FS}{deface_members}{FS}{delete_members}{FS}{FS}{FS}{RS}"
    )
PYEOF
)

# Submit grouped regular-file jobs after the CSV has been fully parsed.
regular_deface_jids=()
if [[ ${#regular_deface_roots[@]} -gt 0 ]]; then
    for root_path in "${regular_deface_roots[@]}"; do
        list_file="${regular_deface_lists[$root_path]:-}"
        [[ -n "$list_file" && -s "$list_file" ]] || continue

        deface_count=$(grep -cve '^[[:space:]]*$' "$list_file" || true)
        (( deface_count == 0 )) && continue

        root_slug="$(echo "${root_path}" | tr ' /.' '___' | cut -c1-60)"
        root_job_name="ddel_${root_slug}"
        array_limit=$(archive_array_concurrency "$parallel_jobs")
        task_count=$(( (deface_count + array_chunk_size - 1) / array_chunk_size ))

        if $dry_run; then
            echo "[DRY-RUN]: Would submit ${root_job_name}_a as array (${task_count} tasks x chunk ${array_chunk_size}, %${array_limit})"
            echo "           deface list: ${list_file}"
            continue
        fi

        deface_jid=$(sbatch \
            $dep_flag \
            --job-name="${root_job_name}_a" \
            --time="${time_limit}" \
            --array="0-$((task_count - 1))%${array_limit}" \
            --output="/home/users/hyang336/jobs/${root_job_name}_array_%A_%a.out" \
            --error="/home/users/hyang336/jobs/${root_job_name}_array_%A_%a.err" \
            "${SCRIPT_DIR}/deface_delete_job.sbatch" \
            "$root_path" \
            "DEFACE_BATCH" \
            "$list_file" \
            "" \
            "$scratch_dir" \
            "$mri_reface_sif" \
            "$imtype" \
            "$array_chunk_size" \
            | awk '{print $4}')

        echo "[INFO]: Submitted ${deface_jid} (${root_job_name}_a) — deface array stage (${task_count} tasks x chunk ${array_chunk_size}, %${array_limit})"
        job_ids+=("${deface_jid}")
        regular_deface_jids+=("${deface_jid}")
    done
fi

if [[ -n "$global_delete_list" && -s "$global_delete_list" ]]; then
    if $dry_run; then
        echo "[DRY-RUN]: Would submit one DELETE_BATCH job for ${global_delete_list}"
    else
        delete_dep="$dep_flag"
        if [[ ${#regular_deface_jids[@]} -gt 0 ]]; then
            deface_after="afterok:$(IFS=':'; echo "${regular_deface_jids[*]}")"
            if [[ -n "$delete_dep" ]]; then
                delete_dep="${delete_dep},${deface_after}"
            else
                delete_dep="--dependency=${deface_after}"
            fi
        fi

        delete_jid=$(sbatch \
            $delete_dep \
            --job-name="ddel_${csv_tag}_delete" \
            --time="${time_limit}" \
            --output="/home/users/hyang336/jobs/ddel_${csv_tag}_delete_%j.out" \
            --error="/home/users/hyang336/jobs/ddel_${csv_tag}_delete_%j.err" \
            "${SCRIPT_DIR}/deface_delete_job.sbatch" \
            "$csv_file" \
            "DELETE_BATCH" \
            "" \
            "$global_delete_list" \
            "$scratch_dir" \
            "$mri_reface_sif" \
            "$imtype" \
            "1" \
            | awk '{print $4}')

        echo "[INFO]: Submitted ${delete_jid} (ddel_${csv_tag}_delete) — delete batch stage"
        job_ids+=("${delete_jid}")
    fi
fi

echo ""
echo "[INFO]: Submitted ${#job_ids[@]} job(s), skipped ${n_skip} (non-actionable type)."
[[ ${#job_ids[@]} -gt 0 ]] && echo "[INFO]: Job IDs: ${job_ids[*]}"

if [[ ${#job_ids[@]} -gt 0 ]]; then
    job_id_csv="$(IFS=','; echo "${job_ids[*]}")"
    status_jid=$(sbatch \
        --export="ALL,JOBIDS=${job_id_csv}" \
        --dependency="afterany:${job_id_csv}" \
        --job-name="ddel_${csv_tag}_status" \
        --time="00:30:00" \
        --output="/home/users/hyang336/jobs/ddel_${csv_tag}_status_%j.out" \
        --error="/home/users/hyang336/jobs/ddel_${csv_tag}_status_%j.err" \
        --wrap='set -euo pipefail; echo "[STATUS] Checking all monitored deface/delete jobs: ${JOBIDS}"; sacct -j "${JOBIDS}" --format=JobID,JobName,State,ExitCode --noheader || true; if sacct -j "${JOBIDS}" --format=State --noheader 2>/dev/null | grep -Eq "FAILED|CANCELLED|TIMEOUT|NODE_FAIL|PREEMPTED"; then echo "[STATUS] One or more deface/delete jobs failed or were canceled."; exit 1; else echo "[STATUS] No failed/canceled deface/delete jobs detected."; fi' \
        | awk '{print $4}')
    echo "[INFO]: Submitted ${status_jid} (ddel_${csv_tag}_status) — final job status summary"
fi
