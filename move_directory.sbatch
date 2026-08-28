#!/bin/bash
#SBATCH --job-name=move_directory
#SBATCH --output=/home/users/hyang336/jobs/move_directory_%j.out
#SBATCH --error=/home/users/hyang336/jobs/move_directory_%j.err
#SBATCH --time=24:00:00
#SBATCH --nodes=1
#SBATCH --mem=4G
#SBATCH --cpus-per-task=1
#SBATCH --partition=awagner,hns,normal

# Move a single directory to a new location.
#
# Usage:
#   sbatch move_directory.sbatch <source_dir> <destination_parent_or_path> [--dry-run]
#
# Examples:
#   sbatch move_directory.sbatch /oak/stanford/groups/awagner/dataA /scratch/users/hyang336/archive
#   sbatch move_directory.sbatch /oak/stanford/groups/awagner/dataA /scratch/users/hyang336/archive/dataA_20260724
#   sbatch move_directory.sbatch /oak/stanford/groups/awagner/dataA /scratch/users/hyang336/archive --dry-run

set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
    echo "Usage: sbatch $0 <source_dir> <destination_parent_or_path> [--dry-run]" >&2
    exit 1
fi

SOURCE_DIR="$1"
DEST_ARG="$2"
DRY_RUN="${3:-}"

if [[ "$DRY_RUN" != "" && "$DRY_RUN" != "--dry-run" ]]; then
    echo "[ERROR]: Third argument must be --dry-run (or omitted)." >&2
    exit 1
fi

if [[ ! -d "$SOURCE_DIR" ]]; then
    echo "[ERROR]: Source directory not found: $SOURCE_DIR" >&2
    exit 1
fi

SOURCE_DIR="$(realpath "$SOURCE_DIR")"
SOURCE_BASENAME="$(basename "$SOURCE_DIR")"

# Destination semantics:
# - If destination exists and is a directory, move source inside it.
# - Otherwise, treat destination as the full target path.
if [[ -d "$DEST_ARG" ]]; then
    DEST_PATH="$(realpath "$DEST_ARG")/$SOURCE_BASENAME"
else
    DEST_PARENT="$(dirname "$DEST_ARG")"
    mkdir -p "$DEST_PARENT"
    DEST_PATH="$(realpath "$DEST_PARENT")/$(basename "$DEST_ARG")"
fi

# Safety checks
if [[ "$SOURCE_DIR" == "$DEST_PATH" ]]; then
    echo "[ERROR]: Source and destination are the same path: $SOURCE_DIR" >&2
    exit 1
fi

# Prevent moving source into itself (or one of its subdirectories)
case "$DEST_PATH" in
    "$SOURCE_DIR"/*)
        echo "[ERROR]: Destination is inside source; refusing to move." >&2
        echo "        source=$SOURCE_DIR" >&2
        echo "        dest=$DEST_PATH" >&2
        exit 1
        ;;
esac

if [[ -e "$DEST_PATH" ]]; then
    echo "[ERROR]: Destination already exists: $DEST_PATH" >&2
    exit 1
fi

echo "[INFO]: source:      $SOURCE_DIR"
echo "[INFO]: destination: $DEST_PATH"

if ! command -v rsync >/dev/null 2>&1; then
    echo "[ERROR]: rsync is not available in PATH." >&2
    exit 1
fi

if [[ "$DRY_RUN" == "--dry-run" ]]; then
    echo "[DRY-RUN]: mkdir -p \"$DEST_PATH\""
    echo "[DRY-RUN]: rsync -a --remove-source-files --info=progress2 --dry-run \"$SOURCE_DIR/\" \"$DEST_PATH/\""
    echo "[DRY-RUN]: find \"$SOURCE_DIR\" -depth -type d -empty -delete"
    echo "[DRY-RUN]: rmdir \"$SOURCE_DIR\""
    exit 0
fi

# Ensure destination exists as the moved directory root.
mkdir -p "$DEST_PATH"

echo "[INFO]: Syncing and removing source files..."
rsync -a --remove-source-files --info=progress2 "$SOURCE_DIR/" "$DEST_PATH/"

echo "[INFO]: Cleaning up empty source directories..."
find "$SOURCE_DIR" -depth -type d -empty -delete

if [[ -d "$SOURCE_DIR" ]]; then
    if ! rmdir "$SOURCE_DIR"; then
        echo "[ERROR]: Source directory still contains files after rsync cleanup: $SOURCE_DIR" >&2
        exit 1
    fi
fi

echo "[INFO]: Move complete."
