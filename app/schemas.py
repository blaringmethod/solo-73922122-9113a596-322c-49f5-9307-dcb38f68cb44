"""HTTP 响应模型。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ReleaseOut(BaseModel):
    id: int = Field(..., description="叶子下标 = id - 1")
    station_id: str
    sample_date: str
    order_no: str
    blob_sha256: str
    size: int
    created_at: str


class PublishResult(BaseModel):
    reused: bool = Field(..., description="True 表示同单同输入重试，复用既有记录")
    release: ReleaseOut
    checkpoint: "CheckpointOut"


class CheckpointOut(BaseModel):
    seq: int = Field(..., description="截至该检查点的发布条数")
    root_hex: str
    created_at: str


class LeafRecordOut(BaseModel):
    v: int
    station_id: str
    sample_date: str
    order_no: str
    blob_sha256: str
    size: int


class InclusionProofOut(BaseModel):
    index: int
    checkpoint_seq: int
    checkpoint_root: str
    tree_depth: int
    leaf_record: LeafRecordOut
    leaf_hash: str
    path: list[dict]


class AppendProofOut(BaseModel):
    old_seq: int
    new_seq: int
    old_root: str
    new_root: str
    tree_depth: int
    entries: list[dict] = Field(
        ..., description="完美子树条目 (start, height, hash)，数量 O(log n)"
    )


class HealthOut(BaseModel):
    status: str
    release_count: int
    problems: list[str]


# --------------------------------------------------------------------------- #
# 目录对账
# --------------------------------------------------------------------------- #
class ReconcileRangeIn(BaseModel):
    """一个待核对的 Merkle 区间：覆盖 [start, start + 2**height) 个叶子槽位。"""

    start: int = Field(
        ..., ge=0, description="区间起始叶子下标，须按 2**height 对齐"
    )
    height: int = Field(
        ..., ge=0, le=64, description="区间高度：覆盖 2**height 个槽位，0 表示单条"
    )
    local_root: str = Field(
        ...,
        pattern="^[0-9a-fA-F]{64}$",
        description="本地计算的该区间摘要（SHA-256 hex，本地缺单按空叶处理）",
    )


class ReconcileRequest(BaseModel):
    checkpoint_seq: int = Field(
        ..., ge=0, description="按该序号的历史检查点核对发布前缀"
    )
    ranges: list[ReconcileRangeIn] = Field(
        ...,
        min_length=1,
        max_length=1024,
        description=(
            "待核对区间列表。第一轮从根区间 "
            "{start:0, height:树深度, local_root:本地根摘要} 开始，"
            "之后仅对响应为 diverged 的区间按两个子区间继续核对"
        ),
    )


class ReconcileResultOut(BaseModel):
    start: int
    height: int
    status: Literal["match", "diverged", "leaf"] = Field(
        ...,
        description=(
            "match=摘要一致无需下钻；diverged=内部区间不一致，"
            "应按两个子区间继续核对；leaf=单条不一致，已附目录字段"
        ),
    )
    server_root: str | None = Field(
        None, description="服务器侧该区间摘要（diverged/leaf 时提供）"
    )
    leaf_record: LeafRecordOut | None = Field(
        None,
        description="leaf 时返回检查点承诺的目录字段；该位置为空叶时为 null",
    )


class ReconcileResponse(BaseModel):
    checkpoint_seq: int
    checkpoint_root: str = Field(
        ..., description="该历史检查点的根摘要（须与科研组留存件一致）"
    )
    tree_depth: int
    results: list[ReconcileResultOut]


PublishResult.model_rebuild()
