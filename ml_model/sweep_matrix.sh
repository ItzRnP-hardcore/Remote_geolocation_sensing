#!/bin/bash
# One matrix answering both open questions: does more data help, and does cutting
# capacity help? Overfitting is 12x, so capacity is expected to dominate.
cd "C:/Users/rudra/Documents/Remote_geolocation_sensing"
r () {  # tag data widths blocks dropout wd
  python -m ml_model.train_iovnbd --data "$2" --epochs 40 --sweep --only data \
    --seed 0 --tag "_$1" --widths "$3" --blocks "$4" --dropout "$5" --weight-decay "$6" \
    > "ml_model/mx_$1.log" 2>&1 && echo "$1 ok" || echo "$1 FAILED"
}
D10=ml_model/dataset_iovnbd.pt
D26=ml_model/dataset_wide.pt
# capacity ladder on the wide (26-run) set
r wide_full   $D26 "64,128,256,512" 2 0.0  0.0
r wide_w32    $D26 "32,64,128,256"  2 0.0  0.0
r wide_w16    $D26 "16,32,64,128"   2 0.0  0.0
r wide_w16b1  $D26 "16,32,64,128"   1 0.0  0.0
r wide_w8b1   $D26 "8,16,32,64"     1 0.0  0.0
# regularisation on the most promising capacity
r wide_w16_d3 $D26 "16,32,64,128"   2 0.3  0.0
r wide_w16_d5 $D26 "16,32,64,128"   2 0.5  0.0
r wide_w16_wd $D26 "16,32,64,128"   2 0.0  0.01
r wide_w16_dw $D26 "16,32,64,128"   2 0.3  0.01
# same small model on the 10-run set: isolates the effect of DATA at fixed capacity
r narrow_w16  $D10 "16,32,64,128"   2 0.0  0.0
r narrow_full $D10 "64,128,256,512" 2 0.0  0.0
echo "MATRIX COMPLETE"
