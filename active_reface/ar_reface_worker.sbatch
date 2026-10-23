#!/bin/bash
#SBATCH --job-name=ar_reface
#SBATCH --time=4:00:00
#SBATCH --nodes=1
#SBATCH --mem=12G
#SBATCH --cpus-per-task=1
#SBATCH --partition=awagner,hns,normal

# ar_reface_worker.sbatch — one array task refaces CHUNK lines of a todo list.
# Submitted by active_reface.sh; do not submit directly.
#
#   $1 RUN_DIR    run directory (holds config.sh)
#   $2 TODO_FILE  id <TAB> rel_path <TAB> imtype   (todo.tsv or a retry list)
#
# For each entry the original under TOP_DIR is copied (read-only) to node-local
# scratch, checksummed, refaced there, and the results are written to RUN_DIR:
#   refaced/<rel_path>     refaced file, same name/compression as the original
#   qc/<id>/*.png          mri_reface before/after renders
#   logs/<id>.log          mri_reface output
#   status/<id>.json       state, checksums, timings (read by report/commit)
# The original file is never written to (symlinks are read through).

set -uo pipefail

RUN_DIR="${1:?RUN_DIR required}"
TODO_FILE="${2:?TODO_FILE required}"
source "${RUN_DIR}/config.sh"
source /home/groups/awagner/miniconda3/etc/profile.d/conda.sh
conda activate mri_deface_env

TASK_ID="${SLURM_ARRAY_TASK_ID:?must run as an array task}"
THREADS="${SLURM_CPUS_PER_TASK:-1}"
LOCAL_BASE="${L_SCRATCH_JOB:-${TMPDIR:-/tmp}}"

start_line=$(( TASK_ID * CHUNK_SIZE + 1 ))
end_line=$(( start_line + CHUNK_SIZE - 1 ))
echo "[INFO]: task=${TASK_ID} lines=${start_line}-${end_line} todo=${TODO_FILE} node=$(hostname)"

write_status() {
    # write_status <id> <rel> <imtype> <state> <exit> <message> <orig_sha> <size> <mtime> <refaced_sha> <t0>
    python3 - "$@" <<'PYEOF'
import json, os, sys, time, glob
(id_, rel, imtype, state, code, msg, osha, size, mtime, rsha, t0) = sys.argv[1:12]
run = os.environ['RUN_DIR']
qc = sorted(os.path.relpath(p, run) for p in glob.glob(os.path.join(run, 'qc', id_, '*.png')))
out = {
    'id': id_, 'rel_path': rel, 'imtype': imtype, 'state': state,
    'exit_code': int(code) if code.lstrip('-').isdigit() else None, 'message': msg,
    'orig_sha256': osha, 'orig_size': int(size) if size.isdigit() else None,
    'orig_mtime': int(mtime) if mtime.isdigit() else None,
    'refaced_sha256': rsha, 'refaced_rel': os.path.join('refaced', rel) if rsha else '',
    'qc_images': qc, 'log': os.path.join('logs', f'{id_}.log'),
    'started': int(t0), 'finished': int(time.time()), 'elapsed_s': int(time.time()) - int(t0),
    'node': os.uname().nodename,
    'slurm_job': f"{os.environ.get('SLURM_ARRAY_JOB_ID', '')}_{os.environ.get('SLURM_ARRAY_TASK_ID', '')}",
}
path = os.path.join(run, 'status', f'{id_}.json')
with open(path + '.tmp', 'w') as f:
    json.dump(out, f, indent=1)
os.replace(path + '.tmp', path)
PYEOF
}

reface_entry() {
    local id="$1" rel="$2" imtype="$3"
    local orig="${TOP_DIR%/}/${rel}"
    local t0; t0=$(date +%s)
    local work="" log="${RUN_DIR}/logs/${id}.log"
    local osha="" size="" mtime="" rsha="" rc=0

    fail() {
        echo "[ERROR]: ${id} ${rel}: $1" >&2
        write_status "$id" "$rel" "$imtype" failed "${2:-1}" "$1" "$osha" "$size" "$mtime" "" "$t0"
        [[ -n "$work" ]] && rm -rf "$work"
        return 1
    }

    echo "[INFO]: --- ${id} ${rel} (imType=${imtype}) ---"
    # Regular file, or a git-annex symlink (detect only queues links whose content is present).
    [[ -f "$orig" ]] || { fail "original missing or unreadable"; return 1; }
    work=$(mktemp -d "${LOCAL_BASE}/ar_${id}_XXXXXX") || { fail "mktemp failed"; return 1; }
    mkdir -p "${work}/in" "${work}/out" "${work}/mcr" "${work}/tmp"

    # Copy once from the original, then checksum the copy and confirm the
    # original did not change while it was being read.
    read -r size mtime < <(stat -L -c '%s %Y' "$orig")
    cp --preserve=timestamps "$orig" "${work}/orig" || { fail "copy from original failed"; return 1; }
    osha=$(sha256sum "${work}/orig" | cut -d' ' -f1)
    local size2 mtime2; read -r size2 mtime2 < <(stat -L -c '%s %Y' "$orig")
    [[ "$size" == "$size2" && "$mtime" == "$mtime2" ]] || { fail "original changed while copying; retry later"; return 1; }

    # mri_reface mis-parses some names (e.g. t1.nii_brain.nii.gz) and only
    # takes .nii, so feed it a canonical uncompressed input.nii.
    if [[ "${rel,,}" == *.nii.gz ]]; then
        gunzip -c "${work}/orig" > "${work}/in/input.nii" || { fail "gunzip failed"; return 1; }
    else
        cp "${work}/orig" "${work}/in/input.nii"
    fi

    apptainer exec --cleanenv \
        --env "MCR_CACHE_ROOT=${work}/mcr,TMPDIR=${work}/tmp" \
        --bind "${work}:${work}" \
        "$MRI_REFACE_SIF" run_mri_reface.sh \
            "${work}/in/input.nii" "${work}/out" \
            -imType "$imtype" -threads "$THREADS" -saveQCRenders 1 \
        > "$log" 2>&1
    rc=$?

    local refaced
    refaced=$(find "${work}/out" -maxdepth 1 \( -iname '*deFaced.nii*' -o -iname '*_reface.nii*' \) | head -1)
    [[ -n "$refaced" ]] || { fail "mri_reface produced no defaced output (exit=${rc}); see log" "$rc"; return 1; }

    mkdir -p "${RUN_DIR}/qc/${id}"
    find "${work}/out" -maxdepth 1 -iname '*.png' -exec cp {} "${RUN_DIR}/qc/${id}/" \;

    # Match the original's compression, write via temp + rename.
    local dest="${RUN_DIR}/refaced/${rel}"
    mkdir -p "$(dirname "$dest")"
    if [[ "${rel,,}" == *.nii.gz ]]; then
        if [[ "${refaced,,}" == *.gz ]]; then cp "$refaced" "${dest}.part"; else gzip -c "$refaced" > "${dest}.part"; fi
    else
        if [[ "${refaced,,}" == *.gz ]]; then gunzip -c "$refaced" > "${dest}.part"; else cp "$refaced" "${dest}.part"; fi
    fi || { fail "writing refaced output failed"; return 1; }
    mv "${dest}.part" "$dest"
    rsha=$(sha256sum "$dest" | cut -d' ' -f1)

    local state=ok msg=""
    if (( rc != 0 )); then
        # Seen in practice: QC rendering fails after the defaced image is written.
        state=warning; msg="mri_reface exited ${rc} but produced a defaced image; check QC and log"
    elif [[ -z "$(ls -A "${RUN_DIR}/qc/${id}" 2>/dev/null)" ]]; then
        state=warning; msg="no QC renders produced"
    fi
    write_status "$id" "$rel" "$imtype" "$state" "$rc" "$msg" "$osha" "$size" "$mtime" "$rsha" "$t0"
    rm -rf "$work"
    echo "[INFO]: ${id} ${state} ($(( $(date +%s) - t0 ))s)"
}

export RUN_DIR
mkdir -p "${RUN_DIR}/status" "${RUN_DIR}/logs" "${RUN_DIR}/qc" "${RUN_DIR}/refaced"

n_fail=0
while IFS=$'\t' read -r id rel imtype; do
    [[ -z "$id" ]] && continue
    reface_entry "$id" "$rel" "$imtype" || n_fail=$((n_fail + 1))
done < <(sed -n "${start_line},${end_line}p" "$TODO_FILE")

echo "[INFO]: task ${TASK_ID} done, failures=${n_fail}"
(( n_fail == 0 ))
