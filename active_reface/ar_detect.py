#!/usr/bin/env python3
"""ar_detect.py — Inventory anatomical scans in an active dataset.

Walk TOP_DIR recursively and classify every neuroimaging file with
ar_classify (filename, parent dir, BIDS dirs, JSON sidecar, NIfTI header).
Each file that is not plainly non-structural gets one action:

  REFACE           NIfTI with a known mri_reface imType (regular file, or a
                   git-annex symlink whose content is present)
  DUPLICATE        byte-identical to a REFACE file; refaced/committed with it
  ALREADY_REFACED  matches a file committed by an earlier run
  ALIAS            symlink to another file in the dataset (handled there)
  REPORT_ONLY      anatomical but not refaced here (non-NIfTI, archive member,
                   unknown modality, symlink leaving the dataset, ...)
  NO_FACE          mask / segmentation / skull-stripped / transform / stat map
  UNCLASSIFIED     neuroimaging file with no anatomical evidence

Nothing in TOP_DIR is modified.

Outputs (in RUN_DIR): manifest.csv, todo.tsv (REFACE rows), run_info.json

Usage:
  python ar_detect.py <top_dir> <run_dir> [--skip-archives] [--skip-nibabel]
                      [--registry commit_log.csv ...]
"""

import argparse
import csv
import datetime
import glob
import hashlib
import json
import os
import sys
import tarfile
import zipfile
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ar_classify as ac  # noqa: E402

MANIFEST_COLUMNS = [
    'id', 'action', 'reason', 'rel_path', 'archive_member', 'format', 'category',
    'confidence', 'imtype', 'evidence', 'dimensions', 'voxel_mm', 'size_bytes',
    'link_kind', 'link_target', 'duplicate_of', 'sha256',
]


def sha256(path, bufsize=8 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(bufsize):
            h.update(chunk)
    return h.hexdigest()


def archive_members(path, kind):
    """Yield member names (files only) of a tar/zip archive."""
    if kind == 'zip':
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not name.endswith('/'):
                    yield name
    else:
        with tarfile.open(path, 'r:*') as tf:
            for m in tf:
                if m.isfile():
                    yield m.name


def symlink_info(path, top_dir):
    """Classify a symlink: returns (link_kind, link_target, reason_if_not_usable)."""
    target_text = os.readlink(path)
    real = os.path.realpath(path)
    if not os.path.exists(real):
        return 'broken', target_text, 'broken symlink (git-annex content not present?)'
    if not (real == top_dir or real.startswith(top_dir + os.sep)):
        return 'external', target_text, 'symlink to a file outside the dataset'
    rel_parts = os.path.relpath(real, top_dir).split(os.sep)
    if '.git' in rel_parts and 'annex' in rel_parts:
        return 'annex', target_text, None
    if any(p.startswith('.') for p in rel_parts):
        return 'hidden', target_text, 'symlink into a hidden directory'
    return 'alias', os.path.relpath(real, top_dir), None


def decide(row):
    """Set action/reason for a classified row (not yet deduplicated)."""
    if row['category'] == 'non_structural':
        return None
    if row['no_face']:
        return 'NO_FACE', row['no_face']
    if row['category'] == 'unclassified':
        return 'UNCLASSIFIED', 'no anatomical evidence in name, sidecar or header'
    if row['archive_member']:
        return 'REPORT_ONLY', 'inside an archive'
    if row['link_kind'] == 'alias':
        return 'ALIAS', f'symlink to {row["link_target"]} (handled there)'
    if row['link_reason']:
        return 'REPORT_ONLY', row['link_reason']
    if row['format'] != 'NIfTI':
        return 'REPORT_ONLY', f'{row["format"]} format is not refaced by this pipeline'
    if not row['imtype']:
        return 'REPORT_ONLY', 'modality could not be determined (name, sidecar)'
    return 'REFACE', 'git-annex file: commit replaces the symlink with a regular file' \
        if row['link_kind'] == 'annex' else ''


def load_registry(paths):
    """Return {(size, sha256): source} of files committed by earlier runs."""
    known = {}
    for p in paths:
        try:
            with open(p, newline='') as f:
                for r in csv.DictReader(f):
                    if r.get('result') == 'COMMITTED' and r.get('refaced_size'):
                        known[(int(r['refaced_size']), r['refaced_sha256'])] = p
        except (OSError, ValueError, KeyError) as exc:
            print(f'Warning: cannot read registry {p}: {exc}', file=sys.stderr)
    return known


def detect(top_dir, use_nibabel=True, inspect_archives=True):
    rows, counts, walk_errors, symlinked_dirs = [], defaultdict(int), [], []

    def onerror(err):
        walk_errors.append(f'{err.filename}: {err.strerror}')
        print(f'Walk error: {err}', file=sys.stderr)

    def add(rel, member, fmt, size, shape=None, zooms=None, sidecar=None,
            link_kind='', link_target='', link_reason=None):
        name_path = os.path.join(rel, member) if member else rel
        res = ac.classify(name_path, fmt, shape, zooms, sidecar)
        _, stem = ac.get_format(os.path.basename(name_path))
        row = {
            'rel_path': rel, 'archive_member': member, 'format': fmt,
            'category': res['category'], 'confidence': res['confidence'],
            'imtype': res['imtype'], 'evidence': '; '.join(res['reasons']),
            'dimensions': 'x'.join(map(str, shape)) if shape else '',
            'voxel_mm': 'x'.join(f'{z:g}' for z in zooms[:3]) if zooms else '',
            'size_bytes': size, 'link_kind': link_kind, 'link_target': link_target,
            'link_reason': link_reason, 'duplicate_of': '', 'sha256': '',
            'no_face': ac.no_face_reason(stem or '', os.path.dirname(name_path)),
        }
        decision = decide(row)
        if decision is None:
            counts['non_structural'] += 1
            return
        row['action'], row['reason'] = decision
        rows.append(row)

    for dirpath, dirnames, filenames in os.walk(top_dir, onerror=onerror):
        for d in dirnames:
            if not d.startswith('.') and os.path.islink(os.path.join(dirpath, d)):
                symlinked_dirs.append(os.path.relpath(os.path.join(dirpath, d), top_dir))
        dirnames[:] = sorted(d for d in dirnames if not d.startswith('.'))
        for fname in sorted(filenames):
            if fname.startswith('.'):
                continue
            path = os.path.join(dirpath, fname)
            rel = os.path.relpath(path, top_dir)
            fmt, _ = ac.get_format(fname)
            arch = None if fmt else (inspect_archives and ac.get_archive_type(fname))
            if not fmt and not arch:
                continue
            link_kind = link_target = ''
            link_reason = None
            if os.path.islink(path):
                link_kind, link_target, link_reason = symlink_info(path, top_dir)
            try:
                if arch:
                    if link_kind in ('alias', 'broken', 'external', 'hidden'):
                        continue  # archives behind symlinks: listed where they live
                    for member in archive_members(path, arch):
                        mfmt, _ = ac.get_format(os.path.basename(member))
                        if mfmt:
                            add(rel, member, mfmt, '')
                    continue
                readable = link_kind not in ('broken',)
                size = os.stat(path).st_size if readable else ''
                shape = zooms = None
                if use_nibabel and readable and fmt == 'NIfTI':
                    shape, zooms = ac.read_header(path)
                sidecar = ac.read_sidecar(path) if readable else {}
                add(rel, '', fmt, size, shape, zooms, sidecar, link_kind, link_target, link_reason)
            except PermissionError:
                walk_errors.append(f'{rel}: permission denied')
            except (tarfile.TarError, zipfile.BadZipFile, EOFError, OSError) as exc:
                walk_errors.append(f'{rel}: unreadable ({exc})')
    return rows, counts, walk_errors, symlinked_dirs


def mark_known_and_duplicates(rows, registry):
    """Hash only where sizes collide; mark ALREADY_REFACED and DUPLICATE rows."""
    refs = [r for r in rows if r['action'] == 'REFACE']
    known_sizes = {s for s, _ in registry}
    by_size = defaultdict(list)
    for r in refs:
        by_size[r['size_bytes']].append(r)
    for size, group in by_size.items():
        if len(group) < 2 and size not in known_sizes:
            continue
        for r in group:
            r['sha256'] = sha256(r['abs_path'])
        for r in group:
            src = registry.get((size, r['sha256']))
            if src:
                r['action'], r['reason'] = 'ALREADY_REFACED', f'matches a file committed per {src}'
        by_hash = defaultdict(list)
        for r in group:
            if r['action'] == 'REFACE':
                by_hash[r['sha256']].append(r)
        for same in by_hash.values():
            # Reface (and show in the report) a regular file outside sourcedata/
            # when possible; the others follow its decision.
            same.sort(key=lambda r: (r['link_kind'] != '',
                                     'sourcedata' in r['rel_path'].split(os.sep), r['rel_path']))
            for dup in same[1:]:
                dup['action'] = 'DUPLICATE'
                dup['duplicate_of'] = same[0]['id']
                dup['reason'] = f'identical to {same[0]["rel_path"]}; refaced and committed with it'


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('top_dir')
    ap.add_argument('run_dir')
    ap.add_argument('--skip-archives', action='store_true')
    ap.add_argument('--skip-nibabel', action='store_true')
    ap.add_argument('--registry', nargs='*', default=None,
                    help='commit_log.csv files of earlier runs (default: '
                         '<parent>/<top>_reface_backup_*/commit_log.csv)')
    args = ap.parse_args()

    top_dir = os.path.realpath(args.top_dir)
    if not os.path.isdir(top_dir):
        sys.exit(f'Error: not a directory: {top_dir}')
    os.makedirs(args.run_dir, exist_ok=True)
    started = datetime.datetime.now().isoformat(timespec='seconds')
    use_nib = not args.skip_nibabel and ac.nib is not None

    rows, counts, walk_errors, symlinked_dirs = detect(top_dir, use_nib, not args.skip_archives)
    for i, row in enumerate(rows, start=1):
        row['id'] = f'{i:06d}'
        row['abs_path'] = os.path.join(top_dir, row['rel_path'])

    registry_files = args.registry if args.registry is not None else sorted(glob.glob(
        os.path.join(os.path.dirname(top_dir), f'{os.path.basename(top_dir)}_reface_backup_*',
                     'commit_log.csv')))
    mark_known_and_duplicates(rows, load_registry(registry_files))

    with open(os.path.join(args.run_dir, 'manifest.csv'), 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(args.run_dir, 'todo.tsv'), 'w') as f:
        for r in rows:
            if r['action'] == 'REFACE':
                f.write(f'{r["id"]}\t{r["rel_path"]}\t{r["imtype"]}\n')

    by_action = defaultdict(int)
    for r in rows:
        by_action[r['action']] += 1
    info = {
        'top_dir': top_dir,
        'run_dir': os.path.realpath(args.run_dir),
        'run_id': os.path.basename(os.path.realpath(args.run_dir)),
        'detect_started': started,
        'detect_finished': datetime.datetime.now().isoformat(timespec='seconds'),
        'nibabel': use_nib,
        'archives_inspected': not args.skip_archives,
        'registry_files': registry_files,
        'counts_by_action': dict(by_action),
        'counts_not_listed': dict(counts),
        'walk_errors': walk_errors,
        'symlinked_dirs_not_followed': symlinked_dirs,
    }
    with open(os.path.join(args.run_dir, 'run_info.json'), 'w') as f:
        json.dump(info, f, indent=2)

    print(f'Top dir : {top_dir}')
    for k in ('REFACE', 'DUPLICATE', 'ALREADY_REFACED', 'ALIAS', 'REPORT_ONLY', 'NO_FACE', 'UNCLASSIFIED'):
        print(f'  {k:<16}: {by_action.get(k, 0)}')
    print(f'  not listed (non-structural): {counts["non_structural"]}, walk errors: {len(walk_errors)}, '
          f'symlinked dirs not followed: {len(symlinked_dirs)}')
    print(f'Manifest: {os.path.join(args.run_dir, "manifest.csv")}')


if __name__ == '__main__':
    main()
