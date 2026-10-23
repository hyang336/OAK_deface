#!/bin/bash
# active_reface.sh — reface anatomical scans in an ACTIVE dataset, with review.
#
# Stages (each runs as Slurm jobs; see README.md):
#   start    detect anatomical scans under --top, reface the refaceable ones
#            on scratch copies, then build report.html for owner review.
#            Nothing under --top is modified.
#   status   print progress counts for a run (no polling; run it by hand).
#   retry    re-submit refacing for entries that failed or never ran.
#   report   rebuild report.html (e.g. after a retry).
#   commit   after review: replace approved originals with refaced versions,
#            moving the faced originals to a backup directory on Oak.
#   restore  undo a commit using the backups.
#
# USAGE:
#   bash active_reface.sh start  --top /oak/.../project --sif /path/mri_reface.sif
#                                [--run_dir DIR] [--chunk 3] [--max_concurrent 16]
#                                [--worker_time 4:00:00] [--detect_time 12:00:00]
#                                [--partition awagner,hns,normal] [--skip_archives]
#   bash active_reface.sh status  --run_dir DIR
#   bash active_reface.sh retry   --run_dir DIR
#   bash active_reface.sh report  --run_dir DIR
#   bash active_reface.sh commit  --run_dir DIR --decisions review_decisions.csv
#                                 [--backup_dir DIR] [--dry_run] [--time 12:00:00]
#   bash active_reface.sh restore --run_dir DIR [--dry_run] [--time 12:00:00]
#
# Default run dir: $SCRATCH/active_reface/<basename of top>_<YYYYmmdd_HHMMSS>
# Scratch is purged after 90 days without modification: review and commit
# before then, or re-run `start` (the originals are untouched until commit).

set -euo pipefail

AR_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CMD="${1:-}"; [[ $# -gt 0 ]] && shift

usage() { sed -n '/^# USAGE/,/^[^#]/{ /^#/{ s/^# \{0,1\}//; p } }' "${AR_HOME}/active_reface.sh"; }
die() { echo "[ERROR]: $*" >&2; exit 1; }
jobid() { awk '{print $4}'; }

# ---- options ----------------------------------------------------------------
top="" sif="" run_dir="" chunk=3 max_concurrent=16 worker_time="4:00:00"
detect_time="12:00:00" partition="awagner,hns,normal" skip_archives=0
decisions="" backup_dir="" dry_run=0 job_time="12:00:00" todo=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --top)            top="$2"; shift 2 ;;
        --sif)            sif="$2"; shift 2 ;;
        --run_dir)        run_dir="$2"; shift 2 ;;
        --chunk)          chunk="$2"; shift 2 ;;
        --max_concurrent) max_concurrent="$2"; shift 2 ;;
        --worker_time)    worker_time="$2"; shift 2 ;;
        --detect_time)    detect_time="$2"; shift 2 ;;
        --partition)      partition="$2"; shift 2 ;;
        --skip_archives)  skip_archives=1; shift ;;
        --decisions)      decisions="$2"; shift 2 ;;
        --backup_dir)     backup_dir="$2"; shift 2 ;;
        --dry_run)        dry_run=1; shift ;;
        --time)           job_time="$2"; shift 2 ;;
        --todo)           todo="$2"; shift 2 ;;
        -h|--help)        usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

need_run_dir() {
    [[ -n "$run_dir" ]] || die "--run_dir required"
    [[ -f "${run_dir}/config.sh" ]] || die "not a run dir (no config.sh): ${run_dir}"
    run_dir="$(realpath "$run_dir")"
    source "${run_dir}/config.sh"
}

py_job() {
    # py_job <name> <time> <dependency-or-empty> <script.py> [args...]
    local name="$1" time="$2" dep="$3"; shift 3
    sbatch --parsable ${dep:+--dependency=$dep} \
        --job-name="ar_${name}" --time="$time" --partition="$PARTITION" \
        --output="${RUN_DIR}/slurm/${name}_%j.out" \
        "${AR_HOME}/ar_py.sbatch" "$RUN_DIR" "$@"
}

submit_reface() {
    # Submit the worker array over a todo file, then a report job after it.
    local todo_file="$1" n tasks wid rid
    n=$(grep -c . "$todo_file" || true)
    if (( n == 0 )); then
        echo "[INFO]: nothing to reface in ${todo_file}"
        rid=$(py_job report 1:00:00 "" ar_report.py "$RUN_DIR")
        echo "[INFO]: report job ${rid}"
        return
    fi
    tasks=$(( (n + CHUNK_SIZE - 1) / CHUNK_SIZE ))
    wid=$(sbatch --parsable --job-name=ar_reface --time="$WORKER_TIME" --partition="$PARTITION" \
        --array="0-$((tasks - 1))%${MAX_CONCURRENT}" \
        --output="${RUN_DIR}/slurm/reface_%A_%a.out" \
        "${AR_HOME}/ar_reface_worker.sbatch" "$RUN_DIR" "$todo_file")
    rid=$(py_job report 1:00:00 "afterany:${wid}" ar_report.py "$RUN_DIR")
    echo "${wid}" >> "${RUN_DIR}/worker_jobs.txt"
    echo "[INFO]: reface array ${wid}: ${n} scans in ${tasks} tasks (chunk ${CHUNK_SIZE}, %${MAX_CONCURRENT})"
    echo "[INFO]: report job ${rid} runs after the array finishes → ${RUN_DIR}/report.html"
}

case "$CMD" in
start)
    [[ -n "$top" && -d "$top" ]] || die "--top must be an existing directory"
    [[ -n "$sif" && -f "$sif" ]] || die "--sif must point to the mri_reface .sif"
    [[ "$chunk" =~ ^[1-9][0-9]*$ ]] || die "--chunk must be >= 1"
    top="$(realpath "$top")"; sif="$(realpath "$sif")"
    [[ -n "$run_dir" ]] || run_dir="${SCRATCH:?SCRATCH not set}/active_reface/$(basename "$top")_$(date +%Y%m%d_%H%M%S)"
    [[ -e "${run_dir}/config.sh" ]] && die "run dir already used: ${run_dir}"
    mkdir -p "${run_dir}/slurm"; run_dir="$(realpath "$run_dir")"
    {
        printf 'TOP_DIR=%q\n' "$top"
        printf 'RUN_DIR=%q\n' "$run_dir"
        printf 'MRI_REFACE_SIF=%q\n' "$sif"
        printf 'AR_HOME=%q\n' "$AR_HOME"
        printf 'CHUNK_SIZE=%q\n' "$chunk"
        printf 'MAX_CONCURRENT=%q\n' "$max_concurrent"
        printf 'WORKER_TIME=%q\n' "$worker_time"
        printf 'PARTITION=%q\n' "$partition"
        printf 'SKIP_ARCHIVES=%q\n' "$skip_archives"
    } > "${run_dir}/config.sh"
    source "${run_dir}/config.sh"
    did=$(sbatch --parsable --job-name=ar_detect --time="$detect_time" --partition="$PARTITION" \
        --output="${RUN_DIR}/slurm/detect_%j.out" \
        "${AR_HOME}/ar_detect.sbatch" "$RUN_DIR")
    echo "[INFO]: run dir:    ${RUN_DIR}"
    echo "[INFO]: detect job: ${did} (submits the reface array and report when done)"
    echo "[INFO]: check progress later with: bash $0 status --run_dir ${RUN_DIR}"
    ;;
submit-reface)   # internal: called by ar_detect.sbatch
    need_run_dir
    submit_reface "${todo:-${RUN_DIR}/todo.tsv}"
    ;;
status)
    need_run_dir
    echo "run dir  : ${RUN_DIR}"
    echo "top dir  : ${TOP_DIR}"
    if [[ -f "${RUN_DIR}/todo.tsv" ]]; then
        echo "to reface: $(grep -c . "${RUN_DIR}/todo.tsv" || true)"
    else
        echo "to reface: (detect not finished)"
    fi
    if [[ -d "${RUN_DIR}/status" ]]; then
        for s in ok warning failed; do
            printf '  %-8s %s\n' "$s" "$(grep -l "\"state\": \"${s}\"" "${RUN_DIR}"/status/*.json 2>/dev/null | wc -l)"
        done
    fi
    if [[ -f "${RUN_DIR}/report.html" ]]; then echo "report   : ${RUN_DIR}/report.html"; fi
    if [[ -f "${RUN_DIR}/commit_log.csv" ]]; then echo "commit   : ${RUN_DIR}/commit_log.csv"; fi
    ;;
retry)
    need_run_dir
    retry_file="${RUN_DIR}/retry_$(date +%Y%m%d_%H%M%S).tsv"
    while IFS=$'\t' read -r id rel imtype; do
        st="${RUN_DIR}/status/${id}.json"
        if [[ ! -f "$st" ]] || grep -q '"state": "failed"' "$st"; then
            printf '%s\t%s\t%s\n' "$id" "$rel" "$imtype"
        fi
    done < "${RUN_DIR}/todo.tsv" > "$retry_file"
    echo "[INFO]: $(grep -c . "$retry_file" || true) entries to retry → ${retry_file}"
    submit_reface "$retry_file"
    ;;
report)
    need_run_dir
    rid=$(py_job report 1:00:00 "" ar_report.py "$RUN_DIR")
    echo "[INFO]: report job ${rid} → ${RUN_DIR}/report.html"
    ;;
commit)
    need_run_dir
    [[ -n "$decisions" && -f "$decisions" ]] || die "--decisions must be the CSV exported from report.html"
    decisions="$(realpath "$decisions")"
    args=(ar_commit.py commit "$RUN_DIR" --decisions "$decisions")
    [[ -n "$backup_dir" ]] && args+=(--backup-dir "$backup_dir")
    (( dry_run )) && args+=(--dry-run)
    cid=$(py_job commit "$job_time" "" "${args[@]}")
    echo "[INFO]: commit job ${cid}$( (( dry_run )) && echo ' (dry run)') → ${RUN_DIR}/slurm/commit_${cid}.out"
    ;;
restore)
    need_run_dir
    args=(ar_commit.py restore "$RUN_DIR")
    (( dry_run )) && args+=(--dry-run)
    cid=$(py_job restore "$job_time" "" "${args[@]}")
    echo "[INFO]: restore job ${cid}$( (( dry_run )) && echo ' (dry run)') → ${RUN_DIR}/slurm/restore_${cid}.out"
    ;;
""|-h|--help) usage ;;
*) die "unknown command: ${CMD} (start|status|retry|report|commit|restore)" ;;
esac
