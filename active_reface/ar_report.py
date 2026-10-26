#!/usr/bin/env python3
"""ar_report.py — Build a self-contained review report for an active_reface run.

Reads RUN_DIR/{run_info.json, manifest.csv, todo.tsv, status/*.json, qc/*}
and writes RUN_DIR/report.html, with:
  * refaced scans with embedded before/after QC renders and approve/reject
    controls; decisions are exported as review_decisions.csv for commit
  * scans whose refacing failed (log tail)
  * anatomical scans NOT refaced (report-only), with the reason
  * collapsed lists: earlier-run matches, identical copies, symlink aliases,
    face-free derivatives, unclassified files

Usage: python ar_report.py <run_dir> [--output report.html]
"""

import argparse
import base64
import csv
import datetime
import html
import json
import os
import sys


def esc(s):
    return html.escape(str(s if s is not None else ''))


def load(run_dir):
    with open(os.path.join(run_dir, 'run_info.json')) as f:
        info = json.load(f)
    with open(os.path.join(run_dir, 'manifest.csv'), newline='') as f:
        manifest = list(csv.DictReader(f))
    status = {}
    sdir = os.path.join(run_dir, 'status')
    if os.path.isdir(sdir):
        for name in os.listdir(sdir):
            if name.endswith('.json'):
                with open(os.path.join(sdir, name)) as f:
                    st = json.load(f)
                status[st['id']] = st
    commits = {}
    clog = os.path.join(run_dir, 'commit_log.csv')
    if os.path.isfile(clog):
        with open(clog, newline='') as f:
            for row in csv.DictReader(f):
                commits[row['id']] = row['result']
    return info, manifest, status, commits


def img_tag(run_dir, rel):
    path = os.path.join(run_dir, rel)
    try:
        with open(path, 'rb') as f:
            data = base64.b64encode(f.read()).decode('ascii')
    except OSError:
        return f'<div class="missing">missing {esc(rel)}</div>'
    label = os.path.basename(rel)
    return (f'<figure><img data-src="data:image/png;base64,{data}" alt="{esc(label)}">'
            f'<figcaption>{esc(label)}</figcaption></figure>')


def log_tail(run_dir, rel, n=25):
    try:
        with open(os.path.join(run_dir, rel), errors='replace') as f:
            return ''.join(f.readlines()[-n:])
    except OSError:
        return '(log not found)'


def table(rows, cols):
    out = ['<table class="sortable"><thead><tr>']
    out += [f'<th>{esc(c)}</th>' for c, _ in cols]
    out.append('</tr></thead><tbody>')
    for r in rows:
        out.append('<tr>' + ''.join(f'<td>{esc(r.get(k, ""))}</td>' for _, k in cols) + '</tr>')
    out.append('</tbody></table>')
    return ''.join(out)


CSS = """
body{font-family:system-ui,sans-serif;margin:1.5em;color:#222;max-width:1400px}
h1{margin-bottom:.2em} h2{border-bottom:2px solid #ccc;padding-bottom:.2em;margin-top:2em}
.meta td{padding:1px 12px 1px 0} .meta td:first-child{color:#666}
.counts span{display:inline-block;margin:0 14px 6px 0;padding:6px 12px;border-radius:6px;background:#eef}
.card{border:1px solid #ccc;border-radius:8px;margin:12px 0;padding:10px 14px}
.card.approve{border-left:8px solid #2a2;background:#f4fff4}
.card.reject{border-left:8px solid #c22;background:#fff4f4}
.card h3{margin:0 0 4px;font-size:1em;font-family:monospace;word-break:break-all}
.badge{font-size:.8em;padding:2px 7px;border-radius:4px;background:#ddd;margin-right:6px}
.b-ok{background:#cfc}.b-warning{background:#fe9}.b-failed{background:#fbb}.b-committed{background:#9cf}
.imgs{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
figure{margin:0} figure img{max-height:260px;max-width:100%;border:1px solid #ddd;background:#000}
figcaption{font-size:.75em;color:#666}
.controls label{margin-right:14px;cursor:pointer} .controls input[type=text]{width:45%}
.warn{color:#a60} pre{background:#f6f6f6;padding:8px;overflow-x:auto;font-size:.8em;max-height:300px}
table{border-collapse:collapse;font-size:.85em;width:100%} th,td{border:1px solid #ddd;padding:3px 6px;text-align:left}
th{background:#f0f0f0;cursor:pointer} td{word-break:break-all}
#bar{position:sticky;top:0;background:#fff;padding:8px 0;border-bottom:1px solid #ccc;z-index:5}
#bar button{margin-right:8px} .note{color:#666;font-size:.9em}
"""

JS = r"""
const RUN_ID = document.body.dataset.runid;
const KEY = 'active_reface:' + RUN_ID;
let state = JSON.parse(localStorage.getItem(KEY) || '{}');
function save(){ localStorage.setItem(KEY, JSON.stringify(state)); refresh(); }
function refresh(){
  let a=0,r=0,t=0;
  document.querySelectorAll('.card[data-id]').forEach(c=>{
    t++; const s=state[c.dataset.id]||{}; c.classList.remove('approve','reject');
    if(s.decision){c.classList.add(s.decision); s.decision==='approve'?a++:r++;}
    c.querySelectorAll('input[type=radio]').forEach(i=>i.checked=(i.value===s.decision));
    const n=c.querySelector('input[type=text]'); if(n && document.activeElement!==n) n.value=s.note||'';
  });
  document.getElementById('progress').textContent=`${a} approved, ${r} rejected, ${t-a-r} undecided of ${t}`;
  document.getElementById('reviewer').value = state.__reviewer || document.getElementById('reviewer').value;
}
document.addEventListener('change', e=>{
  const c=e.target.closest('.card[data-id]'); if(!c) return;
  const s=state[c.dataset.id]||(state[c.dataset.id]={});
  if(e.target.type==='radio') s.decision=e.target.value;
  if(e.target.type==='text') s.note=e.target.value;
  save();
});
function setAll(decision, onlyOk){
  document.querySelectorAll('.card[data-id]').forEach(c=>{
    if(onlyOk && c.dataset.state!=='ok') return;
    (state[c.dataset.id]||(state[c.dataset.id]={})).decision=decision;
  }); save();
}
function csvq(v){ v=String(v==null?'':v); return /[",\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v; }
function exportCsv(){
  const who=document.getElementById('reviewer').value.trim();
  if(!who){ alert('Enter the reviewer name first.'); return; }
  state.__reviewer=who; save();
  const now=new Date().toISOString();
  const rows=[['run_id','id','rel_path','decision','note','reviewer','reviewed_at']];
  document.querySelectorAll('.card[data-id]').forEach(c=>{
    const s=state[c.dataset.id]||{};
    rows.push([RUN_ID,c.dataset.id,c.dataset.rel,s.decision||'',s.note||'',who,now]);
  });
  const blob=new Blob([rows.map(r=>r.map(csvq).join(',')).join('\n')+'\n'],{type:'text/csv'});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob);
  a.download='review_decisions_'+RUN_ID+'.csv'; a.click();
}
// Decode embedded images only when scrolled into view.
const io=new IntersectionObserver(es=>es.forEach(e=>{
  if(e.isIntersecting){ e.target.src=e.target.dataset.src; io.unobserve(e.target);} }),{rootMargin:'800px'});
document.querySelectorAll('img[data-src]').forEach(i=>io.observe(i));
document.querySelectorAll('table.sortable th').forEach((th,idx)=>th.addEventListener('click',()=>{
  const tb=th.closest('table').tBodies[0]; const asc=th.dataset.asc!=='1'; th.dataset.asc=asc?'1':'0';
  [...tb.rows].sort((x,y)=>x.cells[idx].textContent.localeCompare(y.cells[idx].textContent,undefined,{numeric:true})*(asc?1:-1))
    .forEach(r=>tb.appendChild(r));
}));
document.getElementById('reviewer').addEventListener('change',e=>{state.__reviewer=e.target.value; save();});
refresh();
"""


def build(run_dir):
    info, manifest, status, commits = load(run_dir)
    todo_ids = [r['id'] for r in manifest if r['action'] == 'REFACE']
    by_id = {r['id']: r for r in manifest}
    done = [status[i] for i in todo_ids if i in status and status[i]['state'] in ('ok', 'warning')]
    failed = [status[i] for i in todo_ids if i in status and status[i]['state'] == 'failed']
    not_run = [by_id[i] for i in todo_ids if i not in status]
    groups = {a: [r for r in manifest if r['action'] == a]
              for a in ('REPORT_ONLY', 'DUPLICATE', 'ALREADY_REFACED', 'ALIAS', 'NO_FACE', 'UNCLASSIFIED')}
    dups_of = {}
    for r in groups['DUPLICATE']:
        dups_of.setdefault(r['duplicate_of'], []).append(r)
    now = datetime.datetime.now().isoformat(timespec='seconds')

    p = []
    p.append(f'<!DOCTYPE html><html><head><meta charset="utf-8">'
             f'<title>Reface review — {esc(info["run_id"])}</title><style>{CSS}</style></head>'
             f'<body data-runid="{esc(info["run_id"])}">')
    p.append(f'<h1>Reface review</h1><table class="meta">'
             f'<tr><td>Dataset</td><td><code>{esc(info["top_dir"])}</code></td></tr>'
             f'<tr><td>Run</td><td><code>{esc(info["run_id"])}</code> ({esc(info["run_dir"])})</td></tr>'
             f'<tr><td>Detected</td><td>{esc(info["detect_finished"])}</td></tr>'
             f'<tr><td>Report built</td><td>{esc(now)}</td></tr></table>')
    p.append('<p class="note">This report contains face renders of the original scans. '
             'Handle it like the imaging data itself; do not email it or post it outside '
             'approved storage.</p>')

    counts = [('Refaced', len(done)), ('Failed', len(failed)), ('Not yet run', len(not_run)),
              ('Anatomical, not refaced', len(groups['REPORT_ONLY'])),
              ('Identical copies', len(groups['DUPLICATE'])),
              ('Refaced in an earlier run', len(groups['ALREADY_REFACED'])),
              ('Face-free derivatives', len(groups['NO_FACE'])),
              ('Unclassified', len(groups['UNCLASSIFIED']))]
    p.append('<div class="counts">' + ''.join(f'<span><b>{n}</b> {esc(k)}</span>' for k, n in counts) + '</div>')
    if info.get('walk_errors'):
        p.append(f'<p class="warn">{len(info["walk_errors"])} paths could not be read during detection '
                 f'(see run_info.json); files under them are not listed.</p>')
    if info.get('symlinked_dirs_not_followed'):
        p.append(f'<p class="warn">{len(info["symlinked_dirs_not_followed"])} symlinked directories were '
                 f'not followed (see run_info.json); scans reachable only through them are not listed.</p>')

    # ---- refaced ----
    p.append('<h2>1. Refaced scans — review required</h2>')
    p.append('<p>Check each pair of renders: the face should be replaced and the brain untouched. '
             'Approve or reject each scan, then export the decisions and send the CSV back to '
             'the operator. Only <b>approved</b> scans (and their identical copies) are written back '
             'to the dataset; decisions are kept in this browser until exported.</p>')
    p.append('<div id="bar"><b id="progress"></b>&nbsp;&nbsp;'
             '<button onclick="setAll(\'approve\',true)">Approve all OK</button>'
             '<button onclick="setAll(undefined,false)">Clear all</button>'
             'Reviewer: <input id="reviewer" placeholder="name / SUNet ID"> '
             '<button onclick="exportCsv()"><b>Export decisions CSV</b></button></div>')
    for st in sorted(done, key=lambda s: s['rel_path']):
        m = by_id.get(st['id'], {})
        badges = f'<span class="badge b-{esc(st["state"])}">{esc(st["state"])}</span>'
        badges += (f'<span class="badge">{esc(st["imtype"])}</span>'
                   f'<span class="badge">{esc(m.get("category"))} / {esc(m.get("confidence"))}</span>')
        if m.get('link_kind') == 'annex':
            badges += '<span class="badge b-warning">git-annex</span>'
        if st['id'] in commits:
            badges += f'<span class="badge b-committed">commit: {esc(commits[st["id"]])}</span>'
        p.append(f'<div class="card" data-id="{esc(st["id"])}" data-rel="{esc(st["rel_path"])}" '
                 f'data-state="{esc(st["state"])}"><h3>{esc(st["rel_path"])}</h3>{badges}'
                 f'<span class="note">id {esc(st["id"])} · {esc(m.get("dimensions"))} @ '
                 f'{esc(m.get("voxel_mm"))} mm · {esc(m.get("nonzero_ml"))} mL nonzero, '
                 f'{esc(m.get("zero_frac"))} zero fraction · {st.get("elapsed_s", "")}s</span>'
                 f'<div class="note">evidence: {esc(m.get("evidence"))}</div>')
        if st.get('message'):
            p.append(f'<div class="warn">⚠ {esc(st["message"])}</div>')
        if m.get('reason'):
            p.append(f'<div class="warn">{esc(m["reason"])}</div>')
        if dups_of.get(st['id']):
            p.append('<div class="note">Also applies to identical copies: '
                     + ', '.join(f'<code>{esc(d["rel_path"])}</code>' for d in dups_of[st['id']]) + '</div>')
        imgs = st.get('qc_images') or []
        p.append('<div class="imgs">' + ''.join(img_tag(run_dir, q) for q in imgs) + '</div>'
                 if imgs else '<div class="warn">No QC renders — inspect the refaced file directly.</div>')
        p.append('<div class="controls">'
                 f'<label><input type="radio" name="d{esc(st["id"])}" value="approve"> Approve</label>'
                 f'<label><input type="radio" name="d{esc(st["id"])}" value="reject"> Reject</label>'
                 '<input type="text" placeholder="note (optional)"></div>')
        if st['state'] == 'warning':
            p.append(f'<details><summary>log tail</summary><pre>{esc(log_tail(run_dir, st["log"]))}</pre></details>')
        p.append('</div>')
    if not done:
        p.append('<p><i>None.</i></p>')

    # ---- failed / not run ----
    p.append('<h2>2. Refacing failed or not run</h2>')
    p.append('<p>These stay unchanged in the dataset. The operator can retry them '
             '(<code>active_reface.sh retry</code>).</p>')
    for st in sorted(failed, key=lambda s: s['rel_path']):
        p.append(f'<div class="card"><h3>{esc(st["rel_path"])}</h3>'
                 f'<span class="badge b-failed">failed</span><span class="badge">{esc(st["imtype"])}</span>'
                 f'<div class="warn">{esc(st["message"])}</div>'
                 f'<details><summary>log tail</summary><pre>{esc(log_tail(run_dir, st["log"]))}</pre></details></div>')
    if not_run:
        p.append('<p>Not yet run (job still pending, cancelled, or timed out):</p>')
        p.append(table(not_run, [('id', 'id'), ('path', 'rel_path'), ('imType', 'imtype')]))
    if not failed and not not_run:
        p.append('<p><i>None.</i></p>')

    cols = [('id', 'id'), ('path', 'rel_path'), ('archive member', 'archive_member'),
            ('format', 'format'), ('category', 'category'), ('conf.', 'confidence'),
            ('imType', 'imtype'), ('reason', 'reason'), ('evidence', 'evidence'),
            ('dims', 'dimensions'), ('voxel mm', 'voxel_mm'), ('nonzero mL', 'nonzero_ml'),
            ('zero frac', 'zero_frac')]

    # ---- report only ----
    p.append(f'<h2>3. Anatomical scans not refaced ({len(groups["REPORT_ONLY"])})</h2>')
    p.append('<p>Detected as anatomical, but this pipeline does not reface them (see reason). '
             'They are left unchanged; the project owner decides what to do with each.</p>')
    p.append(table(groups['REPORT_ONLY'], cols) if groups['REPORT_ONLY'] else '<p><i>None.</i></p>')

    # ---- collapsed lists ----
    p.append('<h2>4. Other files, for spot-checking</h2>')
    for key, title, blurb in (
            ('ALREADY_REFACED', 'Refaced in an earlier run',
             'Content matches a file committed by an earlier active_reface run; not redone.'),
            ('DUPLICATE', 'Identical copies', 'Byte-identical to a scan in section 1; they follow its decision.'),
            ('ALIAS', 'Symlinks to other files in the dataset', 'The target file is handled directly.'),
            ('NO_FACE', 'Face-free derivatives',
             'Masks, segmentations, skull-stripped images (by name, or by voxels: mostly zeros '
             'and under 3000 mL of tissue), transforms, statistical maps, FreeSurfer derived and '
             'surface files, and templates. '
             'Spot-check that none of these still contain a face.'),
            ('UNCLASSIFIED', 'Unclassified neuroimaging files',
             'No anatomical evidence in name, sidecar or header (e.g. coarse-resolution maps). '
             'Spot-check for anatomical scans with uninformative names.')):
        rows = groups[key]
        p.append(f'<details><summary><b>{esc(title)} ({len(rows)})</b> — {esc(blurb)}</summary>')
        p.append(table(rows, cols) if rows else '<p><i>None.</i></p>')
        p.append('</details>')

    p.append(f'<script>{JS}</script></body></html>')
    summary = {'refaced': len(done), 'failed': len(failed), 'not_run': len(not_run)}
    summary.update({k.lower(): len(v) for k, v in groups.items()})
    return ''.join(p), summary


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('run_dir')
    ap.add_argument('--output', default=None)
    args = ap.parse_args()
    out = args.output or os.path.join(args.run_dir, 'report.html')
    if not os.path.isfile(os.path.join(args.run_dir, 'manifest.csv')):
        sys.exit('Error: manifest.csv not found; has detection finished?')
    doc, counts = build(args.run_dir)
    with open(out + '.tmp', 'w') as f:
        f.write(doc)
    os.replace(out + '.tmp', out)
    print(f'Report: {out} ({os.path.getsize(out) / 1e6:.1f} MB)')
    print('  ' + ', '.join(f'{k}={v}' for k, v in counts.items()))


if __name__ == '__main__':
    main()
