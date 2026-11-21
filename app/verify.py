"""离线验证：仅凭留存的检查点、证明与待验原件即可核验。

科研组可在无网络、无服务端的环境下使用：

    python -m app.verify inclusion --proof proof.json --file 原件.csv
    python -m app.verify append --proof append.json \\
        --old-checkpoint old.json --new-checkpoint new.json
    python -m app.verify reconcile --local 本地目录.json \\
        --reconciliation 对账单.json --checkpoint 留存检查点.json

也可直接导入 :func:`verify_release_bytes` 等纯函数嵌入自己的流程。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

from . import merkle


def _load_json(path: str | Path) -> Any:
    with open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def _parse_hex32(value: Any) -> bytes | None:
    """严格解析 32 字节十六进制摘要；非法输入返回 None。"""
    if not isinstance(value, str) or len(value) != 64:
        return None
    try:
        digest = bytes.fromhex(value)
    except ValueError:
        return None
    return digest if len(digest) == 32 else None


def verify_release_bytes(proof: dict, raw_bytes: bytes) -> tuple[bool, str]:
    """核验单件原件的站日归属与检查点纳入情况。

    ``proof`` 为 GET /releases/.../inclusion-proof 的返回内容。
    返回 (是否通过, 说明)。
    """
    try:
        record = proof["leaf_record"]
        index = int(proof["index"])
        depth = int(proof["tree_depth"])
        root = bytes.fromhex(proof["checkpoint_root"])
    except (KeyError, TypeError, ValueError):
        return False, "证明结构不完整"

    actual_digest = hashlib.sha256(raw_bytes).hexdigest()
    if actual_digest != record["blob_sha256"]:
        return False, "原件 SHA-256 与证明记录不一致，原件可能被篡改或替换"
    if len(raw_bytes) != record["size"]:
        return False, "原件字节数与证明记录不一致"

    leaf = merkle.leaf_hash(
        record["station_id"],
        record["sample_date"],
        record["order_no"],
        record["blob_sha256"],
        record["size"],
    )
    if leaf.hex() != proof["leaf_hash"]:
        return False, "叶子哈希与规范化记录不一致（证明本身被篡改）"

    if not merkle.verify_inclusion(index, leaf, proof["path"], root, depth):
        return False, "纳入路径无法重建检查点根，证明或检查点不匹配"

    if proof["checkpoint_seq"] <= index:
        return False, "检查点条数与叶子下标矛盾"
    return True, (
        f"通过：{record['station_id']} / {record['sample_date']} / "
        f"{record['order_no']} 的原件已纳入 {proof['checkpoint_seq']} 号检查点"
    )


def verify_append_proof(
    old_checkpoint: dict, new_checkpoint: dict, proof: dict
) -> tuple[bool, str]:
    """核验旧检查点到新检查点为仅追加前缀关系（历史未被删除或替换）。"""
    try:
        depth = int(proof["tree_depth"])
        ok = merkle.verify_append(
            old_root=bytes.fromhex(old_checkpoint["root_hex"]),
            new_root=bytes.fromhex(new_checkpoint["root_hex"]),
            old_count=int(old_checkpoint["seq"]),
            new_count=int(new_checkpoint["seq"]),
            entries=proof["entries"],
            depth=depth,
        )
    except (KeyError, TypeError, ValueError):
        return False, "追加证明或检查点结构不完整"
    if not ok:
        return False, "追加证明不成立：新检查点不包含完整旧前缀，历史可能被改动"
    return True, (
        f"通过：{old_checkpoint['seq']} 号检查点是 {new_checkpoint['seq']} 号"
        "检查点的完整仅追加前缀"
    )


# --------------------------------------------------------------------------- #
# 目录对账结果的离线核验
# --------------------------------------------------------------------------- #
class _TranscriptIncomplete(Exception):
    """对账结果缺少某区间的结果（差异区间被漏掉）。"""

    def __init__(self, start: int, height: int):
        super().__init__(f"start={start} height={height}")
        self.start = start
        self.height = height


class _TranscriptMismatch(Exception):
    """对账结果自相矛盾（子区间重建与服务器摘要不符）。"""

    def __init__(self, start: int, height: int):
        super().__init__(f"start={start} height={height}")
        self.start = start
        self.height = height


class _LocalRecordError(Exception):
    """本地目录记录字段不完整，无法参与核验。"""

    def __init__(self, index: int):
        super().__init__(f"index={index}")
        self.index = index


def _record_leaf(record: dict, index: int) -> bytes:
    try:
        return merkle.leaf_hash(
            record["station_id"],
            record["sample_date"],
            record["order_no"],
            record["blob_sha256"],
            int(record["size"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _LocalRecordError(index) from exc


def verify_reconciliation(
    local_records: Sequence[dict | None],
    reconciliation: dict,
    checkpoint: dict,
) -> tuple[bool, str, list[dict], list[dict]]:
    """离线核实对账结果，给出缺单与错单的完整清单。

    参数
    ----
    local_records : 本地目录（按下标排列，下标 = 发布序号 - 1），
        元素为目录字段 ``{station_id, sample_date, order_no, blob_sha256,
        size}`` 或 ``None``（本地缺单）。
    reconciliation : 对账单 ``{"checkpoint_seq", "checkpoint_root",
        "tree_depth", "results": [...]}``，其中 results 为各轮
        ``POST /reconcile`` 响应结果的合并（可由
        :func:`app.reconcile.run_reconciliation` 生成）。
    checkpoint : 留存检查点 ``{"seq", "root_hex"}``。

    返回 ``(ok, message, missing, mismatched)``：
    ``missing`` 为缺单清单（检查点承诺而本地没有的目录字段），
    ``mismatched`` 为错单清单（本地记录与检查点承诺不符的对照）。

    完整性论证
    ----------
    通过的条件是：用本地叶子（缺单按空叶）与对账单提供的目录字段重建出的
    树根，与留存检查点根完全一致；且每个 diverged 区间的两个子区间都必须
    有后续结果。任一差异区间被漏掉——未下钻、被谎报为一致、或目录字段被
    篡改——都会使重建根与留存根不同（或结构校验失败），清单即判不通过，
    即使已取回的记录全部正确。
    """
    try:
        depth = int(reconciliation["tree_depth"])
        raw_results = reconciliation["results"]
        recon_seq = int(reconciliation["checkpoint_seq"])
        recon_root = reconciliation["checkpoint_root"]
        seq = int(checkpoint["seq"])
        root_hex = checkpoint["root_hex"]
    except (KeyError, TypeError, ValueError):
        return False, "对账单或留存检查点结构不完整", [], []
    retained_root = _parse_hex32(root_hex)
    if retained_root is None:
        return False, "留存检查点根摘要非法", [], []
    if not (0 <= depth <= 64):
        return False, f"树深度非法: {depth}", [], []
    capacity = 1 << depth
    if not (0 <= seq <= capacity):
        return False, "留存检查点条数超出树容量", [], []
    if recon_seq != seq or recon_root != root_hex:
        return False, "对账单所针对的检查点与留存检查点不一致", [], []
    if not isinstance(local_records, list) or len(local_records) > capacity:
        return False, "本地目录结构非法或长度超出树容量", [], []
    if not isinstance(raw_results, list):
        return False, "对账单 results 结构非法", [], []

    def local_leaf(index: int) -> bytes:
        if index < len(local_records):
            record = local_records[index]
            if record is not None:
                return _record_leaf(record, index)
        return merkle.EMPTY_LEAF

    @lru_cache(maxsize=None)
    def local_subtree(start: int, height: int) -> bytes:
        size = 1 << height
        if start >= len(local_records):
            return merkle.empty_subtree_root(height)
        if height == 0:
            return local_leaf(start)
        end = min(start + size, len(local_records))
        if all(local_records[i] is None for i in range(start, end)):
            return merkle.empty_subtree_root(height)
        half = size // 2
        return merkle.pair_hash(
            local_subtree(start, height - 1),
            local_subtree(start + half, height - 1),
        )

    # 汇总并校验全部区间结果，按 (start, height) 索引。
    entries: dict[tuple[int, int], dict] = {}
    for raw in raw_results:
        try:
            start = int(raw["start"])
            height = int(raw["height"])
            status = raw["status"]
        except (KeyError, TypeError, ValueError):
            return False, "对账结果条目结构不完整", [], []
        if not (0 <= height <= depth and 0 <= start):
            return False, f"对账区间越界: start={start} height={height}", [], []
        size = 1 << height
        if start % size != 0 or start + size > capacity:
            return (
                False,
                f"对账区间未按 2**height 对齐或超出树容量: "
                f"start={start} height={height}",
                [],
                [],
            )
        if (start, height) in entries:
            return False, f"对账结果存在重复区间: start={start} height={height}", [], []
        if status == "match":
            entries[(start, height)] = {"status": status, "digest": None, "record": None}
        elif status == "diverged":
            if height == 0:
                return False, "高度为 0 的区间不能标记为 diverged", [], []
            digest = _parse_hex32(raw.get("server_root"))
            if digest is None:
                return (
                    False,
                    f"diverged 区间缺少合法的服务器摘要: start={start} height={height}",
                    [],
                    [],
                )
            entries[(start, height)] = {"status": status, "digest": digest, "record": None}
        elif status == "leaf":
            if height != 0:
                return False, "leaf 状态仅适用于单条区间（height=0）", [], []
            record = raw.get("leaf_record")
            if record is None:
                leaf = merkle.EMPTY_LEAF
            else:
                if not isinstance(record, dict) or record.get("v") != merkle.LEAF_VERSION:
                    return (
                        False,
                        f"leaf 区间目录字段缺失或版本号非法: start={start}",
                        [],
                        [],
                    )
                try:
                    leaf = _record_leaf(record, start)
                except _LocalRecordError:
                    return False, f"leaf 区间目录字段不完整: start={start}", [], []
            # 若附带了服务器叶子摘要，必须与目录字段重算结果一致。
            attached = raw.get("server_root")
            if attached is not None and _parse_hex32(attached) != leaf:
                return False, f"leaf 区间目录字段与附带摘要不符: start={start}", [], []
            entries[(start, height)] = {"status": status, "digest": leaf, "record": record}
        else:
            return False, f"未知区间状态: {status!r}", [], []

    server_records: dict[int, dict | None] = {}

    def rebuild(start: int, height: int) -> bytes:
        entry = entries.get((start, height))
        if entry is None:
            raise _TranscriptIncomplete(start, height)
        if entry["status"] == "match":
            return local_subtree(start, height)
        if entry["status"] == "leaf":
            server_records[start] = entry["record"]
            return entry["digest"]
        half = 1 << (height - 1)
        left = rebuild(start, height - 1)
        right = rebuild(start + half, height - 1)
        if merkle.pair_hash(left, right) != entry["digest"]:
            raise _TranscriptMismatch(start, height)
        return entry["digest"]

    try:
        rebuilt = rebuild(0, depth)
    except _TranscriptIncomplete as exc:
        return (
            False,
            f"对账结果不完整：区间 start={exc.start} height={exc.height} "
            "缺少结果，存在漏掉的差异区间，清单不予通过",
            [],
            [],
        )
    except _TranscriptMismatch as exc:
        return (
            False,
            f"对账结果自相矛盾：区间 start={exc.start} height={exc.height} "
            "的子区间重建与服务器摘要不符",
            [],
            [],
        )
    except _LocalRecordError as exc:
        return False, f"本地目录下标 {exc.index} 的记录字段不完整，无法参与核验", [], []

    if rebuilt != retained_root:
        return (
            False,
            "对账结果重建的根与留存检查点不符：存在未覆盖的差异区间"
            "或目录字段有误，清单不予通过",
            [],
            [],
        )

    # 重建根与留存根一致 => 所有差异位置都已被对账单覆盖（Merkle 碰撞抗性）。
    missing: list[dict] = []
    mismatched: list[dict] = []
    for index in sorted(server_records):
        record = server_records[index]
        if record is None:
            # 检查点内该位置为空叶（下标超出检查点条数）：
            # 本地多出的记录不属于该检查点承诺的前缀，不计入缺单/错单。
            continue
        local = local_records[index] if index < len(local_records) else None
        if local is None:
            missing.append({"index": index, "checkpoint_record": record})
            continue
        try:
            same = local_leaf(index) == _record_leaf(record, index)
        except _LocalRecordError:
            same = False
        if not same:
            mismatched.append(
                {
                    "index": index,
                    "local_record": local,
                    "checkpoint_record": record,
                }
            )
    return (
        True,
        f"通过：对账结果与 {seq} 号留存检查点一致，清单完整"
        f"（缺单 {len(missing)} 件，错单 {len(mismatched)} 件）",
        missing,
        mismatched,
    )


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="河流水质发布数据离线验证工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_inc = sub.add_parser("inclusion", help="核验单件原件纳入证明")
    p_inc.add_argument("--proof", required=True)
    p_inc.add_argument("--file", required=True, help="待验原件字节路径")

    p_app = sub.add_parser("append", help="核验检查点追加证明")
    p_app.add_argument("--proof", required=True)
    p_app.add_argument("--old-checkpoint", required=True)
    p_app.add_argument("--new-checkpoint", required=True)

    p_rec = sub.add_parser(
        "reconcile", help="离线核实目录对账结果，输出缺单与错单完整清单"
    )
    p_rec.add_argument(
        "--local", required=True, help="本地目录 JSON（记录数组，缺单位置为 null）"
    )
    p_rec.add_argument(
        "--reconciliation", required=True, help="对账单 JSON（各轮 results 合并）"
    )
    p_rec.add_argument("--checkpoint", required=True, help="留存检查点 JSON")

    args = parser.parse_args(argv)
    if args.cmd == "inclusion":
        proof = _load_json(args.proof)
        raw = Path(args.file).read_bytes()
        ok, message = verify_release_bytes(proof, raw)
        print(message)
        return 0 if ok else 1
    if args.cmd == "append":
        ok, message = verify_append_proof(
            _load_json(args.old_checkpoint),
            _load_json(args.new_checkpoint),
            _load_json(args.proof),
        )
        print(message)
        return 0 if ok else 1
    # reconcile
    local = _load_json(args.local)
    if isinstance(local, dict):
        local = local.get("records")
    ok, message, missing, mismatched = verify_reconciliation(
        local, _load_json(args.reconciliation), _load_json(args.checkpoint)
    )
    print(message)
    print(
        json.dumps(
            {"missing": missing, "mismatched": mismatched},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_main())
