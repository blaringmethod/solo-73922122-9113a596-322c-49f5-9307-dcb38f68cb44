"""目录对账客户端协议：从根摘要下钻差异区间，汇总对账单。

科研组留存某历史检查点 ``(seq, root)`` 后，仅凭本地目录即可与服务端
核对前 ``seq`` 条发布前缀：

1. 本地目录按下标展开为固定深度树的叶子序列，**本地缺单的位置以
   ``EMPTY_LEAF`` 占位**，``seq`` 之后的槽位同样为空叶；
2. 从根区间 ``(start=0, height=tree_depth)`` 开始，把本地算出的区间摘要
   发给 ``POST /reconcile``；
3. 服务端对每个区间回答：
     - ``match``    摘要一致，该区间无需继续核对；
     - ``diverged`` 内部区间不一致，附服务器侧摘要，客户端分裂为左右两个
                    子区间继续核对；
     - ``leaf``     单条不一致，附检查点承诺的目录字段（``leaf_record``）。
4. 各轮响应的 ``results`` 合并即为对账单，交给
   :func:`app.verify.verify_reconciliation` 离线核实缺单与错单清单。

全程只交换区间摘要与单条目录字段，原件字节不参与对账；对账期间服务端的
新发布不属于该检查点前缀，不改变本轮结果。
"""
from __future__ import annotations

from typing import Callable, Sequence

from . import merkle


def local_leaf_hashes(
    local_records: Sequence[dict | None], depth: int
) -> list[bytes]:
    """本地目录 -> 固定深度树的叶子序列（本地缺单位置以 EMPTY_LEAF 占位）。

    ``local_records`` 按下标排列（下标 = 发布序号 - 1），元素为目录字段
    ``{station_id, sample_date, order_no, blob_sha256, size}`` 或 ``None``
    （本地缺单）。超出本地目录长度的槽位一律按空叶处理。
    """
    capacity = 1 << depth
    leaves: list[bytes] = []
    for index in range(capacity):
        record = local_records[index] if index < len(local_records) else None
        if record is None:
            leaves.append(merkle.EMPTY_LEAF)
        else:
            leaves.append(
                merkle.leaf_hash(
                    record["station_id"],
                    record["sample_date"],
                    record["order_no"],
                    record["blob_sha256"],
                    int(record["size"]),
                )
            )
    return leaves


def run_reconciliation(
    local_records: Sequence[dict | None],
    checkpoint: dict,
    query: Callable[[int, list[dict]], dict],
    depth: int,
) -> dict:
    """执行完整对账，返回对账单（可直接交给离线核验函数）。

    参数
    ----
    local_records : 本地目录（见 :func:`local_leaf_hashes`）。
    checkpoint    : 留存检查点 ``{"seq":.., "root_hex":..}``。
    query         : 回调 ``(checkpoint_seq, ranges) -> POST /reconcile 响应 dict``，
                    由调用方实现 HTTP 交互；本函数只产生/消费摘要与目录字段。
    depth         : 服务端 Merkle 树深度（见 ``GET /`` 的 ``limits.tree_depth``）。

    返回的对账单 ``{"checkpoint_seq", "checkpoint_root", "tree_depth",
    "results"}`` 中，``results`` 为各轮响应结果的合并。
    """
    seq = int(checkpoint["seq"])
    expected_root = checkpoint["root_hex"]
    leaves = local_leaf_hashes(local_records, depth)
    transcript: dict = {
        "checkpoint_seq": seq,
        "checkpoint_root": expected_root,
        "tree_depth": depth,
        "results": [],
    }
    # 根摘要一致则无需任何往返：本地目录与该检查点前缀完全吻合。
    if merkle.subtree_root(leaves, 0, depth, depth).hex() == expected_root:
        transcript["results"].append(
            {"start": 0, "height": depth, "status": "match"}
        )
        return transcript

    pending = [(0, depth)]
    while pending:
        ranges = [
            {
                "start": start,
                "height": height,
                "local_root": merkle.subtree_root(leaves, start, height, depth).hex(),
            }
            for start, height in pending
        ]
        response = query(seq, ranges)
        # 响应必须绑定同一份留存检查点与树深度，否则对账结果不可混用。
        if (
            int(response["checkpoint_seq"]) != seq
            or response["checkpoint_root"] != expected_root
            or int(response["tree_depth"]) != depth
        ):
            raise ValueError("对账响应与留存检查点或树深度不一致，中止对账")
        pending = []
        for result in response["results"]:
            transcript["results"].append(result)
            if result["status"] == "diverged":
                half = 1 << (result["height"] - 1)
                pending.append((result["start"], result["height"] - 1))
                pending.append((result["start"] + half, result["height"] - 1))
    return transcript
