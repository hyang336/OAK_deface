#!/usr/bin/env python3
"""ar_commit.py — Write approved refaced scans back into the dataset, or undo it.

commit RUN_DIR --decisions CSV [--backup-dir DIR] [--dry-run]
    For every scan marked "approve" in the review CSV exported from
    report.html, and every byte-identical DUPLICATE of it:
      1. the refaced file in RUN_DIR/refaced must match its recorded checksum;
      2. the original must still match the checksum taken when it was copied
         (otherwise it changed since refacing → CONFLICT, left untouched);
      3. the original is preserved in BACKUP_DIR/<rel_path>: a hard link when
         on the same filesystem, otherwise a verified copy.  For a git-annex
         symlink the backup is a symlink to the annex object (the faced data
         stays in the annex until the owner drops it);
      4. the refaced file is copied next to the original and renamed over it
         (atomic; the path never disappears).  Permission bits and group are
         carried over (annex links become regular, owner-writable files).
    Results are appended to RUN_DIR/commit_log.csv, and the log, decisions,
    report and manifest are copied into BACKUP_DIR so the record outlives
    scratch.  Later runs read these logs to skip already-refaced files.

restore RUN_DIR [--dry-run]
    Put the originals back for every committed file that still holds the
    refaced content (annex files get their symlink back).  Backups are kept.

Default BACKUP_DIR: <parent of top>/<top name>_reface_backup_<run id>
"""

import argparse
import csv
import datetime
import hashlib
import json
import os
import shutil
import sys

LOG_COLUMNS = ['time', 'action', 'id', 'rel_path', 'result', 'message', 'orig_sha256',
               'refaced_sha256', 'refaced_size', 'backup_path', 'link_kind', 'link_target',
               'duplicate_of', 'reviewer']


def sha256(path, bufsize=8 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(bufsize):
            h.update(chunk)
    return h.hexdigest()


def now():
    return datetime.datetime.now().isoformat(timespec='seconds')


def load_run(run_dir):
    with open(os.path.join(run_dir, 'run_info.json')) as f:
        info = json.load(f)
    with open(os.path.join(run_dir, 'manifest.csv'), newline='') as f:
        manifest = {r['id']: r for r in csv.DictReader(f)}
    status = {}
    sdir = os.path.join(run_dir, 'status')
    for name in os.listdir(sdir) if os.path.isdir(sdir) else []:
        if name.endswith('.json'):
            with open(os.path.join(sdir, name)) as f:
                st = json.load(f)
            status[st['id']] = st
    return info, manifest, status


def append_log(run_dir, rows):
    path = os.path.join(run_dir, 'commit_log.csv')
    new = not os.path.exists(path)
    with open(path, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLUMNS, extrasaction='ignore')
        if new:
            w.writeheader()
        w.writerows(rows)
    return path


def install(src, dest, expect_sha, mode, gid):
    """Copy src next to dest, verify, set mode/group, rename over dest
    (replaces a symlink at dest with the regular file)."""
    tmp = os.path.join(os.path.dirname(dest), f'.{os.path.basename(dest)}.reface_tmp')
    shutil.copyfile(src, tmp)
    try:
        with open(tmp, 'rb') as f:
            os.fsync(f.fileno())
        if sha256(tmp) != expect_sha:
            raise IOError('checksum mismatch after copying into place')
        os.chmod(tmp, mode)
        try:
            os.chown(tmp, -1, gid)
        except PermissionError:
            pass
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def relink(dest, link_text):
    """Atomically make dest a symlink with the given target text."""
    tmp = os.path.join(os.path.dirname(dest), f'.{os.path.basename(dest)}.reface_link')
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(link_text, tmp)
    os.replace(tmp, dest)


def preserve(orig, backup, expect_sha, annex):
    """Keep the original at `backup`; returns how."""
    os.makedirs(os.path.dirname(backup), exist_ok=True)
    if annex:
        os.symlink(os.path.realpath(orig), backup)
        return 'symlink to annex object'
    try:
        os.link(orig, backup)
        return 'hardlink'
    except OSError:
        shutil.copy2(orig, backup)
        if sha256(backup) != expect_sha:
            os.remove(backup)
            raise IOError('backup copy checksum mismatch')
        return 'copy'


def commit_one(top, target, st, refaced, backup, dry_run):
    """Commit one dataset file. Returns (result, message)."""
    orig = os.path.join(top, target['rel_path'])
    annex = target.get('link_kind') == 'annex'
    if annex:
        if not os.path.islink(orig) or os.readlink(orig) != target['link_target']:
            return 'SKIP_ORIG_CHANGED', 'annex symlink missing or now points elsewhere'
        if not os.path.isfile(orig):
            return 'SKIP_ORIG_MISSING', 'annex content no longer present'
    elif os.path.islink(orig) or not os.path.isfile(orig):
        return 'SKIP_ORIG_MISSING', 'original missing or no longer a regular file'
    cur = sha256(orig)
    if cur == st['refaced_sha256']:
        return 'ALREADY_COMMITTED', ''
    if cur != st['orig_sha256']:
        return 'CONFLICT', 'original changed since it was refaced; re-run for this file'
    if os.path.lexists(backup):
        return 'SKIP_BACKUP_EXISTS', 'a backup already exists at the target path'
    if dry_run:
        return 'WOULD_COMMIT', 'annex symlink → regular file' if annex else ''
    like = os.stat(orig)
    mode = (like.st_mode & 0o7777) | (0o200 if annex else 0)
    how = preserve(orig, backup, st['orig_sha256'], annex)
    install(refaced, orig, st['refaced_sha256'], mode, like.st_gid)
    return 'COMMITTED', f'backup via {how}'


def commit(run_dir, decisions_csv, backup_dir, dry_run):
    info, manifest, status = load_run(run_dir)
    top = info['top_dir']
    backup_dir = os.path.realpath(backup_dir or os.path.join(
        os.path.dirname(top), f'{os.path.basename(top)}_reface_backup_{info["run_id"]}'))
    with open(decisions_csv, newline='') as f:
        decisions = list(csv.DictReader(f))
    run_ids = {d.get('run_id') for d in decisions}
    if run_ids != {info['run_id']}:
        sys.exit(f'Error: decisions are for run(s) {sorted(run_ids)}, not {info["run_id"]}')
    dups = {}
    for r in manifest.values():
        if r.get('action') == 'DUPLICATE':
            dups.setdefault(r['duplicate_of'], []).append(r)

    print(f'Dataset    : {top}')
    print(f'Backup dir : {backup_dir}')
    print(f'Decisions  : {decisions_csv} ({len(decisions)} rows)')
    if dry_run:
        print('DRY RUN — nothing will be changed')

    log, tally = [], {}

    def record(target, st, result, msg, reviewer, backup=''):
        tally[result] = tally.get(result, 0) + 1
        log.append({'time': now(), 'action': 'commit', 'id': target['id'],
                    'rel_path': target['rel_path'], 'result': result, 'message': msg,
                    'orig_sha256': st.get('orig_sha256', ''), 'refaced_sha256': st.get('refaced_sha256', ''),
                    'refaced_size': st.get('refaced_size', ''), 'backup_path': backup,
                    'link_kind': target.get('link_kind', ''), 'link_target': target.get('link_target', ''),
                    'duplicate_of': target.get('duplicate_of', ''), 'reviewer': reviewer})
        if result != 'NOT_APPROVED':
            print(f'  {result:<18} {target["rel_path"]}  {msg}')

    for d in decisions:
        sid, reviewer = d['id'], d.get('reviewer', '')
        canon = manifest.get(sid, {'id': sid, 'rel_path': d['rel_path']})
        st = dict(status.get(sid, {}))
        if d.get('decision') != 'approve':
            record(canon, st, 'NOT_APPROVED', d.get('decision') or 'undecided', reviewer)
            continue
        if st.get('rel_path') != d['rel_path'] or st.get('state') not in ('ok', 'warning'):
            record(canon, st, 'SKIP_NOT_REFACED', 'no successful reface record for this id/path', reviewer)
            continue
        refaced = os.path.join(run_dir, st['refaced_rel'])
        if not os.path.isfile(refaced) or sha256(refaced) != st['refaced_sha256']:
            for t in [canon] + dups.get(sid, []):
                record(t, st, 'SKIP_REFACED_BAD', 'refaced file missing or altered (scratch purge?)', reviewer)
            continue
        st['refaced_size'] = os.path.getsize(refaced)
        for target in [canon] + dups.get(sid, []):
            backup = os.path.join(backup_dir, target['rel_path'])
            try:
                result, msg = commit_one(top, target, st, refaced, backup, dry_run)
            except Exception as exc:  # keep going; every file is independent
                result, msg = 'ERROR', str(exc)
            record(target, st, result, msg, reviewer, backup)

    if not dry_run:
        logp = append_log(run_dir, log)
        os.makedirs(backup_dir, exist_ok=True)
        for src in (logp, decisions_csv, os.path.join(run_dir, 'report.html'),
                    os.path.join(run_dir, 'manifest.csv'), os.path.join(run_dir, 'run_info.json')):
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(backup_dir, os.path.basename(src)))
        print(f'\nLog: {logp} (copy in {backup_dir})')
        if any(r['link_kind'] == 'annex' and r['result'] == 'COMMITTED' for r in log):
            print('NOTE: git-annex files were replaced by regular files. The faced originals are '
                  'still in the annex: the owner must `datalad save`, then drop the old content '
                  '(and from any clones/remotes) for de-identification to be complete.')
    print('\nSummary: ' + ', '.join(f'{k}={v}' for k, v in sorted(tally.items())))
    return 1 if tally.get('ERROR') or tally.get('CONFLICT') else 0


def restore(run_dir, dry_run):
    info, _, _ = load_run(run_dir)
    top = info['top_dir']
    path = os.path.join(run_dir, 'commit_log.csv')
    if not os.path.isfile(path):
        sys.exit('Error: no commit_log.csv in run dir; nothing to restore')
    latest = {}
    with open(path, newline='') as f:
        for r in csv.DictReader(f):
            if r['result'] in ('COMMITTED', 'RESTORED'):
                latest[r['rel_path']] = r
    log, tally = [], {}
    for rel, r in sorted(latest.items()):
        if r['result'] != 'COMMITTED':
            continue
        orig, backup = os.path.join(top, rel), r['backup_path']
        annex = r.get('link_kind') == 'annex'
        row = dict(r, time=now(), action='restore', message='')

        def done(result, msg=''):
            row['result'], row['message'] = result, msg
            tally[result] = tally.get(result, 0) + 1
            log.append(row)
            print(f'  {result:<18} {rel}  {msg}')
        try:
            if not os.path.lexists(backup) or (not annex and sha256(backup) != r['orig_sha256']):
                done('SKIP_NO_BACKUP', 'backup missing or altered')
            elif os.path.islink(orig) or not os.path.isfile(orig) or sha256(orig) != r['refaced_sha256']:
                done('SKIP_CHANGED', 'dataset file no longer holds the refaced version')
            elif annex and (not os.path.isfile(backup) or sha256(backup) != r['orig_sha256']):
                done('SKIP_NO_BACKUP', 'annex object for the original is no longer present')
            elif dry_run:
                done('WOULD_RESTORE')
            elif annex:
                relink(orig, r['link_target'])
                done('RESTORED', 'annex symlink recreated')
            else:
                like = os.stat(orig)
                install(backup, orig, r['orig_sha256'], like.st_mode & 0o7777, like.st_gid)
                done('RESTORED')
        except Exception as exc:
            done('ERROR', str(exc))
    if not dry_run and log:
        append_log(run_dir, log)
    print(('\nSummary: ' + ', '.join(f'{k}={v}' for k, v in sorted(tally.items()))) if tally
          else 'Nothing to restore.')
    return 1 if tally.get('ERROR') else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('commit')
    c.add_argument('run_dir')
    c.add_argument('--decisions', required=True)
    c.add_argument('--backup-dir')
    c.add_argument('--dry-run', action='store_true')
    r = sub.add_parser('restore')
    r.add_argument('run_dir')
    r.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    if args.cmd == 'commit':
        sys.exit(commit(args.run_dir, args.decisions, args.backup_dir, args.dry_run))
    sys.exit(restore(args.run_dir, args.dry_run))


if __name__ == '__main__':
    main()
