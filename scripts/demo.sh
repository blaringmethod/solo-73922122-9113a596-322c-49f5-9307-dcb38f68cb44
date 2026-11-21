#!/usr/bin/env bash
# 端到端演示：发布 -> 幂等重试 -> 冲突拒绝 -> 取件 -> 两类证明 -> 离线核验 -> 篡改拒绝
#             -> 目录对账（本地缺单/错单排查）-> 离线核实缺单与错单清单。
# 用法：BASE=http://127.0.0.1:8001 bash scripts/demo.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8001}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cat > "$WORK/day1.csv" <<'CSV'
station,time,ph,do
ST01,2026-11-16T08:00,7.2,8.1
CSV
cat > "$WORK/day2.csv" <<'CSV'
station,time,ph,do
ST02,2026-11-16T08:00,7.0,8.4
CSV

echo "== 1. 首次发布 =="
curl -s -X POST "$BASE/releases" \
  -F station_id=ST01 -F sample_date=2026-11-16 -F order_no=ORD-001 \
  -F file=@"$WORK/day1.csv" | python3 -m json.tool

echo "== 2. 同单同输入重试（期望 reused=true）=="
curl -s -X POST "$BASE/releases" \
  -F station_id=ST01 -F sample_date=2026-11-16 -F order_no=ORD-001 \
  -F file=@"$WORK/day1.csv" | python3 -m json.tool

echo "== 3. 同单异输入（期望 HTTP 409）=="
curl -s -w "\nHTTP %{http_code}\n" -X POST "$BASE/releases" \
  -F station_id=ST01 -F sample_date=2026-11-16 -F order_no=ORD-001 \
  -F file=@"$WORK/day2.csv"

echo "== 4. 第二件发布 =="
curl -s -X POST "$BASE/releases" \
  -F station_id=ST02 -F sample_date=2026-11-16 -F order_no=ORD-002 \
  -F file=@"$WORK/day2.csv" | python3 -m json.tool

echo "== 5. 下载并比对原件 =="
curl -s "$BASE/releases/ST01/2026-11-16/ORD-001/download" -o "$WORK/back.csv"
cmp "$WORK/day1.csv" "$WORK/back.csv" && echo "字节一致"

echo "== 6. 纳入证明与离线核验 =="
curl -s "$BASE/releases/ST01/2026-11-16/ORD-001/inclusion-proof" \
  > "$WORK/inclusion.json"
python3 -m app.verify inclusion \
  --proof "$WORK/inclusion.json" --file "$WORK/day1.csv"

echo "== 7. 追加证明（1 号 -> 最新）与离线核验 =="
curl -s "$BASE/checkpoints/1" > "$WORK/cp1.json"
curl -s "$BASE/checkpoints/latest" > "$WORK/cp-latest.json"
curl -s "$BASE/checkpoints/1/append-proof" > "$WORK/append.json"
python3 -m app.verify append \
  --proof "$WORK/append.json" \
  --old-checkpoint "$WORK/cp1.json" \
  --new-checkpoint "$WORK/cp-latest.json"

echo "== 8. 篡改原件（期望离线拒绝，退出码 1）=="
cp "$WORK/day1.csv" "$WORK/tampered.csv"
printf 'X' >> "$WORK/tampered.csv"
python3 -m app.verify inclusion \
  --proof "$WORK/inclusion.json" --file "$WORK/tampered.csv" || true

echo "== 9. 目录对账：凭留存检查点核对本地目录（只交换摘要与目录字段）=="
# 模拟科研组本地目录：第 1 件缺单（null），第 2 件摘要被记错。
python3 - "$BASE" "$WORK" <<'PY'
import json
import sys
import urllib.request

sys.path.insert(0, ".")
from app.reconcile import run_reconciliation

base, work = sys.argv[1], sys.argv[2]


def get(path):
    with urllib.request.urlopen(base + path) as resp:
        return json.loads(resp.read().decode())


def post(path, payload):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


depth = get("/")["limits"]["tree_depth"]
checkpoint = get("/checkpoints/latest")  # 科研组留存检查点
releases = get("/releases")
local = [
    {k: r[k] for k in ("station_id", "sample_date", "order_no", "blob_sha256", "size")}
    for r in releases
]
local[0] = None  # 本地缺单
local[1] = {**local[1], "blob_sha256": "00" * 32}  # 本地错单

transcript = run_reconciliation(
    local,
    checkpoint,
    lambda seq, ranges: post(
        "/reconcile", {"checkpoint_seq": seq, "ranges": ranges}
    ),
    depth,
)
with open(f"{work}/local.json", "w") as fh:
    json.dump(local, fh, ensure_ascii=False)
with open(f"{work}/recon.json", "w") as fh:
    json.dump(transcript, fh, ensure_ascii=False)
with open(f"{work}/cp.json", "w") as fh:
    json.dump(checkpoint, fh, ensure_ascii=False)
print(f"对账完成：共交换 {len(transcript['results'])} 个区间结果（仅摘要与目录字段）")
PY

echo "== 10. 离线核实缺单与错单完整清单 =="
python3 -m app.verify reconcile \
  --local "$WORK/local.json" \
  --reconciliation "$WORK/recon.json" \
  --checkpoint "$WORK/cp.json"
