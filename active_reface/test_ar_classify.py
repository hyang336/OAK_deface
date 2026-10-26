#!/usr/bin/env python3
"""Regression cases for ar_classify.  Run: python test_ar_classify.py

Each case is a file name/path (plus optional header shape/zooms and sidecar)
with the expected (category, imType).  Add a case whenever a real dataset
turns up a misclassification.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ar_classify as c  # noqa: E402

T1_HDR = ((176, 256, 256), (1.0, 1.0, 1.0))
BOLD_HDR = ((86, 102, 90, 310), (1.8, 1.8, 1.8, 2.0))
FMP = 'derivatives/fmriprep2525/sub-AE236Y'

CLASSIFY = [
    # rel_path, header, sidecar, expected (category, imtype)
    ('raw/T1_MPRAGE_SAG_0003.nii.gz', T1_HDR, {}, ('structural', 'T1')),
    ('raw/t1_mprage_sag_p2_iso.nii', None, {}, ('structural', 'T1')),
    ('raw/s002_t2_space.nii.gz', None, {}, ('structural', 'T2')),
    ('raw/sub01_T2_FLAIR.nii', None, {}, ('structural', 'FLAIR')),
    ('raw/rest_bold_run1.nii.gz', BOLD_HDR, {}, ('non_structural', '')),
    ('sub-01/func/sub-01_task-rest_bold.nii.gz', None, {}, ('non_structural', '')),
    ('sub-01/fmap/sub-01_magnitude1.nii.gz', None, {}, ('non_structural', '')),
    ('raw/gre_field_mapping_0005.nii', None, {}, ('non_structural', '')),
    ('raw/FDG_PET_static.nii', None, {}, ('pet', 'FDG')),
    ('raw/sub01_fdg_suvr.nii', None, {}, ('pet', 'FDG')),
    ('sub-01/pet/sub-01_trc-PiB_pet.nii.gz', ((128, 128, 90, 20), (2, 2, 2, 1)), {'TracerName': 'PiB'}, ('pet', 'PIB')),
    ('sub-01/anat/sub-01_echo-1_MEMPRAGE.nii.gz', ((176, 256, 256, 4), (1, 1, 1, 1)), {}, ('structural', 'T1')),
    ('raw/series_0003.nii.gz', T1_HDR, {'SeriesDescription': 'T1_MPRAGE_SAG'}, ('structural', 'T1')),
    ('raw/series_0004.nii.gz', BOLD_HDR, {'SeriesDescription': 'ep2d_bold_rest'}, ('non_structural', '')),
    ('raw/series_0005.nii.gz', T1_HDR, {}, ('structural', '')),
    ('feat/stats/zstat1.nii.gz', ((91, 109, 91), (2, 2, 2)), {}, ('unclassified', '')),
    ('ses-T2/sub01_struct.nii.gz', None, {}, ('structural', '')),
    ('PD_001/PD_001.nii.gz', ((64, 64, 36), (3, 3, 3)), {}, ('unclassified', '')),
    ('T1_MPRAGE_vNav_0003/image.nii', None, {}, ('structural', 'T1')),
    # fMRIPrep: space-T1w is a reference entity, the suffix decides
    (f'{FMP}/func/sub-AE236Y_task-study_acq-blk1_space-T1w_boldref.nii.gz', None, {}, ('non_structural', '')),
    (f'{FMP}/func/sub-AE236Y_task-study_acq-blk1_space-T1w_desc-preproc_bold.nii.gz', BOLD_HDR, {}, ('non_structural', '')),
    (f'{FMP}/anat/sub-AE236Y_desc-preproc_T1w.nii.gz', T1_HDR, {}, ('structural', 'T1')),
    (f'{FMP}/anat/sub-AE236Y_space-MNI152NLin2009cAsym_res-2_desc-preproc_T1w.nii.gz', None, {}, ('structural', 'T1')),
    ('sub-01/anat/sub-01_acq-MPRAGE_T1w.nii.gz', None, {}, ('structural', 'T1')),
    ('sub-01/anat/sub-01_FLAIR.nii.gz', None, {}, ('structural', 'FLAIR')),
    # GE dcm2niix output
    ('tmp/001_33788_X_i00001.nii.gz', ((256, 256, 1), (1, 1, 5)), {'SeriesDescription': '3Plane Loc fgre_blk1'}, ('structural', '')),
    ('tmp/003_33788_X_fieldmaphz.nii.gz', None, {'ImageType': ['ORIGINAL', 'FIELDMAPHZ']}, ('non_structural', '')),
]

NO_FACE = [  # (stem, rel_dir, fmt)
    ('sub-01_desc-brain_mask', '', 'NIfTI'), ('sub-01_desc-aseg_dseg', '', 'NIfTI'),
    ('T1_brain', '', 'NIfTI'), ('highres_brain_mask', '', 'NIfTI'), ('c1T1', '', 'NIfTI'),
    ('wc1sub-01_T1w', '', 'NIfTI'), ('y_sub-01_T1w', '', 'NIfTI'), ('zstat1', '', 'NIfTI'),
    ('cope3', '', 'NIfTI'), ('spmT_0001', '', 'NIfTI'), ('con_0002', '', 'NIfTI'),
    ('sub-01_label-GM_probseg', '', 'NIfTI'), ('sub-01_from-T1w_to-MNI_xfm', '', 'NIfTI'),
    ('aparc+aseg', 'fs/sub-01/mri', 'MGZ'), ('brainmask', 'fs/sub-01/mri', 'MGZ'),
    ('synthstrip', 'fs/sub-01/mri', 'MGZ'), ('filled.auto', 'fs/sub-01/mri', 'MGZ'),
    ('T1', 'fs/fsaverage5/mri', 'MGZ'), ('lh.Yeo2011_7Networks', 'fs/fsaverage5/label', 'MGZ'),
    ('lh.w-g.pct', 'fs/sub-01/surf', 'MGH'), ('test', 'fs/sub-01/mri/transforms/synthmorph', 'NIfTI'),
    ('tpl-MNI152NLin2009cAsym_res-01_T1w', '', 'NIfTI'),
]
KEEP = [
    ('sub-01_T1w', '', 'NIfTI'), ('sub-01_desc-preproc_T1w', '', 'NIfTI'), ('T1_MPRAGE', '', 'NIfTI'),
    ('c1_mprage', '', 'NIfTI'), ('sub-01_space-MNI152_T1w', '', 'NIfTI'),
    ('pet_bet_study_T1w_brainstem', '', 'NIfTI'),
    ('orig', 'fs/sub-01/mri', 'MGZ'), ('rawavg', 'fs/sub-01/mri', 'MGZ'), ('nu', 'fs/sub-01/mri', 'MGZ'),
    ('T1', 'fs/sub-01/mri', 'MGZ'), ('001', 'fs/sub-01/mri/orig', 'MGZ'),
]


def main():
    bad = 0
    for rel, hdr, side, exp in CLASSIFY:
        shape, zooms = hdr or (None, None)
        r = c.classify(rel, 'NIfTI', shape, zooms, side)
        got = (r['category'], r['imtype'])
        if got != exp:
            bad += 1
            print(f'FAIL classify {rel}: got {got}, expected {exp}  ({"; ".join(r["reasons"])})')
    for stem, d, fmt in NO_FACE:
        if not c.no_face_reason(stem, d, fmt):
            bad += 1
            print(f'FAIL no-face  {d}/{stem} ({fmt}) not recognised as face-free')
    for stem, d, fmt in KEEP:
        why = c.no_face_reason(stem, d, fmt)
        if why:
            bad += 1
            print(f'FAIL keep     {d}/{stem} ({fmt}) wrongly face-free: {why}')
    total = len(CLASSIFY) + len(NO_FACE) + len(KEEP)
    print(f'{total - bad}/{total} passed')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
