#!/bin/bash
# Parse survey_error output and build/submit SLURM array retries.

set -euo pipefail

show_help() {
	cat <<'EOF'
USAGE:
  retry_array_from_survey.sh --survey_file FILE [OPTIONS]

DESCRIPTION:
  Parse a survey_error output file, extract array-task error files,
  optionally ignore benign scheduler/container warnings, and either:
	1) print a retry summary + sbatch command (default), or
	2) submit the retry array job (--submit).

REQUIRED:
  --survey_file FILE       Output file produced by survey_error.bash

OPTIONS:
  --jobs_dir DIR           Directory containing .err files [default: ~/jobs]
  --array_job_id ID        Restrict to a specific array parent job ID
  --keep_benign            Keep benign warning-only tasks in retry list
  --retry_ids_out FILE     Write retry task IDs (one per line)

  --submit                 Submit retry job with sbatch
  --state_dir DIR          State dir used by deface archive pipeline (required with --submit)
  --sif FILE               mri_reface .sif path (required with --submit)
  --worker_script FILE     Worker sbatch script
						   [default: <this_dir>/deface_archive_worker.sbatch]
  --chunk_size N           Chunk size passed to worker [default: 4]
  --max_concurrency N      % concurrency for retry array [default: 8]
  --time HH:MM:SS          sbatch time for retry array [default: 24:00:00]
  --job_name NAME          sbatch job name [default: ddel_retry]
  --output_pattern FILE    sbatch --output pattern [default: ~/jobs/ddel_retry_%A_%a.out]
  --error_pattern FILE     sbatch --error pattern [default: ~/jobs/ddel_retry_%A_%a.err]

  --help, -h               Show this help

EXAMPLES:
  # Dry run: parse and print retry command
  bash retry_array_from_survey.sh \
	--survey_file ~/scratch/ddel_fmmt_array_errs \
	--array_job_id 33810283

  # Submit retries directly
  bash retry_array_from_survey.sh \
	--survey_file ~/scratch/ddel_fmmt_array_errs \
	--array_job_id 33810283 \
	--submit \
	--state_dir /scratch/users/hyang336/deface_tmp/ddel_fmmt_state_pVbooB \
	--sif /home/users/hyang336/G_home/mri_reface_docker/mri_reface.sif \
	--chunk_size 4 --max_concurrency 8
EOF
}

normalize_user_path() {
	local p="$1"
	if [[ "$p" == "~" ]]; then
		printf '%s\n' "$HOME"
	elif [[ "$p" == "~/"* ]]; then
		printf '%s\n' "$HOME/${p#~/}"
	else
		printf '%s\n' "$p"
	fi
}

is_benign_warning_only() {
	# Return 0 only for known benign warning-only .err files.
	local f="$1"
	[[ -s "$f" ]] || return 0

	# If any hard-failure signature appears, this is actionable.
	if grep -qiE '(^|[^A-Za-z])(ERROR|Error|FATAL|Fatal|Segmentation|Traceback|Cannot find file|Unable to convert|itk::ExceptionObject|MATLAB is exiting|Killed|DUE TO TIME LIMIT)([^A-Za-z]|$)' "$f"; then
		return 1
	fi

	# Known benign signatures seen on Sherlock.
	if grep -qiE 'slurm_get_node_energy|_get_joules_task|squashfuse_ll mount took an unexpectedly long time' "$f"; then
		return 0
	fi

	# Unknown non-empty content: keep for retry.
	return 1
}

compress_ids_to_ranges() {
	# Read numeric IDs from stdin and emit compact range string, e.g. 1-3,5,7-9.
	awk '
		NR==1 {start=$1; prev=$1; next}
		{
			cur=$1
			if (cur == prev + 1) {
				prev=cur
				next
			}
			if (start == prev) {
				printf "%s,", start
			} else {
				printf "%s-%s,", start, prev
			}
			start=cur
			prev=cur
		}
		END {
			if (NR == 0) exit
			if (start == prev) {
				printf "%s", start
			} else {
				printf "%s-%s", start, prev
			}
		}
	'
}

survey_file=""
jobs_dir="$HOME/jobs"
array_job_id=""
keep_benign=false
retry_ids_out=""

submit=false
state_dir=""
sif=""
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
worker_script="${script_dir}/deface_archive_worker.sbatch"
chunk_size="4"
max_concurrency="8"
time_limit="24:00:00"
job_name="ddel_retry"
output_pattern="$HOME/jobs/ddel_retry_%A_%a.out"
error_pattern="$HOME/jobs/ddel_retry_%A_%a.err"

while [[ $# -gt 0 ]]; do
	case "$1" in
		--survey_file) survey_file="$2"; shift 2 ;;
		--jobs_dir) jobs_dir="$2"; shift 2 ;;
		--array_job_id) array_job_id="$2"; shift 2 ;;
		--keep_benign) keep_benign=true; shift ;;
		--retry_ids_out) retry_ids_out="$2"; shift 2 ;;
		--submit) submit=true; shift ;;
		--state_dir) state_dir="$2"; shift 2 ;;
		--sif) sif="$2"; shift 2 ;;
		--worker_script) worker_script="$2"; shift 2 ;;
		--chunk_size) chunk_size="$2"; shift 2 ;;
		--max_concurrency) max_concurrency="$2"; shift 2 ;;
		--time) time_limit="$2"; shift 2 ;;
		--job_name) job_name="$2"; shift 2 ;;
		--output_pattern) output_pattern="$2"; shift 2 ;;
		--error_pattern) error_pattern="$2"; shift 2 ;;
		--help|-h) show_help; exit 0 ;;
		*) echo "[ERROR]: Unknown option '$1'" >&2; show_help; exit 1 ;;
	esac
done

[[ -n "$survey_file" ]] || { echo "[ERROR]: --survey_file is required" >&2; exit 1; }
survey_file="$(normalize_user_path "$survey_file")"
jobs_dir="$(normalize_user_path "$jobs_dir")"
worker_script="$(normalize_user_path "$worker_script")"
output_pattern="$(normalize_user_path "$output_pattern")"
error_pattern="$(normalize_user_path "$error_pattern")"
[[ -f "$survey_file" ]] || { echo "[ERROR]: survey file not found: $survey_file" >&2; exit 1; }
[[ -d "$jobs_dir" ]] || { echo "[ERROR]: jobs dir not found: $jobs_dir" >&2; exit 1; }
[[ -f "$worker_script" ]] || { echo "[ERROR]: worker script not found: $worker_script" >&2; exit 1; }
[[ "$chunk_size" =~ ^[0-9]+$ ]] || { echo "[ERROR]: --chunk_size must be integer" >&2; exit 1; }
[[ "$max_concurrency" =~ ^[0-9]+$ ]] || { echo "[ERROR]: --max_concurrency must be integer" >&2; exit 1; }

if $submit; then
	[[ -n "$state_dir" ]] || { echo "[ERROR]: --state_dir is required with --submit" >&2; exit 1; }
	[[ -n "$sif" ]] || { echo "[ERROR]: --sif is required with --submit" >&2; exit 1; }
	state_dir="$(normalize_user_path "$state_dir")"
	sif="$(normalize_user_path "$sif")"
	[[ -d "$state_dir" ]] || { echo "[ERROR]: state dir not found: $state_dir" >&2; exit 1; }
	[[ -f "$sif" ]] || { echo "[ERROR]: sif not found: $sif" >&2; exit 1; }
fi

mapfile -t err_basenames < <(
	grep -oE '[^[:space:]]+_[0-9]+_[0-9]+\.err' "$survey_file" | sort -u
)

(( ${#err_basenames[@]} > 0 )) || {
	echo "[INFO]: No array error files found in survey file."
	exit 0
}

declare -A seen_ids=()
declare -A seen_parent=()
declare -a retry_ids=()
total_candidates=0
benign_skipped=0
missing_err_files=0

for err_base in "${err_basenames[@]}"; do
	if [[ "$err_base" =~ _([0-9]+)_([0-9]+)\.err$ ]]; then
		parent_id="${BASH_REMATCH[1]}"
		task_id="${BASH_REMATCH[2]}"
	else
		continue
	fi

	if [[ -n "$array_job_id" && "$parent_id" != "$array_job_id" ]]; then
		continue
	fi

	seen_parent["$parent_id"]=1
	total_candidates=$((total_candidates + 1))
	err_path="${jobs_dir%/}/${err_base}"
	if [[ ! -f "$err_path" ]]; then
		missing_err_files=$((missing_err_files + 1))
		continue
	fi

	if ! $keep_benign && is_benign_warning_only "$err_path"; then
		benign_skipped=$((benign_skipped + 1))
		continue
	fi

	if [[ -z "${seen_ids[$task_id]:-}" ]]; then
		seen_ids["$task_id"]=1
		retry_ids+=("$task_id")
	fi
done

if (( ${#seen_parent[@]} > 1 )) && [[ -z "$array_job_id" ]]; then
	echo "[WARN]: Multiple array parent job IDs detected in survey file: ${!seen_parent[*]}" >&2
	echo "       Use --array_job_id to limit retry list to one array job." >&2
fi

if (( ${#retry_ids[@]} == 0 )); then
	echo "[INFO]: No actionable retry IDs after filtering."
	echo "[INFO]: candidates=${total_candidates}, benign_skipped=${benign_skipped}, missing_err_files=${missing_err_files}"
	exit 0
fi

mapfile -t retry_ids_sorted < <(printf '%s\n' "${retry_ids[@]}" | sort -n)
retry_spec="$(printf '%s\n' "${retry_ids_sorted[@]}" | compress_ids_to_ranges)"

if [[ -n "$retry_ids_out" ]]; then
	retry_ids_out="$(normalize_user_path "$retry_ids_out")"
	printf '%s\n' "${retry_ids_sorted[@]}" > "$retry_ids_out"
fi

echo "[INFO]: survey_file:      $survey_file"
echo "[INFO]: jobs_dir:         $jobs_dir"
echo "[INFO]: parent_ids_seen:  ${!seen_parent[*]}"
echo "[INFO]: candidates:       $total_candidates"
echo "[INFO]: benign_skipped:   $benign_skipped"
echo "[INFO]: missing_err:      $missing_err_files"
echo "[INFO]: retry_count:      ${#retry_ids_sorted[@]}"
echo "[INFO]: retry_spec:       $retry_spec"
[[ -n "$retry_ids_out" ]] && echo "[INFO]: retry_ids_out:     $retry_ids_out"

if ! $submit; then
	echo ""
	echo "[DRY-RUN]: sbatch --job-name=${job_name} --array=${retry_spec}%${max_concurrency} --time=${time_limit} --output=${output_pattern} --error=${error_pattern} ${worker_script} <STATE_DIR> <SIF> ${chunk_size}"
	echo "          Add --submit --state_dir ... --sif ... to submit."
	exit 0
fi

jid=$(sbatch \
	--job-name="${job_name}" \
	--array="${retry_spec}%${max_concurrency}" \
	--time="${time_limit}" \
	--output="${output_pattern}" \
	--error="${error_pattern}" \
	"$worker_script" \
	"$state_dir" \
	"$sif" \
	"$chunk_size" | awk '{print $4}')

echo "[INFO]: Submitted retry array job: ${jid}"

