#!/usr/bin/env python3
"""find_anat.py — Detect non-DICOM structural MRI and PET neuroimaging files.

Walk a directory tree and identify files that require face de-identification:
  - Structural MRI : T1w, T2w, FLAIR, MPRAGE, MP2RAGE, PDw, T2*, …
  - PET            : FDG, amyloid (PiB, florbetapir, florbetaben, flutemetamol),
                     tau (flortaucipir, MK-6240), and generic PET volumes.

Designed for use with mri_reface (Mayo Clinic), which supports T1, T2, FLAIR,
T2*, and ASL MRI plus Amyloid PET, tau PET, FDG PET, and CT.
(https://www.nitrc.org/projects/mri_reface)

Formats detected
----------------
  NIfTI   : .nii, .nii.gz
  MINC    : .mnc, .mnc2
  MGH/MGZ : .mgh, .mgz   (FreeSurfer)
  NRRD    : .nrrd, .nhdr
  ANALYZE : .hdr (ANALYZE 7.5, paired with .img sidecar)

Classification types (CSV Type column)
---------------------------------------
  structural_high       — BIDS anat/ directory, BIDS modality suffix (_T1w,
                          _T2w, _FLAIR, …), or scanner trade name (MPRAGE, …)
  structural_medium     — Generic patterns (T1, T2, FLAIR, anat, structural);
                          3D NIfTI with no modality pattern; MINC/MGH/MGZ
  structural_unclassified — Neuroimaging extension only; no structural evidence
                          (written only with --include-unclassified)
  pet_high              — BIDS _pet suffix, pet/ directory, tracer names (FDG,
                          PiB, florbetapir, SUVR, …), or _trc- BIDS entity
  pet_medium            — Generic \bpet\b keyword in filename
  non_structural        — Explicit func/bold/dwi/fmap indicators
                          (written only with --include-non-structural)

Output CSV columns
------------------
  Type, Root, RelativePath, Format, MatchReason, Dimensions

Usage
-----
  python find_anat.py <root_dir> <output_csv>
                      [--include-unclassified]
                      [--include-non-structural]
                      [--skip-nibabel]
                      [--permission-report <path>]
"""

import argparse
import csv
import os
import re
import stat as stat_mod
import sys
import tarfile
import zipfile
from collections import defaultdict

# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------

try:
    import pwd
    import grp
    _HAS_UNIX_OWNER = True
except ImportError:
    _HAS_UNIX_OWNER = False

try:
    import nibabel as nib
    _HAS_NIBABEL = True
except ImportError:
    nib = None
    _HAS_NIBABEL = False

# ---------------------------------------------------------------------------
# Format definitions
# ---------------------------------------------------------------------------

# Compound extensions must be checked BEFORE single extensions.
_COMPOUND_EXTS = {
    '.nii.gz': 'NIfTI',
    '.mnc.gz': 'MINC',
}

# Single extensions
_SINGLE_EXTS = {
    '.nii':  'NIfTI',
    '.mnc':  'MINC',
    '.mnc2': 'MINC',
    '.mgh':  'MGH',
    '.mgz':  'MGZ',
    '.nrrd': 'NRRD',
    '.nhdr': 'NRRD',
    '.hdr':  'ANALYZE',
}


def _get_format(filename: str):
    """Return (format_name, stem) or (None, None) if not a recognised format."""
    lower = filename.lower()
    for ext, fmt in _COMPOUND_EXTS.items():
        if lower.endswith(ext):
            return fmt, filename[: -len(ext)]
    for ext, fmt in _SINGLE_EXTS.items():
        if lower.endswith(ext):
            return fmt, filename[: -len(ext)]
    return None, None


# ---------------------------------------------------------------------------
# Structural / non-structural patterns
# ---------------------------------------------------------------------------

# High-confidence — BIDS modality suffixes and scanner trade/sequence names
_HIGH_PATTERNS = [
    # BIDS suffixes: underscore before, then dot-or-end
    re.compile(r'_T1w(?:\.|$)',         re.IGNORECASE),
    re.compile(r'_T2w(?:\.|$)',         re.IGNORECASE),
    re.compile(r'_FLAIR(?:\.|$)',       re.IGNORECASE),
    re.compile(r'_PDw(?:\.|$)',         re.IGNORECASE),
    re.compile(r'_T2starw(?:\.|$)',     re.IGNORECASE),
    re.compile(r'_T1rho(?:\.|$)',       re.IGNORECASE),
    re.compile(r'_T2star(?:\.|$)',      re.IGNORECASE),
    re.compile(r'_angio(?:\.|$)',       re.IGNORECASE),
    re.compile(r'_inplaneT1(?:\.|$)',   re.IGNORECASE),
    re.compile(r'_inplaneT2(?:\.|$)',   re.IGNORECASE),
    # Scanner / sequence trade names
    re.compile(r'\bMEMP2RAGE\b',        re.IGNORECASE),
    re.compile(r'\bMP2RAGE\b',          re.IGNORECASE),
    re.compile(r'\bMEMPRAGE\b',         re.IGNORECASE),
    re.compile(r'\bMPRAGE\b',           re.IGNORECASE),
    re.compile(r'\bSPGR\b',             re.IGNORECASE),
    re.compile(r'\bFLASH\b',            re.IGNORECASE),
    re.compile(r'\bGRE\b',              re.IGNORECASE),
    re.compile(r'\bIR-FSPGR\b',         re.IGNORECASE),
    re.compile(r'\bBRAVO\b',            re.IGNORECASE),
]

# Medium-confidence — generic but suggestive labels
_MEDIUM_PATTERNS = [
    re.compile(r'\bT1\b',              re.IGNORECASE),
    re.compile(r'\bT2\b',              re.IGNORECASE),
    re.compile(r'\bFLAIR\b',           re.IGNORECASE),
    re.compile(r'\bPD\b'),                              # uppercase only
    re.compile(r'\banat\b',            re.IGNORECASE),
    re.compile(r'\bstructur',          re.IGNORECASE),  # structural/structure
    re.compile(r'\banatomic',          re.IGNORECASE),  # anatomical/anatomy
    re.compile(r'\binversion',         re.IGNORECASE),
    re.compile(r'\bwhole.?brain\b',    re.IGNORECASE),
    re.compile(r'\bhighres\b',         re.IGNORECASE),
    re.compile(r'\bhigh.?res\b',       re.IGNORECASE),
]

# Patterns that strongly indicate NON-structural content
_NON_STRUCT_PATTERNS = [
    # Functional MRI
    re.compile(r'_bold(?:\.|$)',        re.IGNORECASE),
    re.compile(r'_sbref(?:\.|$)',       re.IGNORECASE),
    re.compile(r'_epi(?:\.|$)',         re.IGNORECASE),
    re.compile(r'\bfunc\b',             re.IGNORECASE),
    re.compile(r'\bbold\b',             re.IGNORECASE),
    # Diffusion
    re.compile(r'_dwi(?:\.|$)',         re.IGNORECASE),
    re.compile(r'\bdwi\b',              re.IGNORECASE),
    re.compile(r'\bdti\b',              re.IGNORECASE),
    re.compile(r'\btbss\b',             re.IGNORECASE),
    # Perfusion / ASL
    re.compile(r'_asl(?:\.|$)',         re.IGNORECASE),
    re.compile(r'_perf(?:\.|$)',        re.IGNORECASE),
    re.compile(r'\basl\b',              re.IGNORECASE),
    re.compile(r'\bperf\b',             re.IGNORECASE),
    # Field maps
    re.compile(r'_phasediff(?:\.|$)',   re.IGNORECASE),
    re.compile(r'_phase[12](?:\.|$)',   re.IGNORECASE),
    re.compile(r'_magnitude[12]?(?:\.|$)', re.IGNORECASE),
    re.compile(r'_fieldmap(?:\.|$)',    re.IGNORECASE),
    re.compile(r'\bfmap\b',             re.IGNORECASE),
    re.compile(r'\bfieldmap\b',         re.IGNORECASE),
]

# High-confidence PET patterns (filename, except path patterns tested on full path)
_PET_HIGH_PATTERNS = [
    re.compile(r'_pet(?:\.|$)',          re.IGNORECASE),  # BIDS _pet suffix
    re.compile(r'_trc-',                 re.IGNORECASE),  # BIDS tracer entity
    re.compile(r'\bfdg\b',               re.IGNORECASE),  # FDG
    re.compile(r'\bpib\b',               re.IGNORECASE),  # Pittsburgh compound B
    re.compile(r'\bflorbetapir\b',       re.IGNORECASE),  # AV-45 / Amyvid
    re.compile(r'\bflorbetaben\b',       re.IGNORECASE),  # NeuraCeq
    re.compile(r'\bflutemetamol\b',      re.IGNORECASE),  # Vizamyl
    re.compile(r'\bflortaucipir\b',      re.IGNORECASE),  # AV-1451 / Tauvid
    re.compile(r'\bmk.?6240\b',          re.IGNORECASE),  # MK-6240 tau tracer
    re.compile(r'\bav.?45\b',            re.IGNORECASE),  # AV-45 = florbetapir
    re.compile(r'\bav.?1451\b',          re.IGNORECASE),  # AV-1451 = flortaucipir
    re.compile(r'\bsuv[rb]?\b',          re.IGNORECASE),  # SUV / SUVR / SUVB
    re.compile(r'\bpetct\b',             re.IGNORECASE),
    re.compile(r'\bpet.?mri\b',          re.IGNORECASE),  # PET/MRI hybrid
]

# Path-based PET patterns (tested against full filepath, not just basename)
_PET_PATH_PATTERNS = [
    re.compile(r'[\\/]pet[\\/]',         re.IGNORECASE),  # .../pet/... directory
]

# Medium-confidence PET (generic keyword only — could also appear in non-PET contexts)
_PET_MEDIUM_PATTERNS = [
    re.compile(r'\bpet\b',               re.IGNORECASE),
]

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _classify(filepath: str, fmt: str, use_nibabel: bool = True):
    """Classify a single neuroimaging file.

    Returns
    -------
    (confidence, reason, dims_str)
      confidence : 'high' | 'medium' | 'unclassified' | 'non_structural'
                   | 'pet_high' | 'pet_medium'
      reason     : human-readable string
      dims_str   : e.g. '256x256x176' or '' if unavailable
    """
    basename = os.path.basename(filepath)
    path_parts = re.split(r'[\\/]', filepath.lower())

    dims_str = ''
    ndim = None

    # ------------------------------------------------------------------
    # Optional NIfTI header inspection for dimensionality
    # ------------------------------------------------------------------
    if use_nibabel and _HAS_NIBABEL and fmt == 'NIfTI':
        try:
            img = nib.load(filepath)
            shape = img.header.get_data_shape()
            ndim = len(shape)
            dims_str = 'x'.join(str(d) for d in shape)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Non-structural fast exit
    # ------------------------------------------------------------------
    for pat in _NON_STRUCT_PATTERNS:
        if pat.search(basename):
            return 'non_structural', f'non-structural pattern: {pat.pattern}', dims_str

    # 4D volumes are almost never structural (functional, diffusion, etc.)
    if ndim is not None and ndim >= 4:
        return 'non_structural', f'4D NIfTI (ndim={ndim})', dims_str

    # ------------------------------------------------------------------
    # PET detection  (checked before structural so that co-registered
    # PET volumes with T1/T2 in the name are classified as PET, not MRI)
    # ------------------------------------------------------------------
    for pat in _PET_PATH_PATTERNS:
        if pat.search(filepath):
            return 'pet_high', f'PET path: {pat.pattern}', dims_str

    for pat in _PET_HIGH_PATTERNS:
        if pat.search(basename):
            return 'pet_high', f'PET high-conf: {pat.pattern}', dims_str

    for pat in _PET_MEDIUM_PATTERNS:
        if pat.search(basename):
            return 'pet_medium', f'PET medium-conf: {pat.pattern}', dims_str

    # ------------------------------------------------------------------
    # Structural evidence accumulation
    # ------------------------------------------------------------------
    reasons = []
    confidence = 'unclassified'

    # 1. BIDS anat/ directory anywhere in path
    if 'anat' in path_parts:
        reasons.append('BIDS anat/ directory')
        confidence = 'high'

    # 2. High-confidence filename patterns
    for pat in _HIGH_PATTERNS:
        if pat.search(basename):
            reasons.append(f'high-conf: {pat.pattern}')
            confidence = 'high'

    # 3. Medium-confidence filename patterns (only upgrade if not already high)
    if confidence != 'high':
        for pat in _MEDIUM_PATTERNS:
            if pat.search(basename):
                reasons.append(f'medium-conf: {pat.pattern}')
                confidence = 'medium'
                break  # first match sufficient

    # 4. Format-level heuristics (MINC/MGH/MGZ are almost always structural)
    if confidence == 'unclassified' and fmt in ('MINC', 'MGH', 'MGZ'):
        reasons.append(f'{fmt} format (typically structural)')
        confidence = 'medium'

    # 5. 3D NIfTI with no other indicator → medium
    if confidence == 'unclassified' and ndim == 3:
        reasons.append('3D NIfTI (no modality pattern)')
        confidence = 'medium'

    reason_str = '; '.join(reasons) if reasons else 'neuroimaging extension only'
    return confidence, reason_str, dims_str


# ---------------------------------------------------------------------------
# Archive inspection
# ---------------------------------------------------------------------------

# Compound extensions must be checked before single extensions.
_ARCHIVE_COMPOUND_EXTS = ['.tar.gz', '.tar.bz2', '.tar.xz']
_ARCHIVE_SINGLE_EXTS   = ['.tgz', '.tbz2', '.txz', '.tar', '.zip']


def _get_archive_type(filename):
    """Return 'tar' or 'zip' if *filename* is a recognised archive, else None."""
    lower = filename.lower()
    for ext in _ARCHIVE_COMPOUND_EXTS:
        if lower.endswith(ext):
            return 'tar'
    for ext in _ARCHIVE_SINGLE_EXTS:
        if lower.endswith(ext):
            return 'zip' if ext == '.zip' else 'tar'
    return None


def find_in_archive(archive_path, arch_type):
    """Inspect *archive_path* and return (records, permission_errors).

    Member filenames are classified using filename and path patterns only;
    nibabel cannot be used without extracting the entry.  Each returned
    record contains the same keys as a regular record plus 'archive_member'
    (the member's path within the archive).
    """
    records = []
    errors  = []
    try:
        if arch_type == 'zip':
            with zipfile.ZipFile(archive_path, 'r') as zf:
                for member_name in zf.namelist():
                    basename = os.path.basename(member_name)
                    if not basename or basename.startswith('.'):
                        continue
                    fmt, _ = _get_format(basename)
                    if fmt is None:
                        continue
                    confidence, reason, _ = _classify(member_name, fmt, use_nibabel=False)
                    records.append({
                        'filepath':       archive_path,
                        'archive_member': member_name,
                        'format':         fmt,
                        'confidence':     confidence,
                        'match_reason':   reason,
                        'dimensions':     '',
                    })
        else:
            # handles .tar, .tar.gz/.tgz, .tar.bz2/.tbz2, .tar.xz/.txz
            with tarfile.open(archive_path, 'r:*') as tf:
                for member in tf.getmembers():
                    if member.isdir():
                        continue
                    basename = os.path.basename(member.name)
                    if not basename or basename.startswith('.'):
                        continue
                    fmt, _ = _get_format(basename)
                    if fmt is None:
                        continue
                    confidence, reason, _ = _classify(member.name, fmt, use_nibabel=False)
                    records.append({
                        'filepath':       archive_path,
                        'archive_member': member.name,
                        'format':         fmt,
                        'confidence':     confidence,
                        'match_reason':   reason,
                        'dimensions':     '',
                    })
    except PermissionError:
        errors.append(archive_path)
        print(f"Permission denied (archive): {archive_path}", file=sys.stderr)
    except Exception as exc:
        print(f"Error inspecting archive {archive_path}: {exc}", file=sys.stderr)
        records.append({
            'filepath':       archive_path,
            'archive_member': '',
            'format':         '',
            'confidence':     'archive_unreadable',
            'match_reason':   str(exc),
            'dimensions':     '',
        })
    return records, errors


# ---------------------------------------------------------------------------
# Directory walk
# ---------------------------------------------------------------------------

def find_structural_files(root_dir, use_nibabel=True,
                          include_unclassified=False,
                          include_non_structural=False,
                          inspect_archives=True):
    """Walk *root_dir* and return lists of classified neuroimaging files.

    Returns
    -------
    high_conf, medium_conf, unclassified, non_structural,
    pet_high, pet_medium, permission_errors
      Each element is a list of dicts with keys:
        filepath, archive_member, format, confidence, match_reason, dimensions
      permission_errors is a list of path strings.
    """
    high_conf          = []
    medium_conf        = []
    unclassified_lst   = []
    non_structural     = []
    pet_high           = []
    pet_medium         = []
    archives_unreadable = []
    permission_errors  = []

    def _route(record):
        confidence = record['confidence']
        if confidence == 'high':
            high_conf.append(record)
        elif confidence == 'medium':
            medium_conf.append(record)
        elif confidence == 'pet_high':
            pet_high.append(record)
        elif confidence == 'pet_medium':
            pet_medium.append(record)
        elif confidence == 'non_structural':
            non_structural.append(record)
        elif confidence == 'archive_unreadable':
            archives_unreadable.append(record)
        else:
            unclassified_lst.append(record)

    def _walk_onerror(err):
        if isinstance(err, PermissionError):
            permission_errors.append(err.filename or str(err))
            print(f"Permission denied: {err.filename}", file=sys.stderr)
        else:
            print(f"Walk error: {err}", file=sys.stderr)

    for dirpath, dirnames, filenames in os.walk(root_dir, onerror=_walk_onerror):
        # Skip hidden directories in-place to avoid descending into them
        dirnames[:] = [d for d in dirnames if not d.startswith('.')]

        for fname in filenames:
            if fname.startswith('.'):
                continue

            filepath = os.path.join(dirpath, fname)

            # ---- Direct neuroimaging file ----
            fmt, _ = _get_format(fname)
            if fmt is not None:
                try:
                    confidence, reason, dims = _classify(filepath, fmt, use_nibabel)
                except PermissionError:
                    permission_errors.append(filepath)
                    print(f"Permission denied: {filepath}", file=sys.stderr)
                    continue
                except Exception as exc:
                    print(f"Error classifying {filepath}: {exc}", file=sys.stderr)
                    continue
                _route({
                    'filepath':       filepath,
                    'archive_member': '',
                    'format':         fmt,
                    'confidence':     confidence,
                    'match_reason':   reason,
                    'dimensions':     dims,
                })
                continue

            # ---- Archive inspection ----
            if inspect_archives:
                arch_type = _get_archive_type(fname)
                if arch_type is not None:
                    arch_records, arch_errors = find_in_archive(filepath, arch_type)
                    permission_errors.extend(arch_errors)
                    for record in arch_records:
                        _route(record)

    return high_conf, medium_conf, unclassified_lst, non_structural, pet_high, pet_medium, archives_unreadable, permission_errors


# ---------------------------------------------------------------------------
# mri_reface imType mapping
# ---------------------------------------------------------------------------

def _get_imtype(basename: str, confidence: str) -> str:
    """Return the mri_reface -imType value for a neuroimaging filename.

    Returns one of the types explicitly supported by mri_reface:
      T1 | T2 | PD | T2ST | FLAIR | FDG | PIB | FBP | TAU
    Returns empty string '' if the modality cannot be determined.
    Callers must route empty-string results to DELETE rather than DEFACE;
    passing AUTO to mri_reface is unreliable — it just checks the filename
    for recognised suffixes and errors if none match.
    """
    b = basename.lower()
    # T2* must be checked before plain T2 to avoid false match
    if re.search(r'_t2starw|_t2star', b):                     return 'T2ST'
    # BIDS / explicit suffixes — highest specificity first
    if re.search(r'_flair(?:\.|$)', b):                       return 'FLAIR'
    if re.search(r'_pdw(?:\.|$)', b):                         return 'PD'
    # T1-weighted sequences and scanner trade names
    if re.search(r'_t1w|_t1rho|_inplanet1'
                 r'|mprage|mp2rage|memp2rage|memprage'
                 r'|\bspgr\b|\bflash\b|\bgre\b|\bbravo\b|ir-fspgr', b):
        return 'T1'
    # T2-weighted
    if re.search(r'_t2w|_inplanet2', b):                      return 'T2'
    # PET tracers with known mri_reface types
    if confidence in ('pet_high', 'pet_medium'):
        if re.search(r'\bfdg\b', b):                          return 'FDG'
        if re.search(r'\bpib\b', b):                          return 'PIB'
        if re.search(r'florbetapir|florbetaben|flutemetamol|\bav.?45\b', b):
            return 'FBP'
        if re.search(r'flortaucipir|mk.?6240|av.?1451', b):   return 'TAU'
    # Medium-confidence fallbacks (word-boundary matches)
    if re.search(r'\bt1\b', b):                               return 'T1'
    if re.search(r'\bt2\b', b):                               return 'T2'
    if re.search(r'\bflair\b', b):                            return 'FLAIR'
    if re.search(r'\bpd\b', b):                               return 'PD'
    # Modality unknown — caller should route to DELETE, not DEFACE
    return ''


def _imtype_for_record(rec: dict) -> str:
    """Derive the mri_reface imType for a classification record."""
    fname = (os.path.basename(rec['archive_member'])
             if rec.get('archive_member')
             else os.path.basename(rec['filepath']))
    return _get_imtype(fname, rec['confidence'])


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

def write_results_to_csv(output_file, root_dir,
                         high_conf, medium_conf,
                         unclassified_lst, non_structural,
                         pet_high, pet_medium,
                         archives_unreadable,
                         permission_errors,
                         include_unclassified=False,
                         include_non_structural=False):
    """Write classified results to *output_file* in the same schema as find_dcm.py.

    Columns: Type, Root, RelativePath, Format, MatchReason, Dimensions, ArchiveMember

    PET and archive_unreadable records are always written. Structural
    unclassified and non_structural records are written only when the
    corresponding flag is True.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
    with open(output_file, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(['Type', 'Root', 'RelativePath', 'Format', 'MatchReason', 'ImType', 'Dimensions', 'ArchiveMember'])

        for rec in high_conf:
            writer.writerow([
                'structural_high',
                root_dir,
                os.path.relpath(rec['filepath'], root_dir),
                rec['format'],
                rec['match_reason'],
                _imtype_for_record(rec),
                rec['dimensions'],
                rec.get('archive_member', ''),
            ])

        for rec in medium_conf:
            writer.writerow([
                'structural_medium',
                root_dir,
                os.path.relpath(rec['filepath'], root_dir),
                rec['format'],
                rec['match_reason'],
                _imtype_for_record(rec),
                rec['dimensions'],
                rec.get('archive_member', ''),
            ])

        # PET records always written (needed for downstream mri_reface processing)
        for rec in pet_high:
            writer.writerow([
                'pet_high',
                root_dir,
                os.path.relpath(rec['filepath'], root_dir),
                rec['format'],
                rec['match_reason'],
                _imtype_for_record(rec),
                rec['dimensions'],
                rec.get('archive_member', ''),
            ])

        for rec in pet_medium:
            writer.writerow([
                'pet_medium',
                root_dir,
                os.path.relpath(rec['filepath'], root_dir),
                rec['format'],
                rec['match_reason'],
                _imtype_for_record(rec),
                rec['dimensions'],
                rec.get('archive_member', ''),
            ])

        if include_unclassified:
            for rec in unclassified_lst:
                writer.writerow([
                    'structural_unclassified',
                    root_dir,
                    os.path.relpath(rec['filepath'], root_dir),
                    rec['format'],
                    rec['match_reason'],
                    _imtype_for_record(rec),
                    rec['dimensions'],
                    rec.get('archive_member', ''),
                ])

        if include_non_structural:
            for rec in non_structural:
                writer.writerow([
                    'non_structural',
                    root_dir,
                    os.path.relpath(rec['filepath'], root_dir),
                    rec['format'],
                    rec['match_reason'],
                    _imtype_for_record(rec),
                    rec['dimensions'],
                    rec.get('archive_member', ''),
                ])

        for rec in archives_unreadable:
            writer.writerow([
                'archive_unreadable',
                root_dir,
                os.path.relpath(rec['filepath'], root_dir),
                '', rec['match_reason'], '', '', '',
            ])

        for perm_path in permission_errors:
            writer.writerow([
                'permission_denied',
                root_dir,
                os.path.relpath(perm_path, root_dir),
                '', '', '', '', '',
            ])


# ---------------------------------------------------------------------------
# Permission report (mirrors find_dcm.py)
# ---------------------------------------------------------------------------

def _resolve_owner(path):
    """Best-effort resolution of owner/group/mode for *path* or its parent."""
    for target in (path, os.path.dirname(path)):
        try:
            st = os.stat(target)
        except (PermissionError, FileNotFoundError, OSError):
            continue
        uid, gid = st.st_uid, st.st_gid
        mode = stat_mod.filemode(st.st_mode)
        if _HAS_UNIX_OWNER:
            try:
                user = pwd.getpwuid(uid).pw_name
            except KeyError:
                user = str(uid)
            try:
                group = grp.getgrgid(gid).gr_name
            except KeyError:
                group = str(gid)
        else:
            user, group = str(uid), str(gid)
        return user, group, uid, gid, mode
    return 'unknown', 'unknown', -1, -1, '??????????'


def write_permission_report(report_path, root_dir, permission_errors):
    """Write a human-readable permission report grouped by owner (+ CSV)."""
    if not permission_errors:
        return None, None

    by_owner = defaultdict(list)
    records = []
    for p in sorted(permission_errors):
        user, group, uid, gid, mode = _resolve_owner(p)
        rel = os.path.relpath(p, root_dir)
        by_owner[(user, group)].append((rel, mode))
        records.append((user, group, uid, gid, mode, rel))

    with open(report_path, 'w') as f:
        f.write('=' * 72 + '\n')
        f.write('PERMISSION-DENIED SUMMARY REPORT\n')
        f.write(f'Root searched : {root_dir}\n')
        f.write(f'Total blocked : {len(permission_errors)} path(s)\n')
        f.write(f'Unique owners : {len(by_owner)}\n')
        f.write('=' * 72 + '\n\n')
        for (user, group), entries in sorted(by_owner.items(),
                                             key=lambda kv: (-len(kv[1]), kv[0])):
            f.write(f'--- Owner: {user}  Group: {group}  ({len(entries)} path(s)) ---\n')
            for rel, mode in entries:
                f.write(f'  {mode}  {rel}\n')
            f.write('\n')
        f.write('-' * 72 + '\n')
        f.write('Suggested fix (run as owner or root):\n')
        f.write('  chmod -R g+rX <path>\n')

    csv_path = report_path + '.csv'
    with open(csv_path, 'w', newline='') as cf:
        writer = csv.writer(cf)
        writer.writerow(['Owner', 'Group', 'UID', 'GID', 'Mode', 'RelativePath'])
        for rec in records:
            writer.writerow(rec)

    return report_path, csv_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Detect non-DICOM structural MRI and PET neuroimaging files.',  
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('root_dir',   help='Root directory to search.')
    parser.add_argument('output_csv', help='Path for the output CSV report.')
    parser.add_argument('--include-unclassified', action='store_true',
                        help='Include neuroimaging files with no structural indicators.')
    parser.add_argument('--include-non-structural', action='store_true',
                        help='Include files with explicit non-structural indicators.')
    parser.add_argument('--skip-nibabel', action='store_true',
                        help='Skip nibabel header inspection (faster, less accurate).')
    parser.add_argument('--skip-archives', action='store_true',
                        help='Do not inspect archive files (.tar, .tar.gz, .zip, …) '
                             'for neuroimaging members (faster, but misses archived files).')
    parser.add_argument('--permission-report', default=None,
                        help='Path for the permission-denied summary. '
                             'Defaults to <output_csv_stem>_permissions.txt')
    args = parser.parse_args()
    #!#debug#!#
    print(args.root_dir)
    #!#debug#!#
    if not os.path.isdir(args.root_dir):
        print(f"Error: root directory does not exist: {args.root_dir}", file=sys.stderr)
        sys.exit(1)

    use_nibabel = (not args.skip_nibabel)

    if use_nibabel and not _HAS_NIBABEL:
        print("Warning: nibabel not available; skipping header inspection.", file=sys.stderr)
        use_nibabel = False

    perm_report = args.permission_report
    if perm_report is None:
        stem = os.path.splitext(args.output_csv)[0]
        perm_report = stem + '_permissions.txt'

    print(f"Searching for structural MRI and PET files in: {args.root_dir}")
    print(f"nibabel header inspection: {'enabled' if use_nibabel else 'disabled'}")
    print(f"archive inspection:        {'enabled' if not args.skip_archives else 'disabled'}")

    inspect_archives = not args.skip_archives
    high_conf, medium_conf, unclassified_lst, non_structural, pet_high, pet_medium, archives_unreadable, permission_errors = \
        find_structural_files(
            args.root_dir,
            use_nibabel=use_nibabel,
            include_unclassified=args.include_unclassified,
            include_non_structural=args.include_non_structural,
            inspect_archives=inspect_archives,
        )

    write_results_to_csv(
        args.output_csv, args.root_dir,
        high_conf, medium_conf, unclassified_lst, non_structural,
        pet_high, pet_medium, archives_unreadable, permission_errors,
        include_unclassified=args.include_unclassified,
        include_non_structural=args.include_non_structural,
    )

    if permission_errors:
        result = write_permission_report(perm_report, args.root_dir, permission_errors)
        if result[0]:
            print(f"Permission report : {result[0]}")
            print(f"Permission CSV    : {result[1]}")

    n_high         = len(high_conf)
    n_medium       = len(medium_conf)
    n_unclassified = len(unclassified_lst)
    n_non          = len(non_structural)
    n_pet_high     = len(pet_high)
    n_pet_medium   = len(pet_medium)
    n_unreadable   = len(archives_unreadable)
    n_perm         = len(permission_errors)

    print()
    print('Summary')
    print('-------')
    print(f'  structural_high          : {n_high}')
    print(f'  structural_medium        : {n_medium}')
    print(f'  structural_unclassified  : {n_unclassified}   (--include-unclassified to write)')
    print(f'  pet_high                 : {n_pet_high}')
    print(f'  pet_medium               : {n_pet_medium}')
    print(f'  non_structural           : {n_non}   (--include-non-structural to write)')
    print(f'  archive_unreadable       : {n_unreadable}')
    print(f'  permission_denied        : {n_perm}')
    print(f'  nibabel available        : {_HAS_NIBABEL}')
    print(f'  archives inspected       : {inspect_archives}')
    print()
    print(f'Results written to: {args.output_csv}')

    # Format breakdown for written records
    written = high_conf + medium_conf + pet_high + pet_medium
    if args.include_unclassified:
        written += unclassified_lst
    if args.include_non_structural:
        written += non_structural

    fmt_counts = defaultdict(int)
    for rec in written:
        fmt_counts[rec['format']] += 1
    if fmt_counts:
        print('Format breakdown (written records):')
        for fmt, count in sorted(fmt_counts.items(), key=lambda x: -x[1]):
            print(f'  {fmt:<12} : {count}')


if __name__ == '__main__':
    main()
