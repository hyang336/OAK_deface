#!/bin/bash
#SBATCH --job-name=deface_finalize
#SBATCH --output=/home/users/hyang336/jobs/deface_finalize_%j.out
#SBATCH --error=/home/users/hyang336/jobs/deface_finalize_%j.err
#SBATCH --time=8:00:00
#SBATCH --nodes=1
#SBATCH --mem=64G
#SBATCH --cpus-per-task=4
#SBATCH --partition=awagner,hns,normal

set -euo pipefail

FILEPATH="${1:?FILEPATH required}"
STATE_DIR="${2:?STATE_DIR required}"
DELETE_FILE="${3:-}"

EXTRACT_DIR="${STATE_DIR}/extract"
DELETE_MANIFEST="${STATE_DIR}/manifests/delete_members.txt"
TMP_ARCHIVE="${STATE_DIR}/repacked_archive.tmp"

tar_compress_flag() {
    local lower="${1,,}"
    [[ "$lower" == *.tar.gz  || "$lower" == *.tgz  ]] && { echo "z"; return; }
    [[ "$lower" == *.tar.bz2 || "$lower" == *.tbz2 ]] && { echo "j"; return; }
    [[ "$lower" == *.tar.xz  || "$lower" == *.txz  ]] && { echo "J"; return; }
    echo ""
}

delete_file() {
    local f="$1"
    if [[ ! -f "$f" ]]; then
        echo "[WARN]: Not found, skipping delete: ${f}"
        return 0
    fi
    rm -f "$f"
    echo "[INFO]: Deleted: ${f}"
    local lower_f="${f,,}"
    if [[ "$lower_f" == *.hdr ]]; then
        local img="${f%.*}.img"
        if [[ -f "$img" ]]; then
            rm -f "$img"
            echo "[INFO]: Deleted ANALYZE sidecar: ${img}"
        fi
    fi
    if [[ "$lower_f" == *.nhdr ]]; then
        echo "[WARN]: Deleted NRRD header ${f} — verify that any separate data file referenced inside the header has been removed."
    fi
}

trap "rm -f '${TMP_ARCHIVE}'" EXIT

echo "[INFO]: archive:    ${FILEPATH}"
echo "[INFO]: state_dir:  ${STATE_DIR}"

# Failsafe: if delete_members.txt is missing or empty and DELETE_FILE was provided, restore from source.
if [[ ! -s "$DELETE_MANIFEST" ]]; then
    if [[ -n "$DELETE_FILE" && -f "$DELETE_FILE" ]]; then
        echo "[WARN]: delete_members.txt missing or empty, restoring from: ${DELETE_FILE}"
        cp "$DELETE_FILE" "$DELETE_MANIFEST"
    else
        if [[ -n "$DELETE_FILE" ]]; then
            echo "[ERROR]: delete_members.txt is missing/empty, but provided DELETE_FILE not found: ${DELETE_FILE}" >&2
        else
            echo "[ERROR]: delete_members.txt is missing/empty, and no DELETE_FILE was provided (pass as 3rd argument to finalize)" >&2
        fi
        exit 1
    fi
fi

if [[ -f "$DELETE_MANIFEST" ]]; then
    while IFS= read -r member || [[ -n "$member" ]]; do
        [[ -z "$member" ]] && continue
        IFS=';' read -ra delete_arr <<< "$member"
        for delete_member in "${delete_arr[@]}"; do
            [[ -z "$delete_member" ]] && continue
            echo "[INFO]: --- Deleting member: ${delete_member} ---"
            delete_file "${EXTRACT_DIR}/${delete_member}"
        done
    done < "$DELETE_MANIFEST"
fi

archive_lower="${FILEPATH,,}"
echo "[INFO]: Repacking modified extraction tree…"
if [[ "$archive_lower" == *.zip ]]; then
    (cd "$EXTRACT_DIR" && zip -qr "$TMP_ARCHIVE" .)
else
    cflag=$(tar_compress_flag "$FILEPATH")
    # tar exits 1 for non-fatal warnings (e.g. file changed/removed while reading).
    # Only treat exit code 2 (fatal error) as a failure.
    set +e
    tar -c${cflag}f "$TMP_ARCHIVE" -C "$EXTRACT_DIR" .
    tar_exit=$?
    set -e
    if (( tar_exit == 2 )); then
        echo "[ERROR]: tar failed fatally (exit=2)" >&2
        exit 2
    elif (( tar_exit == 1 )); then
        echo "[WARN]: tar reported non-fatal warnings (exit=1); archive may be incomplete — check .err log."
    fi
fi

orig_dir=$(dirname "$FILEPATH")
orig_base=$(basename "$FILEPATH")
staging="${orig_dir}/.${orig_base}.deface_staging"
cp "$TMP_ARCHIVE" "$staging"
mv "$staging" "$FILEPATH"
echo "[INFO]: Original archive replaced: ${FILEPATH}  ($(du -sh "$FILEPATH" | cut -f1))"

rm -rf "$STATE_DIR"
echo "[INFO]: Finalize stage complete."