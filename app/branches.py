"""开关分支选择核心逻辑（纯标准库实现，不依赖 Web 框架）。

规则概述
--------
* 请求沿用 ``POST /compile`` 的口令数组，先用同一个编排器解析并校验每条口令的
  起止毫秒；另附至多 8 个名称互异的**有序**布尔开关（各含当前取值），以及按
  口令 id 给出的开关取值条件；未设置条件的口令始终启用。
* 枚举全部开关组合：某组合下口令的全部条件成立才启用；启用口令引用了未启用
  锚点的组合无效；其余组合中同通道半开区间存在重叠（首尾相接不算）的也不安全，
  一并排除。
* 在安全组合中先取相对当前取值**翻转开关数最少**者；并列时按开关输入顺序、
  false 小于 true 的取值序列字典序最小者。
* 返回选中取值、被翻转的开关名与最终启用口令的时间线；无安全组合时抛出
  ``NO_SAFE_COMBINATION``。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .compiler import START, CompileError, compile_commands

# 一次分支选择最多允许的布尔开关个数
MAX_SWITCHES = 8


class BranchError(CompileError):
    """分支选择失败的业务异常；复用 CompileError 的 422 转换通道。"""


def select_branch(
    commands: list[dict[str, Any]],
    switches: list[dict[str, Any]],
    conditions: list[dict[str, Any]],
) -> dict[str, Any]:
    """枚举开关组合，求翻转最少、并列取值序列字典序最小的安全组合。

    不会修改传入的 ``commands`` / ``switches`` / ``conditions``。

    :raises CompileError: 口令编排非法（重复 id、依赖环、负起点等，与 /compile 相同）。
    :raises BranchError: 开关或条件非法（空开关名、开关名重复、超过 8 个、空口令 id、
        重复条件声明、未知口令 id、未知开关）或不存在安全组合时抛出，
        ``code`` 可用于程序化区分错误类型。
    """
    # 1) 复用编排器解析并校验现有口令（深拷贝，setdefault 等不会回写调用方数据；
    #    分支选择只需解析后的时间线，跳过 O(n²) 的冲突对枚举）
    compiled = compile_commands(deepcopy(commands), with_conflicts=False)
    timeline = compiled["timeline"]
    entries = {entry["id"]: entry for entry in timeline}

    # 2) 校验开关与条件，得到输入顺序的 (开关名, 当前取值) 与 id -> 条件映射
    switch_list = _validate_switches(switches)
    condition_map = _validate_conditions(
        conditions, entries, {name for name, _ in switch_list}
    )

    # 3) 枚举全部开关组合（至多 2^8 = 256 个），筛出安全组合并保留最优
    n = len(switch_list)
    names = [name for name, _ in switch_list]
    currents = [current for _, current in switch_list]
    index_of = {name: i for i, name in enumerate(names)}
    # 每条带条件口令的 (开关下标, 要求取值) 列表；未设置条件的口令不在表中
    requirements = {
        cid: [(index_of[name], want) for name, want in requires.items()]
        for cid, requires in condition_map.items()
    }

    best_values: list[bool] | None = None
    best_enabled: set[str] | None = None
    best_key: tuple[int, tuple[bool, ...]] | None = None
    for mask in range(1 << n):
        values = [bool(mask >> i & 1) for i in range(n)]
        enabled = {
            entry["id"]
            for entry in timeline
            if all(values[i] == want for i, want in requirements.get(entry["id"], ()))
        }
        # 启用口令的锚点必须同样启用（START 为虚拟锚点，始终可用）
        if any(
            entries[cid]["anchor"] != START and entries[cid]["anchor"] not in enabled
            for cid in enabled
        ):
            continue
        # 同通道半开区间重叠的组合不安全，排除
        if _has_channel_conflict(entries, enabled):
            continue
        # 裁决：翻转数最少；并列时按输入顺序 false < true 的取值序列字典序最小
        flips = sum(1 for i in range(n) if values[i] != currents[i])
        key = (flips, tuple(values))
        if best_key is None or key < best_key:
            best_key = key
            best_values = values
            best_enabled = enabled

    if best_enabled is None or best_values is None:
        raise BranchError(
            "NO_SAFE_COMBINATION",
            "不存在满足全部启用条件、锚点依赖闭合且无同通道冲突的开关组合",
            {"combinations": 1 << n},
        )

    # 4) 组装结果：values / flipped 按开关输入顺序，timeline 沿用编排排序
    return {
        "values": {names[i]: best_values[i] for i in range(n)},
        "flipped": [names[i] for i in range(n) if best_values[i] != currents[i]],
        "timeline": [dict(entry) for entry in timeline if entry["id"] in best_enabled],
    }


def _validate_switches(switches: list[dict[str, Any]]) -> list[tuple[str, bool]]:
    """校验开关列表，返回按输入顺序排列的 (name, current) 列表。"""
    # 1) 开关名不能为空
    if any(switch["name"] == "" for switch in switches):
        raise BranchError("INVALID_SWITCH", "开关名不能为空")

    # 2) 开关名互异
    seen: set[str] = set()
    duplicates: list[str] = []
    for switch in switches:
        name = switch["name"]
        if name in seen and name not in duplicates:
            duplicates.append(name)
        seen.add(name)
    if duplicates:
        raise BranchError(
            "DUPLICATE_SWITCH",
            f"存在重复的开关名: {', '.join(sorted(duplicates))}",
            {"duplicates": sorted(duplicates)},
        )

    # 3) 个数上限（HTTP 层由请求结构校验提前拦截，此处兜底直接调用核心逻辑的场景）
    if len(switches) > MAX_SWITCHES:
        raise BranchError(
            "TOO_MANY_SWITCHES",
            f"最多 {MAX_SWITCHES} 个布尔开关参与一次分支选择，当前 {len(switches)} 个",
            {"count": len(switches), "limit": MAX_SWITCHES},
        )

    return [(switch["name"], switch["current"]) for switch in switches]


def _validate_conditions(
    conditions: list[dict[str, Any]],
    entries: dict[str, dict[str, Any]],
    switch_names: set[str],
) -> dict[str, dict[str, bool]]:
    """校验条件列表，返回 口令 id -> {开关名: 要求取值} 的映射。"""
    # 1) 条件指向的口令 id 不能为空
    if any(condition["id"] == "" for condition in conditions):
        raise BranchError("INVALID_ID", "条件指向的口令 id 不能为空")

    # 2) 同一口令只能声明一次条件
    declared: dict[str, dict[str, bool]] = {}
    duplicates: list[str] = []
    for condition in conditions:
        cid = condition["id"]
        if cid in declared:
            if cid not in duplicates:
                duplicates.append(cid)
            continue
        declared[cid] = dict(condition["requires"])
    if duplicates:
        raise BranchError(
            "DUPLICATE_ID",
            f"存在重复的口令条件声明: {', '.join(sorted(duplicates))}",
            {"duplicates": sorted(duplicates)},
        )

    # 3) 条件指向的口令必须存在
    unknown = sorted(cid for cid in declared if cid not in entries)
    if unknown:
        raise BranchError(
            "CONDITION_TARGET_NOT_FOUND",
            f"条件引用了不存在的口令 id: {', '.join(unknown)}",
            {"ids": unknown},
        )

    # 4) 条件引用的开关必须已定义
    unknown_switches = sorted(
        {name for requires in declared.values() for name in requires} - switch_names
    )
    if unknown_switches:
        raise BranchError(
            "UNKNOWN_SWITCH",
            f"条件引用了未定义的开关: {', '.join(unknown_switches)}",
            {"switches": unknown_switches},
        )

    return declared


def _has_channel_conflict(
    entries: dict[str, dict[str, Any]], enabled: set[str]
) -> bool:
    """启用的口令集合内是否存在同通道半开区间重叠（首尾相接不算）。"""
    by_channel: dict[str, list[tuple[int, int]]] = {}
    for cid in enabled:
        entry = entries[cid]
        by_channel.setdefault(entry["channel"], []).append(
            (entry["start_ms"], entry["end_ms"])
        )
    for intervals in by_channel.values():
        intervals.sort()
        max_end = intervals[0][1]
        for start, end in intervals[1:]:
            if start < max_end:  # 半开区间重叠；start == max_end 为首尾相接
                return True
            if end > max_end:
                max_end = end
    return False
