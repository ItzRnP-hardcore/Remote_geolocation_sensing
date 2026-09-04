#!/bin/bash
# The matrix showed two things: capacity reduction helps, and the 16 loosely-mounted
# runs hurt. The untested combination is small model on CLEAN data, which is what this
# sweeps - together with quality weighting, which is the middle road between using
# those runs and discarding them.
#
# Scored on the fixed S2_r1/S3c journey split for continuity with everything before.
cd "C:/Users/rudra/Documents/Remote_geolocation_sensing"
r () {  # tag data widths blocks dropout wd extra
  python -u -m ml_model.train_iovnbd --data "$2" --epochs 40 --sweep --only data \
    --seed 0 --tag "_$1" --widths "$3" --blocks "$4" --dropout "$5" --weight-decay "$6" \
    $7 > "ml_model/cl_$1.log" 2>&1 && echo "$1 ok" || echo "$1 FAILED"
}
C=ml_model/dataset_iovnbd.pt      # 10 clean runs
W=ml_model/dataset_wide.pt        # 26 runs, carries per-run quality weights

# capacity ladder on the clean set
r cl_w32     $C "32,64,128,256" 2 0.0 0.0    ""
r cl_w16     $C "16,32,64,128"  2 0.0 0.0    ""
r cl_w16b1   $C "16,32,64,128"  1 0.0 0.0    ""
r cl_w8b1    $C "8,16,32,64"    1 0.0 0.0    ""
r cl_w8b1d3  $C "8,16,32,64"    1 0.3 0.0    ""
# regularise the best-guess clean capacity
r cl_w16_d3  $C "16,32,64,128"  2 0.3 0.0    ""
r cl_w16_dw  $C "16,32,64,128"  2 0.3 0.01   ""
r cl_w16_rot $C "16,32,64,128"  2 0.0 0.0    "--augment rot"
# quality weighting: use all 26 runs but discount the badly mounted ones
r wq_w16     $W "16,32,64,128"  2 0.0 0.0    "--weight-by-quality"
r wq_w16_rot $W "16,32,64,128"  2 0.0 0.0    "--weight-by-quality --augment rot"
echo "CLEAN SWEEP COMPLETE"
