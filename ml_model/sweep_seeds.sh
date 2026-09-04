#!/bin/bash
# Multi-seed confirmation of the three configurations that beat the baseline on a
# single seed. Single-seed differences of a few percent have already misled this
# project once (an along-road result measured on five outages did not survive a
# denser sample), and these gaps are of that size.
cd "C:/Users/rudra/Documents/Remote_geolocation_sensing"
F="64,128,256,512"
C=ml_model/dataset_iovnbd.pt
W=ml_model/dataset_wide.pt
for s in 1 2 3; do
  python -u -m ml_model.train_iovnbd --data $C --epochs 40 --sweep --only data \
    --seed $s --tag "_sd_base_s$s"  --widths "$F" > "ml_model/sd_base_s$s.log" 2>&1
  python -u -m ml_model.train_iovnbd --data $C --epochs 40 --sweep --only data \
    --seed $s --tag "_sd_d3rot_s$s" --widths "$F" --dropout 0.3 --augment rot \
    > "ml_model/sd_d3rot_s$s.log" 2>&1
  python -u -m ml_model.train_iovnbd --data $C --epochs 40 --sweep --only data \
    --seed $s --tag "_sd_rotgn_s$s" --widths "$F" --augment rot,gain,noise \
    > "ml_model/sd_rotgn_s$s.log" 2>&1
  python -u -m ml_model.train_iovnbd --data $W --epochs 40 --sweep --only data \
    --seed $s --tag "_sd_wqrot_s$s" --widths "$F" --weight-by-quality --augment rot \
    > "ml_model/sd_wqrot_s$s.log" 2>&1
  echo "seed $s done"
done
echo "SEED SWEEP COMPLETE"
