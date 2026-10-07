set -x
pip install -q -r requirements.txt
mkdir -p probe/out4
BAGIAN=gerakan_tanah BUDGET_MENIT=40 python scripts/fetch_all.py > probe/out4/run.log 2>&1
tail -5 probe/out4/run.log
ls -la data/gerakan_tanah data/gerakan_tanah/cache_portal >> probe/out4/run.log 2>&1
git add -A data
# 1791357515
