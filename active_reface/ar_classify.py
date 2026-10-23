"""ar_classify.py — Evidence-based classification of neuroimaging files.

Successor to the regex rules in ../find_anat.py, used only by active_reface
(the legacy pipeline keeps its own rules).  Differences:

  * Tokens are delimited by anything that is not a letter or digit, so
    '_', '-', '.', '/' and spaces all separate words ("T1_MPRAGE_SAG" matches
    T1 and MPRAGE; find_anat's \\b treated '_' as part of a word).
  * Evidence is collected from the filename, the parent directory, the BIDS
    datatype directory, the JSON sidecar (SeriesDescription, ProtocolName,
    SequenceName, TracerName, Modality, ...) and the NIfTI header, then
    weighed, instead of first-pattern-wins.
  * 4D volumes are only excluded when nothing marks them as anatomical or PET
    (multi-echo MEMPRAGE / dynamic PET are kept).
  * A 3D NIfTI counts as structural on header evidence alone only when its
    voxels are fine (<= 1.5 mm); coarse 3D volumes with no other evidence are
    'unclassified' (typically stat maps or functional derivatives).
  * Face-free derivatives (masks, segmentations, skull-stripped images,
    transforms, stat maps, FreeSurfer label volumes) are recognised with
    patterns anchored to the end of the name or to SPM/FSL prefixes.
"""

import json
import os
import re

try:
    import nibabel as nib
except ImportError:
    nib = None

# ---------------------------------------------------------------------------
# Formats
# ---------------------------------------------------------------------------

_EXTS = [  # compound first
    ('.nii.gz', 'NIfTI'), ('.mnc.gz', 'MINC'), ('.nii', 'NIfTI'), ('.mnc', 'MINC'),
    ('.mnc2', 'MINC'), ('.mgh', 'MGH'), ('.mgz', 'MGZ'), ('.nrrd', 'NRRD'),
    ('.nhdr', 'NRRD'), ('.hdr', 'ANALYZE'),
]
_ARCHIVE_EXTS = [('.tar.gz', 'tar'), ('.tar.bz2', 'tar'), ('.tar.xz', 'tar'), ('.tgz', 'tar'),
                 ('.tbz2', 'tar'), ('.txz', 'tar'), ('.tar', 'tar'), ('.zip', 'zip')]


def get_format(filename):
    """Return (format, stem) or (None, None)."""
    low = filename.lower()
    for ext, fmt in _EXTS:
        if low.endswith(ext):
            return fmt, filename[:-len(ext)]
    return None, None


def get_archive_type(filename):
    low = filename.lower()
    for ext, kind in _ARCHIVE_EXTS:
        if low.endswith(ext):
            return kind
    return None


# ---------------------------------------------------------------------------
# Pattern helpers
# ---------------------------------------------------------------------------

def tok(word):
    """Match `word` as a whole token: not preceded or followed by a letter/digit."""
    return re.compile(r'(?<![a-z0-9])(?:' + word + r')(?![a-z0-9])', re.IGNORECASE)


def sub(word):
    """Match `word` anywhere (for long, unambiguous sequence names)."""
    return re.compile(word, re.IGNORECASE)


def suffix(words):
    """BIDS-style suffix: _<word> at the very end of the stem."""
    return re.compile(r'_(?:' + words + r')$', re.IGNORECASE)


# Each entry: (pattern, imtype-or-None, level)
STRUCT_PATTERNS = [
    (suffix('t2starw|t2star|megre'), 'T2ST', 'high'),
    (suffix('flair'), 'FLAIR', 'high'),
    (suffix('pdw|pdt2'), 'PD', 'high'),
    (suffix('t1w|t1rho|inplanet1|unit1'), 'T1', 'high'),
    (suffix('t2w|inplanet2|mese'), 'T2', 'high'),
    (suffix('angio|t1map|t2map|t2starmap|r1map|r2map|pdmap|mp2rage|mpm|uniden'), None, 'high'),
    (tok('t2star|t2starw|t2\\*'), 'T2ST', 'high'),
    (sub('flair'), 'FLAIR', 'high'),
    (sub('memprage|mprage|mp2rage|memp2rage|mprg'), 'T1', 'high'),
    (sub('fspgr|spgr|bravo'), 'T1', 'high'),
    (tok('t1w|t1_?weighted|t1mpr'), 'T1', 'high'),
    (tok('t2w|t2_?weighted'), 'T2', 'high'),
    (tok('pdw|pd_?weighted'), 'PD', 'high'),
    (tok('t1'), 'T1', 'medium'),
    (tok('t2'), 'T2', 'medium'),
    (tok('flash|gre'), 'T1', 'medium'),
    (tok('anat|anatomy|anatomical|structural|struct|highres|hires|high_res|whole_?brain'), None, 'medium'),
    (tok('localizer|localiser|scout|aahead|survey'), None, 'medium'),
]

PET_PATTERNS = [
    (suffix('pet'), None, 'high'),
    (re.compile(r'(?<![a-z0-9])trc-', re.IGNORECASE), None, 'high'),
    (tok('fdg'), 'FDG', 'high'),
    (tok('pib|nav_?-?4694'), 'PIB', 'high'),
    (sub('florbetapir|florbetaben|flutemetamol'), 'FBP', 'high'),
    (tok('av_?-?45|fbp|fbb|fmm|amyloid'), 'FBP', 'high'),
    (sub('flortaucipir'), 'TAU', 'high'),
    (tok('av_?-?1451|ftp|mk_?-?6240|pi_?-?2620|tau'), 'TAU', 'high'),
    (tok('suv|suvr|suvb|petct|pet_?mri?|petmr'), None, 'high'),
    (tok('pet'), None, 'medium'),
]

NONSTRUCT_PATTERNS = [
    suffix('bold|sbref|epi|dwi|asl|m0scan|perf|cbf|cbv|phasediff|phase1|phase2|phase|'
           'magnitude|magnitude1|magnitude2|fieldmap|physio|events|stim'),
    re.compile(r'(?<![a-z0-9])task-', re.IGNORECASE),
    tok('bold|func|fmri|rest|resting|rsfmri|ep2d|epi|sbref'),
    tok('dwi|dti|diff|diffusion|tbss|hardi|dsi|fa|md|adc|trace'),
    tok('asl|pcasl|pasl|perf|perfusion|cbf'),
    tok('fmap|fieldmap|field_?map|field_?mapping|b0map|topup|phasediff'),
]

BIDS_NONSTRUCT_DIRS = {'func', 'dwi', 'fmap', 'perf', 'meg', 'eeg', 'ieeg', 'beh', 'motion', 'nirs'}

# Face-free derivatives.  Stem-end patterns; SPM/FSL prefixes; FreeSurfer names.
_NO_FACE_END = re.compile(
    r'(?:^|[_\-.])(?:brain|brainmask|brain_?mask|mask|dseg|probseg|seg|pveseg|pve_?\d|'
    r'mixeltype|xfm|warp|warpfield|jacobian|jac|labels?|aseg|wmparc|ribbon|'
    r'skull_?stripped|skullstrip|ss|bet|restore_brain|brain_restore)$', re.IGNORECASE)
_NO_FACE_ANY = [
    re.compile(r'(?<![a-z0-9])desc-(?:brain|skullstripped|ss|bet|aseg|aparcaseg)', re.IGNORECASE),
    re.compile(r'(?<![a-z0-9])label-', re.IGNORECASE),
    re.compile(r'^(?:m?w?c|rc|wrc)[1-6](?=[a-z])', re.IGNORECASE),      # SPM c1T1, wc1sub-01
    re.compile(r'^i?y_', re.IGNORECASE),                                 # SPM deformations
    re.compile(r'(?<![a-z0-9])(?:z|t|f)stat\d*(?![a-z])', re.IGNORECASE),  # FSL stats
    re.compile(r'(?<![a-z0-9])(?:var)?cope\d+(?![a-z])', re.IGNORECASE),
    re.compile(r'(?<![a-z0-9])pe\d+(?![a-z])', re.IGNORECASE),
    re.compile(r'^(?:spm[tf]|con|beta|ess|resms|rpv|mask)_?\d*$', re.IGNORECASE),  # SPM stats
    re.compile(r'(?<![a-z0-9])(?:res4d|sigmasquareds|mean_func|example_func|thresh_zstat\d*)$', re.IGNORECASE),
    re.compile(r'aparc', re.IGNORECASE),
]
# FreeSurfer subject mri/ volumes without a face (orig, rawavg, nu, T1, T2,
# FLAIR, 001.mgz keep the face).
_FS_NO_FACE = {'brain', 'brainmask', 'brain.finalsurfs', 'brain.finalsurfs.manedit', 'aseg',
               'aseg.auto', 'aseg.auto_noccseg', 'aseg.presurf', 'aseg.presurf.hypos', 'wm',
               'wm.seg', 'wm.asegedit', 'filled', 'norm', 'ribbon', 'lh.ribbon', 'rh.ribbon',
               'ctrl_pts', 'talairach.m3z', 'antsdn.brain', 'synthseg', 'surface.defects'}

SIDECAR_TEXT_FIELDS = ('SeriesDescription', 'ProtocolName', 'SequenceName',
                       'PulseSequenceDetails', 'PulseSequenceType')


def no_face_reason(stem, rel_dir=''):
    base = os.path.basename(stem)
    if os.path.basename(rel_dir).lower() == 'mri' and base.lower() in _FS_NO_FACE:
        return 'FreeSurfer face-free volume'
    if _NO_FACE_END.search(base):
        return f'face-free derivative ({_NO_FACE_END.search(base).group(0).strip("_-.")})'
    for pat in _NO_FACE_ANY:
        m = pat.search(base)
        if m:
            return f'face-free derivative ({m.group(0)})'
    return None


# ---------------------------------------------------------------------------
# Evidence sources
# ---------------------------------------------------------------------------

def read_sidecar(path):
    """Return the BIDS JSON sidecar next to a NIfTI (or {})."""
    fmt, stem = get_format(os.path.basename(path))
    if fmt != 'NIfTI':
        return {}
    js = os.path.join(os.path.dirname(path), stem + '.json')
    try:
        with open(js) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_header(path):
    """Return (shape, zooms) from a NIfTI header, or (None, None)."""
    if nib is None:
        return None, None
    try:
        hdr = nib.load(path).header
        return tuple(int(x) for x in hdr.get_data_shape()), tuple(float(z) for z in hdr.get_zooms())
    except Exception:
        return None, None


def _scan(text, patterns):
    """Return [(pattern, imtype, level), ...] matches in text."""
    return [(p, im, lvl) for p, im, lvl in patterns if p.search(text)]


def _first_imtype(hits):
    for _, im, _lvl in hits:
        if im:
            return im
    return ''


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify(rel_path, fmt, shape=None, zooms=None, sidecar=None):
    """Classify one file.

    Returns dict(category, confidence, imtype, reasons) where category is
    'structural' | 'pet' | 'non_structural' | 'unclassified' and confidence is
    'high' | 'medium' | ''.
    """
    sidecar = sidecar or {}
    _, stem = get_format(os.path.basename(rel_path))
    stem = stem or os.path.basename(rel_path)
    parts = [p for p in re.split(r'[\\/]', os.path.dirname(rel_path)) if p]
    parent = parts[-1] if parts else ''
    dirs_lower = {p.lower() for p in parts}
    side_text = ' | '.join(str(sidecar.get(k, '')) for k in SIDECAR_TEXT_FIELDS if sidecar.get(k))
    reasons = []

    st_name = _scan(stem, STRUCT_PATTERNS)
    pt_name = _scan(stem, PET_PATTERNS)
    st_side = _scan(side_text, STRUCT_PATTERNS) if side_text else []
    pt_side = _scan(side_text, PET_PATTERNS) if side_text else []
    st_dir = _scan(parent, STRUCT_PATTERNS) if parent else []
    pt_dir = _scan(parent, PET_PATTERNS) if parent else []
    if sidecar.get('TracerName') or str(sidecar.get('Modality', '')).upper() in ('PT', 'PET'):
        tracer = str(sidecar.get('TracerName', ''))
        pt_side.append((None, _first_imtype(_scan(tracer, PET_PATTERNS)), 'high'))
        reasons.append(f'sidecar PET (TracerName={tracer or "?"}, Modality={sidecar.get("Modality", "")})')

    ns_name = [p for p in NONSTRUCT_PATTERNS if p.search(stem)]
    ns_side = [p for p in NONSTRUCT_PATTERNS if side_text and p.search(side_text)]
    ns_dir = sorted(dirs_lower & BIDS_NONSTRUCT_DIRS)
    in_anat = 'anat' in dirs_lower
    in_pet = 'pet' in dirs_lower

    def note(label, hits):
        for p, _im, lvl in hits:
            if p is not None:
                reasons.append(f'{label} {lvl}: {p.pattern}')

    st_high_name = any(l == 'high' for _, _, l in st_name)
    explicit_name = st_high_name or bool(pt_name)

    # 1. Explicit non-structural markers win unless the filename itself says anatomical/PET.
    if (ns_name or ns_dir or (ns_side and not st_side and not pt_side)) and not explicit_name:
        why = ([f'filename: {p.pattern}' for p in ns_name] + [f'BIDS {d}/ dir' for d in ns_dir]
               + [f'sidecar: {p.pattern}' for p in ns_side])
        return {'category': 'non_structural', 'confidence': '', 'imtype': '',
                'reasons': ['non-structural ' + '; '.join(why[:3])]}

    is_4d = shape is not None and len(shape) >= 4 and any(s > 1 for s in shape[3:])
    any_struct = st_name or st_side or st_dir or in_anat
    any_pet = pt_name or pt_side or pt_dir or in_pet

    # 2. 4D without anatomical/PET evidence.
    if is_4d and not (any_struct or any_pet):
        return {'category': 'non_structural', 'confidence': '', 'imtype': '',
                'reasons': [f'4D volume {shape} with no anatomical/PET evidence']}

    if is_4d:
        reasons.append(f'4D volume {shape} kept: anatomical/PET evidence')

    # 3. PET (before structural: co-registered PET often carries "T1" in its name).
    if any_pet:
        note('filename PET', pt_name)
        note('sidecar PET', [h for h in pt_side if h[0] is not None])
        note('parent dir PET', pt_dir)
        if in_pet:
            reasons.append('BIDS pet/ dir')
        level = 'high' if (in_pet or any(l == 'high' for _, _, l in pt_name + pt_side + pt_dir)) else 'medium'
        imtype = _first_imtype(pt_name) or _first_imtype(pt_side) or _first_imtype(pt_dir)
        return {'category': 'pet', 'confidence': level, 'imtype': imtype, 'reasons': reasons}

    # 4. Structural.
    if any_struct:
        if in_anat:
            reasons.append('BIDS anat/ dir')
        note('filename', st_name)
        note('sidecar', st_side)
        note('parent dir', st_dir)
        level = 'high' if (in_anat or st_high_name or any(l == 'high' for _, _, l in st_side)) else 'medium'
        # Parent-dir names like T1/T2 are often timepoints, so only trust
        # high-confidence directory names (e.g. T1_MPRAGE_0003/) for imType.
        imtype = (_first_imtype(st_name) or _first_imtype(st_side)
                  or _first_imtype([h for h in st_dir if h[2] == 'high']))
        return {'category': 'structural', 'confidence': level, 'imtype': imtype, 'reasons': reasons}

    # 5. Header-only evidence: fine-resolution 3D volume.
    if shape is not None and zooms and len(shape) >= 3:
        vox = max(zooms[:3])
        if vox <= 1.5 and min(shape[:3]) >= 64:
            return {'category': 'structural', 'confidence': 'medium', 'imtype': '',
                    'reasons': [f'3D volume {shape[:3]} at {vox:.2f} mm (no modality in name/sidecar)']}
        reasons.append(f'volume {shape} at {vox:.2f} mm, no anatomical evidence')

    # 6. Formats that are almost always structural.
    if fmt in ('MINC', 'MGH', 'MGZ'):
        return {'category': 'structural', 'confidence': 'medium', 'imtype': '',
                'reasons': [f'{fmt} format (typically structural)']}

    return {'category': 'unclassified', 'confidence': '', 'imtype': '',
            'reasons': reasons or ['neuroimaging extension only']}
