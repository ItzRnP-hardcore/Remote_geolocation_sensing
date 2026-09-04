#!/bin/bash
# Corrected after the matrix. Two results reshaped this sweep:
#   - the 16 loosely-mounted runs HURT (5.31 against 4.92), so the clean 10 are the base
#   - cutting capacity helps on the wide set (5.31 -> 5.12) but HURTS on the clean one
#     (4.88 -> 5.42), so full capacity stays the base here
# Test error is therefore not classic variance - it is distribution shift between runs,
# and a smaller model cannot know a new session's speed scale either. So this sweeps
# the things that attack the shift itself, all at full capacity on clean data.
cd "C:/Users/rudra/Documents/Remote_geolocation_sensing"
F="64,128,256,512"
r () {  # tag data dropout wd extra
  python -u -m ml_model.train_iovnbd --data "$2" --epochs 40 --sweep --only data \
    --seed 0 --tag "_$1" --widths "$F" --blocks 2 --dropout "$3" --weight-decay "$4" \
    $5 > "ml_model/cl_$1.log" 2>&1 && echo "$1 ok" || echo "$1 FAILED"
}
C=ml_model/dataset_iovnbd.pt      # 10 clean runs, the best base found
CD=ml_model/dataset_clean_db.pt   # same, with per-run stationary bias removed
W=ml_model/dataset_wide.pt        # 26 runs, carries per-run quality weights

r cl_rot     $C  0.0 0.0   "--augment rot"
r cl_rotgn   $C  0.0 0.0   "--augment rot,gain,noise"
r cl_d3      $C  0.3 0.0   ""
r cl_d5      $C  0.5 0.0   ""
r cl_wd      $C  0.0 0.01  ""
r cl_d3rot   $C  0.3 0.0   "--augment rot"
r cl_debias  $CD 0.0 0.0   ""
r cl_deb_rot $CD 0.0 0.0   "--augment rot"
r wq_full    $W  0.0 0.0   "--weight-by-quality"
r wq_rot     $W  0.0 0.0   "--weight-by-quality --augment rot"
echo "CLEAN SWEEP COMPLETE"
