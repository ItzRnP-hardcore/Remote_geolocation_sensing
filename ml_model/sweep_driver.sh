#!/bin/bash
# Honest generalisation: hold out a whole driver, not a journey. Journey splits share
# a driver, vehicle, phone, mounting and area - the S3c test box overlaps the S3a
# training box by 46% - so they cannot answer the question the per-session bias poses.
cd "C:/Users/rudra/Documents/Remote_geolocation_sensing"
F="64,128,256,512"
for D in A B D E; do
  DS=ml_model/dataset_drv$D.pt
  python -u -m ml_model.build_dataset_iovnbd --npz dataset/iovnbd_train_wide.npz \
    --out $DS --frame vehicle --test-driver $D > /dev/null 2>&1
  python -u -m ml_model.train_iovnbd --data $DS --epochs 40 --sweep --only data \
    --seed 0 --tag "_drv${D}_base"  --widths "$F" > "ml_model/drv${D}_base.log" 2>&1
  python -u -m ml_model.train_iovnbd --data $DS --epochs 40 --sweep --only data \
    --seed 0 --tag "_drv${D}_d3rot" --widths "$F" --dropout 0.3 --augment rot \
    > "ml_model/drv${D}_d3rot.log" 2>&1
  echo "driver $D done"
done
echo "DRIVER SWEEP COMPLETE"
