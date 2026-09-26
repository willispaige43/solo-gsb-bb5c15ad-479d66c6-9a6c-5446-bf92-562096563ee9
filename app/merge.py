"""口令三方合并核心逻辑（纯标准库实现，不依赖 Web 框架）。

规则概述
--------
* 输入 base / left / right 三份与 ``POST /compile`` 同格式的口令数组，先各自补全
  缺省字段（``anchor`` 缺省为 ``START``、``offset_ms`` 缺省为 ``0``）。
* 以 ``id`` 为口令身份、按字段做三方合并：

  - 只有一侧相对 base 改动时，直接采纳该侧改动（含单侧新增、单侧删除）；
  - 两侧都未改动（含双侧一致删除）时，保留 base 原值 / 删除；
  - 两侧对同一字段改成相同值时采纳该值；
  - 两侧对同一字段改成不同值时记为分歧；
  - 一侧删除整条口令、另一侧修改了字段时，每个被修改字段各记一条分歧；
  - 同 id 在两侧都新增（base 中不存在）时逐字段对比，字段值不同即分歧。
* 两侧分别修改同一条口令的不同字段时，改动合并进同一条口令。
* 任一分歧都不会生成合并结果；分歧按 ``(id, field)`` 升序返回。
* 无分歧时复用 ``app.compiler`` 的编排器校验并编译合并口令，缺失锚点、依赖环、
  负起点等沿用原有错误码；成功时返回按 id 排序的合并口令，以及与 ``/compile``
  同结构的时间线与冲突对。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .compiler import START, CompileError, compile_commands

# 参与三方合并的字段（按字段名字典序，用于分歧排序与逐字段对比）
MERGE_FIELDS: tuple[str, ...] = (
    "anchor",
    "channel",
    "duration_ms",
    "offset_ms",
)

# 分歧类型：双侧改成不同值 / 双侧新增但内容不同 / 一侧删除另一侧修改
KIND_MODIFY_MODIFY = "MODIFY_MODIFY"
KIND_ADD_ADD = "ADD_ADD"
KIND_DELETE_MODIFY = "DELETE_MODIFY"


class MergeError(CompileError):
    """三方合并输入非法的业务异常；复用 CompileError 的 422 转换通道。"""


def merge_versions(
    base: list[dict[str, Any]],
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
) -> dict[str, Any]:
    """对三份口令版本做三方合并，返回合并口令、时间线与冲突对。

    不会修改传入的 ``base`` / ``left`` / ``right``。

    :raises CompileError: 合并口令编排非法（缺失锚点、依赖环、负起点等，
        错误码与 /compile 相同）。
    :raises MergeError: 某一版本内部存在重复 id 时抛出 ``DUPLICATE_ID``。
    """
    # 1) 补全缺省字段并建立 id 索引（深拷贝，绝不回写调用方数据）
    base_items = _normalize(base, "base")
    left_items = _normalize(left, "left")
    right_items = _normalize(right, "right")

    all_ids = sorted(set(base_items) | set(left_items) | set(right_items))

    # 2) 逐口令、逐字段三方合并，收集分歧
    divergences: list[dict[str, Any]] = []
    merged: list[dict[str, Any]] = []
    for cid in all_ids:
        b = base_items.get(cid)
        l = left_items.get(cid)
        r = right_items.get(cid)

        if b is not None:
            merged_item = _merge_existing(cid, b, l, r, divergences)
        else:
            merged_item = _merge_both_added(cid, l, r, divergences)
        if merged_item is not None:
            merged.append(merged_item)

    # 3) 有分歧时不生成合并结果
    if divergences:
        divergences.sort(key=lambda d: (d["id"], d["field"]))
        return {"status": "diverged", "divergences": divergences}

    # 合并口令按 id 排序，保证结果确定性与请求无状态
    merged.sort(key=lambda item: item["id"])

    # 4) 复用现有编排器校验合并口令：缺失锚点 / 依赖环 / 负起点等沿用原错误码
    compiled = compile_commands(deepcopy(merged))
    return {
        "status": "merged",
        "commands": merged,
        "timeline": compiled["timeline"],
        "conflicts": compiled["conflicts"],
    }


def _normalize(items: list[dict[str, Any]], side: str) -> dict[str, dict[str, Any]]:
    """深拷贝一份口令并补全缺省字段，返回以 id 为键的映射。

    :raises MergeError: 该版本内部出现重复 id 时抛出 ``DUPLICATE_ID``。
    """
    by_id: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for raw in items:
        item = deepcopy(raw)
        item.setdefault("anchor", START)
        item.setdefault("offset_ms", 0)
        cid = item["id"]
        if cid in by_id:
            if cid not in duplicates:
                duplicates.append(cid)
            continue
        by_id[cid] = item

    if duplicates:
        raise MergeError(
            "DUPLICATE_ID",
            f"{side} 版本中存在重复的口令 id: {', '.join(sorted(duplicates))}",
            {"side": side, "duplicates": sorted(duplicates)},
        )
    return by_id


def _merge_existing(
    cid: str,
    base_item: dict[str, Any],
    left_item: dict[str, Any] | None,
    right_item: dict[str, Any] | None,
    divergences: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """合并 base 中存在的口令；返回合并口令，双侧一致删除时返回 ``None``。"""
    # 双侧都删除：一致采纳删除
    if left_item is None and right_item is None:
        return None

    # 单侧删除、另一侧修改：每个被修改字段各记一条分歧，口令不进合并结果
    if left_item is None or right_item is None:
        survivor = right_item if left_item is None else left_item
        survivor_side = "right" if left_item is None else "left"
        changed = [
            field
            for field in MERGE_FIELDS
            if survivor[field] != base_item[field]
        ]
        for field in changed:
            divergences.append(
                {
                    "id": cid,
                    "field": field,
                    "kind": KIND_DELETE_MODIFY,
                    "base_value": base_item[field],
                    "left_value": (
                        survivor[field] if survivor_side == "left" else None
                    ),
                    "right_value": (
                        survivor[field] if survivor_side == "right" else None
                    ),
                }
            )
        return None

    # 双侧都保留：逐字段合并
    merged_item = {"id": cid}
    for field in MERGE_FIELDS:
        base_value = base_item[field]
        left_value = left_item[field]
        right_value = right_item[field]
        left_changed = left_value != base_value
        right_changed = right_value != base_value

        if not left_changed and not right_changed:
            merged_item[field] = base_value
        elif left_changed and not right_changed:
            merged_item[field] = left_value  # 单侧改动直接采纳
        elif right_changed and not left_changed:
            merged_item[field] = right_value
        elif left_value == right_value:
            merged_item[field] = left_value  # 双侧改为相同值
        else:
            divergences.append(
                {
                    "id": cid,
                    "field": field,
                    "kind": KIND_MODIFY_MODIFY,
                    "base_value": base_value,
                    "left_value": left_value,
                    "right_value": right_value,
                }
            )
    return merged_item


def _merge_both_added(
    cid: str,
    left_item: dict[str, Any] | None,
    right_item: dict[str, Any] | None,
    divergences: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """合并 base 中不存在的口令：单侧新增直接采纳；双侧新增逐字段对比。"""
    # 单侧新增（另一侧无此 id 且 base 也没有）直接采纳
    if left_item is None or right_item is None:
        return deepcopy(left_item if left_item is not None else right_item)

    # 双侧新增：字段值全部一致才采纳，否则每个不同字段各记一条分歧
    merged_item = {"id": cid}
    for field in MERGE_FIELDS:
        left_value = left_item[field]
        right_value = right_item[field]
        if left_value == right_value:
            merged_item[field] = left_value
        else:
            divergences.append(
                {
                    "id": cid,
                    "field": field,
                    "kind": KIND_ADD_ADD,
                    "base_value": None,
                    "left_value": left_value,
                    "right_value": right_value,
                }
            )
    return merged_item
