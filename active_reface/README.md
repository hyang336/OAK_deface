# active_reface — reviewed refacing for active datasets

The scripts one level up (`find_anat.py`, `deface_delete_from_csv.sh`, …) were
built to clean **legacy** data: they deface or delete in place. This folder
is for **active** datasets, where nothing may change until the project owner
has reviewed the result.

```
start ──► ar_detect (job) ──► ar_reface_worker (array) ──► ar_report (job) ──► report.html
                                                                                  │ owner reviews,
                                                                                  ▼ exports CSV
                                                     commit (job) ◄── review_decisions_<run>.csv
                                                     restore (job, optional undo)
```

## What each stage does

| Stage | Touches the dataset? | What happens |
|---|---|---|
| **detect** | read-only | Walks the top directory and classifies every neuroimaging file with `ar_classify.py` (see [Detection](#detection)). Each listed file gets one action (see below). Writes `manifest.csv`, `todo.tsv` and `run_info.json`. |
| **reface** | read-only | For each `REFACE` scan, copies the original to node-local scratch, checksums it (and checks it didn't change during the copy), and runs `mri_reface` with the detected `-imType`. The refaced file goes to `refaced/<rel_path>`, the QC renders to `qc/<id>/`, the log to `logs/<id>.log` and the status to `status/<id>.json`. |
| **report** | read-only | Builds `report.html`: one self-contained file with embedded before/after renders, approve/reject controls, failed scans with log tails, and the anatomical scans that were not refaced. |
| **commit** | **writes** | For **approved** scans and their identical copies only: checks that the refaced file and the original both still match their recorded checksums, keeps the original in a backup directory on Oak, and atomically replaces it with the refaced file. |
| **restore** | **writes** | Puts the backed-up originals back (undoes a commit). |

What each detect action means:

| Action | Meaning | In the report |
|---|---|---|
| `REFACE` | NIfTI with a known modality (T1, T2, FLAIR, PD, T2ST, FDG, PIB, FBP, TAU): a regular file, or a git-annex symlink whose content is present | Section 1, with QC images and approve/reject |
| `DUPLICATE` | Byte-identical to a `REFACE` file. Refaced once; the copies follow its decision | Listed on that file's card, and in section 4 |
| `ALREADY_REFACED` | Matches a file committed by an earlier run of this pipeline on the same dataset | Section 4 |
| `ALIAS` | Symlink to another file in the dataset; the target is handled directly | Section 4 |
| `REPORT_ONLY` | Anatomical, but not refaced here: MGZ/MINC/NRRD/ANALYZE, archive member, unknown modality, symlink leaving the dataset or broken | Section 3, never modified |
| `NO_FACE` | Mask, segmentation, skull-stripped image, transform or statistical map | Section 4 (collapsed) |
| `UNCLASSIFIED` | Neuroimaging file with no anatomical evidence | Section 4 (collapsed) |

Functional, diffusion, fieldmap and perfusion files are counted but not listed.

## Detection

`ar_classify.py` replaces `find_anat.py`'s rules for this pipeline only; the legacy scripts keep their own.

- **Formats:** `.nii`/`.nii.gz`, `.mnc`/`.mnc.gz`/`.mnc2`, `.mgh`/`.mgz`, `.nrrd`/`.nhdr` and ANALYZE `.hdr`, plus their members inside tar/zip archives (by name). Hidden files and directories are skipped, and symlinked directories are not followed (they're listed in `run_info.json`).
- **Tokens:** anything other than a letter or digit separates words, so `T1_MPRAGE_SAG_0003` matches both `T1` and `MPRAGE`. Long sequence names (MPRAGE, MP2RAGE, SPGR, BRAVO, FLAIR, tracer names) also match inside longer words.
- **Evidence** comes from the filename, the parent directory, BIDS datatype directories (`anat/`, `pet/`, `func/`, `dwi/`, `fmap/`, `perf/`, …), the JSON sidecar (`SeriesDescription`, `ProtocolName`, `SequenceName`, `PulseSequenceDetails`, `TracerName`, `Modality`) and the NIfTI header (shape, voxel size).
- **Decision order:**
  1. Explicit functional/diffusion/fieldmap/perfusion markers mean non-structural, unless the filename itself names an anatomical sequence or a PET tracer.
  2. A 4D volume is non-structural only if nothing marks it as anatomical or PET. Multi-echo MEMPRAGE and dynamic PET are kept.
  3. PET evidence means PET. This is checked before structural because co-registered PET often has `T1` in its name.
  4. Structural evidence means structural: high confidence for an `anat/` directory, a BIDS suffix, a sequence name in the filename or a sidecar hit; medium confidence for a generic token like `T1` or `struct`.
  5. With no name or sidecar evidence, a 3D NIfTI with voxels ≤ 1.5 mm counts as structural with unknown modality (`REPORT_ONLY`). Coarser volumes are `UNCLASSIFIED`.
- **imType** comes from the filename, then the sidecar, then a parent directory with a sequence name (e.g. `T1_MPRAGE_0003/`). Bare `T1`/`T2` directory names are ignored because they are often timepoints. Check the imType badge on each card: a wrong imType usually shows up as a failed or poor reface.
- **Face-free derivatives** are matched at the end of the name (`_mask`, `_dseg`, `_probseg`, `_brain`, `_xfm`, `_warp`, …), by entities (`desc-brain`, `label-`), by SPM/FSL forms (`c1…`, `wc1…`, `y_…`, `zstat1`, `cope3`, `spmT_0001`, `con_0002`, …) and by FreeSurfer `mri/` names (`brain`, `brainmask`, `aseg`, `norm`, `wm`, …). FreeSurfer `orig`, `rawavg`, `nu` and `T1` keep the face and are listed in section 3.

## Usage

Run from a login node; every stage is submitted as a Slurm job.

```bash
AR=/home/groups/awagner/find_anat/active_reface/active_reface.sh
SIF=/home/groups/awagner/mri_reface_docker/mri_reface.sif

# 1. Detect + reface + report (originals untouched)
bash $AR start --top /oak/stanford/groups/awagner/<project> --sif $SIF
#    → prints the run dir, e.g. $SCRATCH/active_reface/<project>_20261023_150000

bash $AR status --run_dir <run_dir>      # check by hand now and then
bash $AR retry  --run_dir <run_dir>      # re-run failed / never-run scans (+ new report)
bash $AR report --run_dir <run_dir>      # rebuild report.html only

# 2. Review: give <run_dir>/report.html to the project owner. They open it in a
#    browser, approve/reject each scan, enter their name, and click
#    "Export decisions CSV". They send that CSV back to you.

# 3. Commit approved scans. Dry run first:
bash $AR commit --run_dir <run_dir> --decisions review_decisions_<run>.csv --dry_run
bash $AR commit --run_dir <run_dir> --decisions review_decisions_<run>.csv

# Undo, if needed:
bash $AR restore --run_dir <run_dir> [--dry_run]
```

Options for `start`:

| Option | Default | Meaning |
|---|---|---|
| `--chunk` | 3 | Scans per array task (measured ~12 min and ~7 GB peak per T1w scan, so ~40 min per task) |
| `--max_concurrent` | 16 | Maximum array tasks running at once |
| `--worker_time` | 4:00:00 | Time limit per array task |
| `--detect_time` | 12:00:00 | Time limit for the detect job |
| `--partition` | `awagner,hns,normal` | Slurm partitions |
| `--skip_archives` | off | Don't list the anatomical members of archives |

`ar_detect.py` also takes `--registry <commit_log.csv> ...` to name earlier runs explicitly. By default it reads every `<parent>/<top>_reface_backup_*/commit_log.csv`.

## Commit safety details

- **Only approved scans are touched,** plus their `DUPLICATE` copies, each verified against its own checksum. Rejected and undecided scans, failed reface runs and everything else are left alone.
- **Conflicts:** if an original changed after it was refaced (someone re-ran a conversion, say), it is reported as `CONFLICT` and skipped. Run a new `start` to pick it up.
- **Backups:** the default backup location is `<parent of top>/<top name>_reface_backup_<run id>/<rel_path>`; override it with `--backup_dir`. The original is hard-linked there when on the same filesystem (instant, no extra space until the dataset copy is replaced), otherwise copied and verified. The commit log, decisions CSV, report and manifest are copied there as the permanent record. **The owner deletes the backup directory once they're satisfied**; until then it holds identifiable faces.
- **Atomic replacement:** the refaced file is copied next to the original, verified and renamed over it, so the path never disappears. The original's permission bits and group are kept, but the file owner becomes whoever runs commit.
- **Repeats:** commit is idempotent (`ALREADY_COMMITTED`) and logs every row to `commit_log.csv`. Later `start` runs read these logs and mark matching files `ALREADY_REFACED`.
- **git-annex/datalad files:** the symlink is replaced by a regular file holding the refaced data (the same result as `git annex unlock` plus an edit), and the backup is a symlink to the annex object; `restore` recreates the original link. **The faced original stays in the annex object store and in git history.** For de-identification to be complete, the owner must `datalad save` and then drop the old content from the dataset and every clone or remote. The same applies to any dataset whose NIfTIs are tracked in plain git.

## Things to know

- **Scratch purge:** the run dir lives on `$SCRATCH`, which purges files not modified for 90 days. Commit within that window. If the refaced files are purged, commit refuses them (`SKIP_REFACED_BAD`); just run `start` again, since the originals are unchanged until commit.
- **The report contains face renders of the originals.** Treat it like the imaging data: share it through approved storage (Oak, Stanford Google Drive/Box per the data's risk classification), not email.
- **Not handled:** DICOM series, AFNI `BRIK/HEAD`, Philips `PAR/REC`. Fieldmap magnitude images and functional runs can show a face at low resolution but are not refaced.
- **Sidecar JSONs are not changed;** refacing doesn't affect them.
- **Report size:** each refaced scan embeds two ~100 KB renders, about 0.25 MB of report per scan. Images load as you scroll, but a report with several thousand scans gets heavy; consider one run per study or subject group.
- **Re-committing after a restore:** commit refuses to overwrite an existing backup (`SKIP_BACKUP_EXISTS`). Move or delete the old backup directory first, or pass a new `--backup_dir`.
