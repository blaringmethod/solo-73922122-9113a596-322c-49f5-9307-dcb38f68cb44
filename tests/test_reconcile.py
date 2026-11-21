"""目录对账测试：API 下钻协议、快照一致性、离线清单核验及其安全性质。

关键安全性质：只要漏掉一个有差异的区间（未下钻、谎报一致、目录字段被改），
即使已取回的记录全部正确，离线核验也必须判定清单不通过。
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app import merkle
from app.reconcile import local_leaf_hashes, run_reconciliation
from app.verify import verify_reconciliation

REPO_ROOT = Path(__file__).resolve().parents[1]

_LEAF_KEYS = ("station_id", "sample_date", "order_no", "blob_sha256", "size")


def _csv(seed: str) -> bytes:
    return f"station,time,ph,do\n{seed},2026-11-18T08:00,7.2,8.1\n".encode()


def _publish(client, station, day, order, data=None):
    return client.post(
        "/releases",
        data={"station_id": station, "sample_date": day, "order_no": order},
        files={"file": ("day.csv", io.BytesIO(data or _csv(order)), "text/csv")},
    )


def _leaf_fields(release: dict) -> dict:
    return {k: release[k] for k in _LEAF_KEYS}


def _query(client):
    def query(seq, ranges):
        resp = client.post(
            "/reconcile", json={"checkpoint_seq": seq, "ranges": ranges}
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    return query


@pytest.fixture(scope="module")
def rec_env(app_client):
    """发布 5 件并留存最新检查点；本地目录初始与服务器完全一致。"""
    client, _, settings = app_client
    created = []
    for i in range(5):
        resp = _publish(client, f"REC{i:02d}", "2026-11-18", f"ORD-REC-{i:02d}")
        assert resp.status_code == 201, resp.text
        created.append(resp.json()["release"])
    checkpoint = client.get("/checkpoints/latest").json()
    releases = client.get("/releases").json()
    assert checkpoint["seq"] == len(releases)
    return {
        "client": client,
        "settings": settings,
        "checkpoint": checkpoint,
        "releases": releases,
        "local": [_leaf_fields(r) for r in releases],
        "created": created,
    }


def _transcript(env, local, checkpoint=None):
    return run_reconciliation(
        local,
        checkpoint or env["checkpoint"],
        _query(env["client"]),
        env["settings"].tree_depth,
    )


# --------------------------------------------------------------------------- #
# Merkle 子树摘要原语
# --------------------------------------------------------------------------- #
def test_subtree_root_primitives():
    depth = 4
    leaves = [
        merkle.leaf_hash(f"S{i}", "2026-11-18", f"O{i}", "ab" * 32, 10 + i)
        for i in range(9)
    ]
    # 全树根与 root_from_leaves 一致；空根与全空子树根一致。
    assert merkle.subtree_root(leaves, 0, depth, depth) == merkle.root_from_leaves(
        leaves, depth
    )
    assert merkle.empty_root(depth) == merkle.empty_subtree_root(depth)
    assert merkle.empty_subtree_root(0) == merkle.EMPTY_LEAF
    # 完全落在已有叶子之后的区间走全空捷径。
    assert merkle.subtree_root(leaves, 12, 2, depth) == merkle.empty_subtree_root(2)
    # 部分覆盖区间与手工重建一致（叶子 8 真实，9..11 空）。
    manual = merkle.pair_hash(
        merkle.pair_hash(leaves[8], merkle.EMPTY_LEAF),
        merkle.empty_subtree_root(1),
    )
    assert merkle.subtree_root(leaves, 8, 2, depth) == manual
    # 未对齐/越界拒绝。
    with pytest.raises(ValueError):
        merkle.subtree_root(leaves, 1, 1, depth)
    with pytest.raises(ValueError):
        merkle.subtree_root(leaves, 1 << depth, 0, depth)


# --------------------------------------------------------------------------- #
# 对账 API：下钻协议
# --------------------------------------------------------------------------- #
def test_reconcile_root_match_via_api(rec_env):
    env = rec_env
    depth = env["settings"].tree_depth
    leaves = local_leaf_hashes(env["local"], depth)
    root_local = merkle.subtree_root(leaves, 0, depth, depth).hex()
    resp = env["client"].post(
        "/reconcile",
        json={
            "checkpoint_seq": env["checkpoint"]["seq"],
            # 大写 hex 同样合法（服务端归一化后比对）。
            "ranges": [{"start": 0, "height": depth, "local_root": root_local.upper()}],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["checkpoint_seq"] == env["checkpoint"]["seq"]
    assert body["checkpoint_root"] == env["checkpoint"]["root_hex"]
    assert body["tree_depth"] == depth
    assert body["results"] == [
        {
            "start": 0,
            "height": depth,
            "status": "match",
            "server_root": None,
            "leaf_record": None,
        }
    ]


def test_reconcile_available_when_degraded(rec_env, monkeypatch):
    """卷上缺件（degraded）时对账仍可用于排查：只读目录与摘要，不碰原件字节。"""
    from app import main as main_mod

    env = rec_env
    monkeypatch.setattr(main_mod, "_startup_problems", ["模拟卷上缺件"])
    assert env["client"].get("/health").json()["status"] == "degraded"
    depth = env["settings"].tree_depth
    leaves = local_leaf_hashes(env["local"], depth)
    resp = env["client"].post(
        "/reconcile",
        json={
            "checkpoint_seq": env["checkpoint"]["seq"],
            "ranges": [
                {
                    "start": 0,
                    "height": depth,
                    "local_root": merkle.subtree_root(leaves, 0, depth, depth).hex(),
                }
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["results"][0]["status"] == "match"


def test_reconcile_all_match_offline(rec_env):
    env = rec_env
    transcript = _transcript(env, env["local"])
    # 根摘要一致时客户端短路，无需任何往返。
    assert transcript["results"] == [
        {"start": 0, "height": env["settings"].tree_depth, "status": "match"}
    ]
    ok, msg, missing, mismatched = verify_reconciliation(
        env["local"], transcript, env["checkpoint"]
    )
    assert ok, msg
    assert missing == [] and mismatched == []


def test_reconcile_missing_order(rec_env):
    env = rec_env
    target = env["created"][2]
    idx = target["id"] - 1
    local = list(env["local"])
    local[idx] = None  # 本地缺单
    transcript = _transcript(env, local)
    # 协议性质：根区间 diverged，一路下钻，仅差异叶子返回目录字段。
    assert transcript["results"][0]["status"] == "diverged"
    leaf_entries = [r for r in transcript["results"] if r["status"] == "leaf"]
    assert len(leaf_entries) == 1
    assert leaf_entries[0]["start"] == idx and leaf_entries[0]["height"] == 0
    record = leaf_entries[0]["leaf_record"]
    assert record["order_no"] == target["order_no"]
    assert record["blob_sha256"] == target["blob_sha256"]
    assert record["station_id"] == target["station_id"]
    # 下钻轮数受树深度约束（每轮一批区间，最多 depth+1 轮：根到叶子每层一轮）。
    ok, msg, missing, mismatched = verify_reconciliation(
        local, transcript, env["checkpoint"]
    )
    assert ok, msg
    assert mismatched == []
    assert len(missing) == 1
    assert missing[0]["index"] == idx
    assert missing[0]["checkpoint_record"]["order_no"] == target["order_no"]


def test_reconcile_mismatched_order(rec_env):
    env = rec_env
    target = env["created"][1]
    idx = target["id"] - 1
    local = list(env["local"])
    local[idx] = {**local[idx], "blob_sha256": "00" * 32}  # 本地记错摘要
    transcript = _transcript(env, local)
    ok, msg, missing, mismatched = verify_reconciliation(
        local, transcript, env["checkpoint"]
    )
    assert ok, msg
    assert missing == []
    assert len(mismatched) == 1
    assert mismatched[0]["index"] == idx
    assert mismatched[0]["local_record"]["blob_sha256"] == "00" * 32
    assert (
        mismatched[0]["checkpoint_record"]["blob_sha256"] == target["blob_sha256"]
    )


def test_reconcile_mixed_missing_and_mismatched(rec_env):
    env = rec_env
    local = list(env["local"])
    drop1 = env["created"][0]["id"] - 1
    drop2 = env["created"][3]["id"] - 1
    wrong = env["created"][4]["id"] - 1
    local[drop1] = None
    local[drop2] = None
    local[wrong] = {**local[wrong], "size": local[wrong]["size"] + 1}
    transcript = _transcript(env, local)
    ok, msg, missing, mismatched = verify_reconciliation(
        local, transcript, env["checkpoint"]
    )
    assert ok, msg
    assert sorted(m["index"] for m in missing) == sorted([drop1, drop2])
    assert [m["index"] for m in mismatched] == [wrong]


def test_reconcile_empty_local_directory_full_backfill(rec_env):
    env = rec_env
    transcript = _transcript(env, [])
    ok, msg, missing, mismatched = verify_reconciliation(
        [], transcript, env["checkpoint"]
    )
    assert ok, msg
    assert mismatched == []
    # 缺单清单 = 检查点承诺的全部发布单，目录字段与服务器目录一致。
    assert len(missing) == env["checkpoint"]["seq"]
    for item in missing:
        server_release = env["releases"][item["index"]]
        assert item["checkpoint_record"]["order_no"] == server_release["order_no"]
        assert (
            item["checkpoint_record"]["blob_sha256"]
            == server_release["blob_sha256"]
        )


def test_reconcile_against_older_checkpoint(rec_env):
    """按旧检查点对账：本地多出的记录不属于该检查点承诺的前缀。"""
    env = rec_env
    client = env["client"]
    old_cp = client.get(f"/checkpoints/{env['checkpoint']['seq'] - 2}").json()
    transcript = _transcript(env, env["local"], checkpoint=old_cp)
    # 超出旧检查点条数的位置：服务器按空叶处理（leaf_record 为 None）。
    null_leaves = [
        r
        for r in transcript["results"]
        if r["status"] == "leaf" and r["leaf_record"] is None
    ]
    assert len(null_leaves) == 2
    ok, msg, missing, mismatched = verify_reconciliation(
        env["local"], transcript, old_cp
    )
    assert ok, msg
    assert missing == [] and mismatched == []


def test_reconcile_against_zero_checkpoint(rec_env):
    """0 号空树检查点：空本地目录直接一致；非空本地目录逐条判为检查点外。"""
    env = rec_env
    client = env["client"]
    zero_cp = client.get("/checkpoints/0").json()
    # 空本地目录与空树检查点一致：客户端短路，无往返。
    transcript = _transcript(env, [], checkpoint=zero_cp)
    assert transcript["results"] == [
        {"start": 0, "height": env["settings"].tree_depth, "status": "match"}
    ]
    ok, msg, missing, mismatched = verify_reconciliation([], transcript, zero_cp)
    assert ok and missing == [] and mismatched == []
    # 本地有记录而检查点为空：每条都下钻为 leaf 且 leaf_record=None。
    transcript2 = _transcript(env, env["local"], checkpoint=zero_cp)
    leaf_entries = [r for r in transcript2["results"] if r["status"] == "leaf"]
    assert len(leaf_entries) == len(env["local"])
    assert all(r["leaf_record"] is None for r in leaf_entries)
    ok2, msg2, missing2, mismatched2 = verify_reconciliation(
        env["local"], transcript2, zero_cp
    )
    assert ok2, msg2
    assert missing2 == [] and mismatched2 == []


# --------------------------------------------------------------------------- #
# 快照一致性：对账期间的新发布不改变本轮结果
# --------------------------------------------------------------------------- #
def test_reconcile_snapshot_isolated_from_new_publishes(rec_env):
    env = rec_env
    client = env["client"]
    depth = env["settings"].tree_depth
    checkpoint = env["checkpoint"]
    idx = env["created"][0]["id"] - 1
    local = list(env["local"])
    local[idx] = None

    baseline = _transcript(env, local)
    # 对账期间发生新发布（检查点继续前进）。
    resp = _publish(client, "REC-LATE", "2026-11-19", "ORD-REC-LATE")
    assert resp.status_code == 201
    assert client.get("/checkpoints/latest").json()["seq"] > checkpoint["seq"]

    after = _transcript(env, local)
    assert baseline["results"] == after["results"]  # 本轮结果不受新发布影响
    ok, msg, missing, mismatched = verify_reconciliation(local, after, checkpoint)
    assert ok and len(missing) == 1 and missing[0]["index"] == idx


def test_reconcile_rounds_unaffected_by_publish_between_rounds(rec_env):
    """手动分轮对账：两轮之间发生新发布，后续轮次结果不变。"""
    env = rec_env
    client = env["client"]
    depth = env["settings"].tree_depth
    checkpoint = env["checkpoint"]
    idx = env["created"][1]["id"] - 1
    local = list(env["local"])
    local[idx] = None
    leaves = local_leaf_hashes(local, depth)

    def post_ranges(ranges):
        resp = client.post(
            "/reconcile",
            json={"checkpoint_seq": checkpoint["seq"], "ranges": ranges},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["checkpoint_root"] == checkpoint["root_hex"]
        return body

    # 第一轮：根区间。
    first = post_ranges(
        [
            {
                "start": 0,
                "height": depth,
                "local_root": merkle.subtree_root(leaves, 0, depth, depth).hex(),
            }
        ]
    )
    assert first["results"][0]["status"] == "diverged"
    # 两轮之间发生新发布。
    assert _publish(client, "REC-MID", "2026-11-19", "ORD-REC-MID").status_code == 201
    # 继续下钻直至全部 match/leaf。
    results = list(first["results"])
    pending = [(0, depth - 1), (1 << (depth - 1), depth - 1)]
    while pending:
        ranges = [
            {
                "start": s,
                "height": h,
                "local_root": merkle.subtree_root(leaves, s, h, depth).hex(),
            }
            for s, h in pending
        ]
        pending = []
        for result in post_ranges(ranges)["results"]:
            results.append(result)
            if result["status"] == "diverged":
                half = 1 << (result["height"] - 1)
                pending.append((result["start"], result["height"] - 1))
                pending.append((result["start"] + half, result["height"] - 1))
    transcript = {
        "checkpoint_seq": checkpoint["seq"],
        "checkpoint_root": checkpoint["root_hex"],
        "tree_depth": depth,
        "results": results,
    }
    ok, msg, missing, mismatched = verify_reconciliation(local, transcript, checkpoint)
    assert ok, msg
    assert len(missing) == 1 and missing[0]["index"] == idx


# --------------------------------------------------------------------------- #
# 请求校验
# --------------------------------------------------------------------------- #
def test_reconcile_unknown_checkpoint_404(rec_env):
    resp = rec_env["client"].post(
        "/reconcile",
        json={
            "checkpoint_seq": 10**6,
            "ranges": [{"start": 0, "height": 0, "local_root": "00" * 32}],
        },
    )
    assert resp.status_code == 404


def test_reconcile_range_validation_422(rec_env):
    env = rec_env
    client = env["client"]
    depth = env["settings"].tree_depth
    seq = env["checkpoint"]["seq"]
    digest = "ab" * 32

    def post(ranges, seq=seq):
        return client.post(
            "/reconcile", json={"checkpoint_seq": seq, "ranges": ranges}
        ).status_code

    assert post([{"start": 1, "height": 1, "local_root": digest}]) == 422  # 未对齐
    assert post([{"start": 0, "height": depth + 1, "local_root": digest}]) == 422
    assert (
        post([{"start": 1 << depth, "height": 0, "local_root": digest}]) == 422
    )  # 超出容量
    assert post([{"start": 0, "height": 0, "local_root": "zz" * 32}]) == 422  # 非法 hex
    assert post([{"start": 0, "height": 0, "local_root": "ab"}]) == 422  # 长度不足
    assert post([]) == 422  # 空区间列表
    assert post([{"start": -1, "height": 0, "local_root": digest}]) == 422
    assert post([{"start": 0, "height": 0, "local_root": digest}], seq=-1) == 422


# --------------------------------------------------------------------------- #
# 离线核验的安全性质：漏掉任何一个差异区间都不能通过
# --------------------------------------------------------------------------- #
def test_offline_rejects_dropped_subtree_results(rec_env):
    """diverged 区间未完整下钻（对账结果缺区间）-> 清单不通过。"""
    env = rec_env
    idx = env["created"][2]["id"] - 1
    local = list(env["local"])
    local[idx] = None
    transcript = _transcript(env, local)
    victim = min(
        (r for r in transcript["results"] if r["status"] == "diverged"),
        key=lambda r: r["height"],
    )
    cut_start, cut_height = victim["start"], victim["height"] - 1
    cut_end = cut_start + (1 << cut_height)
    pruned = [
        r
        for r in transcript["results"]
        if not (cut_start <= r["start"] < cut_end and r["height"] <= cut_height)
    ]
    assert len(pruned) < len(transcript["results"])
    broken = {**transcript, "results": pruned}
    ok, msg, missing, mismatched = verify_reconciliation(
        local, broken, env["checkpoint"]
    )
    assert not ok
    assert "不完整" in msg and "漏掉" in msg


def test_offline_rejects_differing_leaf_reported_as_match(rec_env):
    """有差异的单条被谎报为一致 -> 即使其余结果全对也不通过。"""
    env = rec_env
    idx = env["created"][3]["id"] - 1
    local = list(env["local"])
    local[idx] = None
    transcript = _transcript(env, local)
    forged = [
        (
            {"start": r["start"], "height": 0, "status": "match"}
            if r["status"] == "leaf" and r["start"] == idx
            else r
        )
        for r in transcript["results"]
    ]
    ok, msg, _, _ = verify_reconciliation(
        local, {**transcript, "results": forged}, env["checkpoint"]
    )
    assert not ok


def test_offline_rejects_internal_diverged_range_hidden(rec_env):
    """整个有差异的内部区间被谎报为一致（其下结果全部漏报）-> 不通过。"""
    env = rec_env
    idx = env["created"][4]["id"] - 1
    local = list(env["local"])
    local[idx] = None
    transcript = _transcript(env, local)
    victim = min(
        (r for r in transcript["results"] if r["status"] == "diverged"),
        key=lambda r: r["height"],
    )
    v_end = victim["start"] + (1 << victim["height"])
    forged = []
    for r in transcript["results"]:
        if (r["start"], r["height"]) == (victim["start"], victim["height"]):
            forged.append(
                {"start": victim["start"], "height": victim["height"], "status": "match"}
            )
        elif victim["start"] <= r["start"] < v_end and r["height"] < victim["height"]:
            continue  # 漏掉该区间下的全部后续结果
        else:
            forged.append(r)
    ok, msg, missing, mismatched = verify_reconciliation(
        local, {**transcript, "results": forged}, env["checkpoint"]
    )
    assert not ok
    assert missing == [] and mismatched == []  # 不通过时不得输出清单


def test_offline_rejects_tampered_leaf_record(rec_env):
    """对账单中的目录字段被篡改 -> 不通过。"""
    env = rec_env
    idx = env["created"][0]["id"] - 1
    local = list(env["local"])
    local[idx] = None
    transcript = _transcript(env, local)
    forged = [
        (
            {**r, "leaf_record": {**r["leaf_record"], "order_no": "ORD-FORGED"}}
            if r["status"] == "leaf" and r["leaf_record"]
            else r
        )
        for r in transcript["results"]
    ]
    ok, msg, _, _ = verify_reconciliation(
        local, {**transcript, "results": forged}, env["checkpoint"]
    )
    assert not ok


def test_offline_rejects_tampered_server_root(rec_env):
    """diverged 区间的服务器摘要被篡改 -> 不通过。"""
    env = rec_env
    idx = env["created"][1]["id"] - 1
    local = list(env["local"])
    local[idx] = None
    transcript = _transcript(env, local)
    victim = min(
        (r for r in transcript["results"] if r["status"] == "diverged"),
        key=lambda r: r["height"],
    )
    forged = [
        (
            {**r, "server_root": "00" * 32}
            if (r["start"], r["height"]) == (victim["start"], victim["height"])
            else r
        )
        for r in transcript["results"]
    ]
    ok, msg, _, _ = verify_reconciliation(
        local, {**transcript, "results": forged}, env["checkpoint"]
    )
    assert not ok


def test_offline_rejects_transcript_bound_to_other_checkpoint(rec_env):
    """对账单与留存检查点不对应 -> 不通过。"""
    env = rec_env
    local = list(env["local"])
    local[0] = None
    transcript = _transcript(env, local)
    forged_root = {**env["checkpoint"], "root_hex": "00" * 32}
    ok, msg, _, _ = verify_reconciliation(local, transcript, forged_root)
    assert not ok and "不一致" in msg
    forged_seq = {**env["checkpoint"], "seq": env["checkpoint"]["seq"] - 1}
    ok2, _, _, _ = verify_reconciliation(local, transcript, forged_seq)
    assert not ok2


def test_offline_rejects_duplicate_and_misaligned_ranges(rec_env):
    env = rec_env
    local = list(env["local"])
    local[1] = None
    transcript = _transcript(env, local)
    duplicated = {**transcript, "results": transcript["results"] * 2}
    ok, msg, _, _ = verify_reconciliation(local, duplicated, env["checkpoint"])
    assert not ok and "重复" in msg
    misaligned = {
        **transcript,
        "results": transcript["results"]
        + [{"start": 1, "height": 1, "status": "match"}],
    }
    ok2, msg2, _, _ = verify_reconciliation(local, misaligned, env["checkpoint"])
    assert not ok2


def test_run_reconciliation_rejects_inconsistent_response(rec_env):
    """客户端驱动：响应与留存检查点不对应时中止对账。"""
    env = rec_env
    local = list(env["local"])
    local[0] = None

    def bad_query(seq, ranges):
        return {
            "checkpoint_seq": seq,
            "checkpoint_root": "00" * 32,
            "tree_depth": env["settings"].tree_depth,
            "results": [],
        }

    with pytest.raises(ValueError):
        run_reconciliation(
            local, env["checkpoint"], bad_query, env["settings"].tree_depth
        )


# --------------------------------------------------------------------------- #
# 离线 CLI
# --------------------------------------------------------------------------- #
def test_reconcile_cli_offline(rec_env, tmp_path):
    env = rec_env
    idx = env["created"][4]["id"] - 1
    local = list(env["local"])
    local[idx] = None
    transcript = _transcript(env, local)
    local_file = tmp_path / "local.json"
    recon_file = tmp_path / "recon.json"
    cp_file = tmp_path / "cp.json"
    local_file.write_text(json.dumps(local, ensure_ascii=False))
    recon_file.write_text(json.dumps(transcript, ensure_ascii=False))
    cp_file.write_text(json.dumps(env["checkpoint"], ensure_ascii=False))

    def run_cli(recon_path):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "app.verify",
                "reconcile",
                "--local",
                str(local_file),
                "--reconciliation",
                str(recon_path),
                "--checkpoint",
                str(cp_file),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )

    proc = run_cli(recon_file)
    assert proc.returncode == 0, proc.stderr
    assert "缺单 1 件" in proc.stdout and "错单 0 件" in proc.stdout
    assert "ORD-REC-04" in proc.stdout  # 缺单清单给出检查点承诺的目录字段

    # 漏掉叶子级结果的对账单 -> 退出码 1。
    broken = json.loads(recon_file.read_text())
    broken["results"] = [
        r for r in broken["results"] if r["status"] != "leaf"
    ]
    broken_file = tmp_path / "broken.json"
    broken_file.write_text(json.dumps(broken, ensure_ascii=False))
    proc2 = run_cli(broken_file)
    assert proc2.returncode == 1
    assert "不予通过" in proc2.stdout
