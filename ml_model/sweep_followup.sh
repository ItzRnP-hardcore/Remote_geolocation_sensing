#!/bin/bash
# Follow-up to sweep_matrix.sh. Takes the best capacity found there (set BEST) and
# crosses it with the three regularisers that attack cross-session generalisation
# specifically, rather than capacity: rotation augmentation, per-run stationary bias
# removal, and a denser window stride (more diverse crops of the same journeys).
cd "C:/Users/rudra/Documents/Remote_geolocation_sensing"
BEST="${BEST:-16,32,64,128}"
BLOCKS="${BLOCKS:-2}"
r () {  # tag data dropout wd augment
  python -u -m ml_model.train_iovnbd --data "$2" --epochs 40 --sweep --only data \
    --seed 0 --tag "_$1" --widths "$BEST" --blocks "$BLOCKS" --dropout "$3" \
    --weight-decay "$4" --augment "$5" > "ml_model/fu_$1.log" 2>&1 \
    && echo "$1 ok" || echo "$1 FAILED"
}
D=ml_model/dataset_wide.pt
DB=ml_model/dataset_wide_db.pt
r fu_plain    $D  0.0  0.0   ""
r fu_rot      $D  0.0  0.0   "rot"
r fu_rotgn    $D  0.0  0.0   "rot,gain,noise"
r fu_debias   $DB 0.0  0.0   ""
r fu_deb_rot  $DB 0.0  0.0   "rot"
r fu_all      $DB 0.3  0.01  "rot,gain,noise"
echo "FOLLOWUP COMPLETE"
