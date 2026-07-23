#!/bin/bash
#SBATCH --job-name=deface_extract
#SBATCH --output=/home/users/hyang336/jobs/deface_extract_%j.out
#SBATCH --error=/home/users/hyang336/jobs/deface_extract_%j.err
#SBATCH --time=8:00:00
#SBATCH --nodes=1
#SBATCH --mem=64G
#SBATCH --cpus-per-task=4
#SBATCH --partition=awagner,hns,normal

set -euo pipefail

FILEPATH="${1:?FILEPATH required}"
DEFACE_FILE="${2:?DEFACE_FILE required}"
DELETE_FILE="${3:?DELETE_FILE required}"
STATE_DIR="${4:?STATE_DIR required}"

EXTRACT_DIR="${STATE_DIR}/extract"
MANIFEST_DIR="${STATE_DIR}/manifests"
DEFACE_MANIFEST="${MANIFEST_DIR}/deface_manifest.tsv"
DELETE_MANIFEST="${MANIFEST_DIR}/delete_members.txt"

echo "[INFO]: archive:      ${FILEPATH}"
echo "[INFO]: state_dir:    ${STATE_DIR}"
echo "[INFO]: deface_file:  ${DEFACE_FILE}"
echo "[INFO]: delete_file:  ${DELETE_FILE}"

mkdir -p "$EXTRACT_DIR" "$MANIFEST_DIR"

cp "$DEFACE_FILE" "$MANIFEST_DIR/deface_members_raw.txt"
cp "$DELETE_FILE" "$DELETE_MANIFEST"

python3 - "$DEFACE_FILE" "$DEFACE_MANIFEST" <<'PYEOF'
import pathlib, sys

src = pathlib.Path(sys.argv[1]).read_text().strip()
out = pathlib.Path(sys.argv[2])
with out.open('w', newline='') as handle:
    if src:
        for entry in src.split(';'):
            if not entry:
                continue
            member, sep, imtype = entry.rpartition('|')
            if not sep:
                member, imtype = entry, ''
            handle.write(f"{member}\t{imtype}\n")
PYEOF

archive_lower="${FILEPATH,,}"
echo "[INFO]: Extracting archive ($(du -sh "$FILEPATH" | cut -f1) compressed)…"
if [[ "$archive_lower" == *.zip ]]; then
    unzip -q "$FILEPATH" -d "$EXTRACT_DIR"
else
    cflag=""
    [[ "$archive_lower" == *.tar.gz  || "$archive_lower" == *.tgz  ]] && cflag="z"
    [[ "$archive_lower" == *.tar.bz2 || "$archive_lower" == *.tbz2 ]] && cflag="j"
    [[ "$archive_lower" == *.tar.xz  || "$archive_lower" == *.txz  ]] && cflag="J"
    tar -x${cflag}f "$FILEPATH" -C "$EXTRACT_DIR"
fi

deface_count=$(wc -l < "$DEFACE_MANIFEST" | tr -d '[:space:]')
delete_count=$(python3 - "$DELETE_MANIFEST" <<'PYEOF'
import pathlib, sys
text = pathlib.Path(sys.argv[1]).read_text().strip()
print(0 if not text else sum(1 for entry in text.split(';') if entry))
PYEOF
)

echo "[INFO]: Deface manifest entries: ${deface_count}"
echo "[INFO]: Delete manifest entries: ${delete_count}"
echo "[INFO]: Extract stage complete."