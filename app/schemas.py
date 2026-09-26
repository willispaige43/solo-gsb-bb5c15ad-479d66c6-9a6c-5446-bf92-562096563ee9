"""请求 / 响应的 Pydantic 模型。"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CommandItem(BaseModel):
    """单条导播口令。

    ``anchor`` 为 ``START`` 时表示以时间原点（0 ms）为锚；也可以填另一条
    口令的 id，此时 ``offset_ms`` 相对该锚点口令的 **结束时刻** 计算。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="口令唯一标识；不能为空，也不能是保留字 START")
    channel: str = Field(..., description="通道标识；只有同一通道内的口令才会产生冲突")
    anchor: str = Field(
        default="START",
        description="锚点：START（时间原点）或另一条口令的 id",
    )
    offset_ms: int = Field(
        default=0,
        description="相对锚点结束时刻的偏移（毫秒），允许为负",
    )
    duration_ms: int = Field(..., description="持续时长（毫秒），必须为正数")


class TimelineEntry(BaseModel):
    id: str
    channel: str
    anchor: str
    offset_ms: int
    duration_ms: int
    start_ms: int = Field(..., description="解析后的起点（毫秒，>= 0）")
    end_ms: int = Field(..., description="解析后的结束时刻（毫秒）")


class ConflictPair(BaseModel):
    channel: str = Field(..., description="冲突所在通道")
    a: str = Field(..., description="冲突对中 id 较小者")
    b: str = Field(..., description="冲突对中 id 较大者")
    a_interval: list[int] = Field(..., description="[a 的起点, a 的结束时刻)")
    b_interval: list[int] = Field(..., description="[b 的起点, b 的结束时刻)")


class CompileResponse(BaseModel):
    timeline: list[TimelineEntry] = Field(
        ..., description="按 (start_ms, id) 升序排列的时间线"
    )
    conflicts: list[ConflictPair] = Field(
        ..., description="同通道半开区间重叠的冲突 id 对，按 (a, b, channel) 排序"
    )


class CommandChange(BaseModel):
    """对一条现有口令的改动：指定 id，并提供 offset_ms / duration_ms 新值。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="要改动的现有口令 id（须存在于口令数组中）")
    offset_ms: int | None = Field(
        default=None,
        description="新的相对锚点结束时刻偏移（毫秒），允许为负；不填则保留原值",
    )
    duration_ms: int | None = Field(
        default=None,
        description="新的持续时长（毫秒），必须为正数；不填则保留原值",
    )

    @model_validator(mode="after")
    def require_at_least_one_new_value(self) -> "CommandChange":
        if self.offset_ms is None and self.duration_ms is None:
            raise ValueError(
                "offset_ms 与 duration_ms 至少需要提供一个新值"
            )
        return self


class PreviewRequest(BaseModel):
    """POST /preview 请求体：/compile 同格式口令数组 + 一项改动。"""

    model_config = ConfigDict(extra="forbid")

    commands: list[CommandItem] = Field(
        ..., description="口令数组，格式与 POST /compile 的请求体一致"
    )
    change: CommandChange = Field(..., description="对现有口令的一项改动")


class PreviewResponse(BaseModel):
    changed_ids: list[str] = Field(
        ...,
        description=(
            "时间起止发生变化的口令 id（含锚点依赖传导），按 id 升序"
        ),
    )
    added_conflicts: list[ConflictPair] = Field(
        ...,
        description=(
            "改动后新增的同通道冲突对，携带改动后版本的区间，"
            "按 (a, b, channel) 排序"
        ),
    )
    removed_conflicts: list[ConflictPair] = Field(
        ...,
        description=(
            "改动后消失的同通道冲突对，携带原版本的区间，"
            "按 (a, b, channel) 排序"
        ),
    )


class BackupChannelSpec(BaseModel):
    """一条口令的备用通道声明：按 id 指定允许改派到的通道列表。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="口令 id（必须存在于 commands 中，不能为空）")
    backup_channels: list[str] = Field(
        ...,
        description=(
            "备用通道名列表；不能为空字符串、同一条声明内不能重复，"
            "原通道始终可选且无需列入"
        ),
        min_length=1,
    )


class RoutingAssignRequest(BaseModel):
    """POST /routing/assign 请求体：/compile 同格式口令数组 + 按 id 的备用通道声明。"""

    model_config = ConfigDict(extra="forbid")

    commands: list[CommandItem] = Field(
        ..., description="口令数组，格式与 POST /compile 的请求体一致"
    )
    backups: list[BackupChannelSpec] = Field(
        ...,
        description=(
            "按口令 id 声明的备用通道列表；未列出的口令只能留在原通道，"
            "最多 12 条口令可拥有备用通道"
        ),
        max_length=12,
    )


class RoutingAssignResponse(BaseModel):
    timeline: list[TimelineEntry] = Field(
        ...,
        description=(
            "改派后的时间线，仅 channel 可能变化；起止时刻、排序均与 /compile 一致"
        ),
    )
    changed_ids: list[str] = Field(
        ..., description="实际改派通道的口令 id（新通道与原通道不同），按 id 升序"
    )


class RebalanceSegment(BaseModel):
    """单条节目段。

    ``fixed_start_at_sec`` 非空表示该段是固定点：开场时刻不可移动（时长仍可被
    压缩）；``actual_end_at_sec`` 非空表示该段已结束，取值为实际结束时刻。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="节目段唯一标识；不能为空")
    planned_duration_sec: int = Field(
        ..., description="计划时长（秒），必须为正数且不短于最短时长"
    )
    min_duration_sec: int = Field(
        ..., description="最短时长（秒），必须为正数；延误分摊时的压缩下限"
    )
    fixed_start_at_sec: int | None = Field(
        default=None,
        description="固定开场时刻（秒，相对节目单原点）；不可移动，缺省表示非固定点",
    )
    actual_end_at_sec: int | None = Field(
        default=None,
        description="已结束段的实际结束时刻（秒）；已结束段必须构成节目单的连续前缀",
    )


class RebalanceRequest(BaseModel):
    """POST /schedule/rebalance 请求体。"""

    model_config = ConfigDict(extra="forbid")

    segments: list[RebalanceSegment] = Field(
        ..., description="按播出顺序排列的节目段"
    )


class RebalancedSegment(BaseModel):
    id: str
    planned_duration_sec: int = Field(..., description="原计划时长（秒）")
    adjusted_duration_sec: int = Field(
        ..., description="分摊后的时长（秒）；已结束段保持计划时长"
    )
    compressed_sec: int = Field(
        ..., description="被压缩吸收的秒数（planned - adjusted），已结束段为 0"
    )
    finished: bool = Field(..., description="该段是否已结束（已结束段不参与压缩）")


class FixedPointSlack(BaseModel):
    segment_id: str = Field(..., description="固定点所在段的 id")
    fixed_start_at_sec: int = Field(..., description="固定开场时刻（秒）")
    absorbed_sec: int = Field(
        ..., description="该固定点前的窗口内被压缩吸收的延误秒数"
    )
    remaining_sec: int = Field(
        ..., description="该固定点前的剩余秒数（发生压缩时为 0）"
    )


class RebalanceResponse(BaseModel):
    segments: list[RebalancedSegment] = Field(
        ..., description="按节目顺序排列的分摊结果"
    )
    fixed_points: list[FixedPointSlack] = Field(
        ..., description="各固定点的延误吸收与剩余秒数，按固定时刻升序"
    )


class ActualRecord(BaseModel):
    """单条实播记录：实际播出口令的通道与实际起止毫秒。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="实播记录唯一标识；不能为空")
    channel: str = Field(..., description="通道标识；只有同通道的计划与实播才可匹配")
    actual_start_ms: int = Field(..., description="实际起点（毫秒），不得为负")
    actual_end_ms: int = Field(..., description="实际结束时刻（毫秒），必须大于实际起点")


class ReconcileRequest(BaseModel):
    """POST /runs/reconcile 请求体：/compile 同格式计划口令数组 + 实播记录与参数。"""

    model_config = ConfigDict(extra="forbid")

    commands: list[CommandItem] = Field(
        ..., description="计划口令数组，格式与 POST /compile 的请求体一致"
    )
    records: list[ActualRecord] = Field(..., description="实播记录数组")
    tolerance_ms: int = Field(
        ...,
        ge=0,
        description="匹配容差（毫秒）：起点偏差与终点偏差均不超过它才允许匹配",
    )
    miss_penalty: int = Field(
        ..., ge=0, description="漏播罚分：每条未匹配的计划口令计一次"
    )
    extra_penalty: int = Field(
        ..., ge=0, description="额外播出罚分：每条未匹配的实播记录计一次"
    )


class ReconcileMatch(BaseModel):
    plan_id: str = Field(..., description="匹配的计划口令 id")
    actual_id: str = Field(..., description="匹配的实播记录 id")
    channel: str = Field(..., description="匹配所在通道")
    planned_interval: list[int] = Field(..., description="计划区间 [起点, 结束时刻)")
    actual_interval: list[int] = Field(..., description="实播区间 [起点, 结束时刻)")
    start_deviation_ms: int = Field(
        ..., description="起点偏差（实际 − 计划，毫秒，带符号；罚分按绝对值计算）"
    )
    end_deviation_ms: int = Field(
        ..., description="终点偏差（实际 − 计划，毫秒，带符号；罚分按绝对值计算）"
    )


class ReconcileResponse(BaseModel):
    matches: list[ReconcileMatch] = Field(
        ..., description="匹配对列表，按 (plan_id, actual_id) 升序"
    )
    missed_plan_ids: list[str] = Field(
        ..., description="未匹配的计划口令 id（漏播），按 id 升序"
    )
    extra_actual_ids: list[str] = Field(
        ..., description="未匹配的实播记录 id（额外播出），按 id 升序"
    )
    total_penalty: int = Field(..., description="总罚分（匹配偏差绝对值之和 + 未匹配罚分）")


class CalibrateRequest(BaseModel):
    """POST /runs/calibrate 请求体：/compile 同格式计划口令数组 + 实播记录与双端容差。"""

    model_config = ConfigDict(extra="forbid")

    commands: list[CommandItem] = Field(
        ..., description="计划口令数组，格式与 POST /compile 的请求体一致"
    )
    records: list[ActualRecord] = Field(
        ...,
        description=(
            "实播记录数组；记录 id 必须出现在计划口令中，"
            "且通道必须与该口令一致"
        ),
    )
    tolerance_ms: int = Field(
        ...,
        ge=0,
        description=(
            "双端容差（毫秒），非负；计划起点与终点加偏移后都落入实播起止的"
            " ±tolerance_ms 范围内，该记录才计为一致"
        ),
    )


class CalibrateChannelResult(BaseModel):
    channel: str = Field(..., description="通道标识")
    status: Literal["calibrated", "uncalibratable"] = Field(
        ...,
        description=(
            "calibrated：已估计出整数时钟偏移；"
            "uncalibratable：所有记录都无法满足双端容差，无法校准"
        ),
    )
    offset_ms: int | None = Field(
        ...,
        description=(
            "估计出的整数时钟偏移（毫秒）：一致记录数最多，"
            "并列时绝对值最小、再数值较小；无法校准时为 null"
        ),
    )
    consistent_ids: list[str] = Field(
        ..., description="按估计偏移满足双端容差的实播记录 id，按 id 升序"
    )
    anomalous_ids: list[str] = Field(
        ..., description="按估计偏移不满足双端容差的实播记录 id，按 id 升序"
    )


class CalibrateResponse(BaseModel):
    channels: list[CalibrateChannelResult] = Field(
        ...,
        description="各通道的校准结果，按通道名升序；仅包含实播记录出现过的通道",
    )


class InterruptionPlanRequest(BaseModel):
    """POST /interruptions/plan 请求体：现有口令 + 插播目标通道 / 时长 / 起点窗口 / 可撤销口令。"""

    model_config = ConfigDict(extra="forbid")

    commands: list[CommandItem] = Field(
        ..., description="现有口令数组，格式与 POST /compile 的请求体一致"
    )
    channel: str = Field(..., description="插播目标通道标识；只有同通道口令需要考虑撤销")
    duration_ms: int = Field(..., description="插播持续时长（毫秒），必须为正数")
    earliest_start_ms: int = Field(
        ..., ge=0, description="允许的最早起点（毫秒，闭区间左端，含该点），不得为负"
    )
    latest_start_ms: int = Field(
        ..., ge=0, description="允许的最晚起点（毫秒，闭区间右端，含该点），不得为负"
    )
    cancellable_ids: list[str] = Field(
        ...,
        description=(
            "允许撤销的现有口令 id 列表；某个起点的撤销闭包（重叠同通道口令及其"
            "全部锚点后继）含列表之外的口令时，该起点不可行"
        ),
    )

    @model_validator(mode="after")
    def require_ordered_window(self) -> "InterruptionPlanRequest":
        if self.latest_start_ms < self.earliest_start_ms:
            raise ValueError(
                "latest_start_ms 不得小于 earliest_start_ms"
            )
        return self


class InterruptionPlanResponse(BaseModel):
    start_ms: int = Field(
        ...,
        description=(
            "选定的插播起点（毫秒）：撤销口令数最少、并列时起点最早"
        ),
    )
    cancelled_ids: list[str] = Field(
        ..., description="需要撤销的口令 id（含锚点后继递归），按 id 升序"
    )
    timeline: list[TimelineEntry] = Field(
        ...,
        description=(
            "撤销后剩余口令的时间线，结构与排序与 /compile 一致（不含插播口令本身）"
        ),
    )


class FillerItem(BaseModel):
    """单条填充素材：id、正整数时长、非负整数优先级。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="素材唯一标识；不能为空，各素材之间不可重复")
    duration_ms: int = Field(..., description="素材时长（毫秒），必须为正整数")
    priority: int = Field(..., description="素材优先级，必须为非负整数")


class FillerPlanRequest(BaseModel):
    """POST /fillers/plan 请求体：/compile 同格式口令数组 + 目标通道 / 视界 / 素材列表。"""

    model_config = ConfigDict(extra="forbid")

    commands: list[CommandItem] = Field(
        ..., description="现有口令数组，格式与 POST /compile 的请求体一致"
    )
    channel: str = Field(
        ..., description="目标通道标识；只有同通道口令参与占用计算，不能为空"
    )
    horizon_end_ms: int = Field(
        ...,
        description="规划视界结束时刻（毫秒），必须为正数；规划范围为 [0, horizon_end_ms)",
    )
    fillers: list[FillerItem] = Field(
        ...,
        description="填充素材列表，至多 12 条；每条素材最多使用一次",
        max_length=12,
    )


class FillerPlacement(BaseModel):
    id: str = Field(..., description="入选素材 id")
    start_ms: int = Field(..., description="排布起点（毫秒）")
    end_ms: int = Field(..., description="排布结束时刻（毫秒）")


class FillerGap(BaseModel):
    start_ms: int = Field(..., description="空档起点（毫秒，半开区间左端）")
    end_ms: int = Field(..., description="空档结束时刻（毫秒，半开区间右端）")
    remaining_ms: int = Field(..., description="填充后空档剩余时长（毫秒）")


class FillerPlanResponse(BaseModel):
    placements: list[FillerPlacement] = Field(
        ..., description="入选素材的排布，按空档时间顺序、空档内按 id 升序"
    )
    gaps: list[FillerGap] = Field(
        ..., description="全部空档及其填充后的剩余时长，按时间顺序"
    )


class MergeRequest(BaseModel):
    """POST /versions/merge 请求体：base / left / right 三份 /compile 同格式口令数组。"""

    model_config = ConfigDict(extra="forbid")

    base: list[CommandItem] = Field(
        ..., description="合并基线口令数组，格式与 POST /compile 的请求体一致"
    )
    left: list[CommandItem] = Field(
        ..., description="左侧版本口令数组，格式与 POST /compile 的请求体一致"
    )
    right: list[CommandItem] = Field(
        ..., description="右侧版本口令数组，格式与 POST /compile 的请求体一致"
    )


class MergeDivergence(BaseModel):
    id: str = Field(..., description="发生分歧的口令 id")
    field: Literal["channel", "anchor", "offset_ms", "duration_ms"] = Field(
        ..., description="发生分歧的字段名，按 (id, field) 排序"
    )
    kind: Literal["MODIFY_MODIFY", "ADD_ADD", "DELETE_MODIFY"] = Field(
        ...,
        description=(
            "分歧类型：双侧对同一字段改成不同值（MODIFY_MODIFY）；"
            "双侧新增同 id 但字段值不同（ADD_ADD）；"
            "一侧删除整条口令而另一侧修改该字段（DELETE_MODIFY）"
        ),
    )
    base_value: str | int | None = Field(
        ..., description="base 中该字段的值；双侧新增或 base 被删除时为 null"
    )
    left_value: str | int | None = Field(
        ..., description="left 中该字段的值；left 删除该口令时为 null"
    )
    right_value: str | int | None = Field(
        ..., description="right 中该字段的值；right 删除该口令时为 null"
    )


class MergeResponseMerged(BaseModel):
    """无分歧时的合并成功响应：合并口令 + 与 /compile 同结构的时间线和冲突对。"""

    status: Literal["merged"] = Field(
        ..., description="合并结果状态：merged（无分歧，已完成编排校验）"
    )
    commands: list[CommandItem] = Field(
        ..., description="合并后的口令数组（已补全缺省字段），按 id 升序"
    )
    timeline: list[TimelineEntry] = Field(
        ..., description="对合并口令调用编排器得到的时间线，结构与 /compile 一致"
    )
    conflicts: list[ConflictPair] = Field(
        ..., description="合并口令的同通道冲突对，结构与 /compile 一致"
    )


class MergeResponseDiverged(BaseModel):
    """存在分歧时的响应：只返回按 (id, field) 排序的分歧，不生成合并结果。"""

    status: Literal["diverged"] = Field(..., description="合并结果状态：diverged（存在分歧）")
    divergences: list[MergeDivergence] = Field(
        ..., description="分歧列表，按 (id, field) 升序"
    )


MergeResponse = Annotated[
    Union[MergeResponseMerged, MergeResponseDiverged],
    Field(discriminator="status"),
]


class SwitchItem(BaseModel):
    """单个布尔开关：名称与当前取值。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="开关名；不能为空，各开关之间不可重复")
    current: bool = Field(..., description="开关当前取值；翻转数相对它计算")


class BranchCondition(BaseModel):
    """一条口令的启用条件：按口令 id 给出的开关取值要求。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="口令 id（必须存在于 commands 中，不能为空）")
    requires: dict[str, bool] = Field(
        ...,
        description=(
            "开关名 -> 要求取值；全部成立时该口令才启用，空对象等价于未设置条件"
        ),
    )


class BranchSelectRequest(BaseModel):
    """POST /branches/select 请求体：/compile 同格式口令数组 + 有序开关 + 按 id 的启用条件。"""

    model_config = ConfigDict(extra="forbid")

    commands: list[CommandItem] = Field(
        ..., description="现有口令数组，格式与 POST /compile 的请求体一致"
    )
    switches: list[SwitchItem] = Field(
        ...,
        description=(
            "有序布尔开关列表（名称互异），至多 8 个；"
            "组合枚举、翻转数与字典序裁决均按输入顺序"
        ),
        max_length=8,
    )
    conditions: list[BranchCondition] = Field(
        ...,
        description="按口令 id 给出的开关取值条件；未列出的口令始终启用",
    )


class BranchSelectResponse(BaseModel):
    values: dict[str, bool] = Field(
        ..., description="选中的开关取值（按 switches 输入顺序）"
    )
    flipped: list[str] = Field(
        ..., description="相对当前取值被翻转的开关名，按 switches 输入顺序"
    )
    timeline: list[TimelineEntry] = Field(
        ...,
        description=(
            "选中取值下最终启用口令的时间线，结构与排序与 /compile 一致"
        ),
    )


class PreloadAssetItem(BaseModel):
    """单条预载素材：id、关联口令 id、就绪时刻、加载时长、优先级。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="素材唯一标识；不能为空，各素材之间不可重复")
    command_id: str = Field(
        ...,
        description=(
            "关联口令 id（必须存在于 commands 中，不能为空）；"
            "该口令解析出的起点即素材加载截止时刻，且一条口令至多被一条素材关联"
        ),
    )
    ready_ms: int = Field(..., description="素材最早可开始加载时刻（毫秒），必须为非负整数")
    load_ms: int = Field(..., description="素材串行加载时长（毫秒），必须为正整数")
    priority: int = Field(..., description="素材优先级，必须为非负整数")


class PreloadRequest(BaseModel):
    """POST /assets/preload 请求体：/compile 同格式口令数组 + 至多 12 条预载素材。"""

    model_config = ConfigDict(extra="forbid")

    commands: list[CommandItem] = Field(
        ..., description="现有口令数组，格式与 POST /compile 的请求体一致"
    )
    assets: list[PreloadAssetItem] = Field(
        ...,
        description=(
            "预载素材列表，至多 12 条；素材 id 与关联的 command_id 均不可重复，"
            "关联口令必须存在"
        ),
        max_length=12,
    )


class PreloadLoadEntry(BaseModel):
    id: str = Field(..., description="入选（按时加载）素材 id")
    start_ms: int = Field(..., description="实际开始加载时刻（毫秒），不早于 ready_ms")
    end_ms: int = Field(..., description="实际加载完成时刻（毫秒），不晚于关联口令起点")


class PreloadResponse(BaseModel):
    loads: list[PreloadLoadEntry] = Field(
        ...,
        description=(
            "入选素材的加载安排，按加载器实际加载顺序排列；"
            "同一入选集合取总完工最早、并列取加载 id 顺序字典序最小"
        ),
    )
    skipped_ids: list[str] = Field(
        ..., description="未入选（跳过）的素材 id，按 id 升序"
    )


class ReplayCandidateItem(BaseModel):
    """单个回放候选：现有口令 id 与其非负整数优先级。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        ...,
        description=(
            "候选口令 id（必须存在于 commands 中，不能为空）；"
            "各候选之间不可重复"
        ),
    )
    priority: int = Field(..., description="候选优先级，必须为非负整数")


class ReplayPlanRequest(BaseModel):
    """POST /replays/plan 请求体：/compile 同格式口令数组 + 候选列表 / 留白 / 预算。"""

    model_config = ConfigDict(extra="forbid")

    commands: list[CommandItem] = Field(
        ..., description="现有口令数组，格式与 POST /compile 的请求体一致"
    )
    candidates: list[ReplayCandidateItem] = Field(
        ...,
        description="回放候选口令列表，至多 12 条；每条携带非负整数 priority，允许为空数组",
        max_length=12,
    )
    lead_in_ms: int = Field(
        ...,
        description=(
            "前留白毫秒数，必须为非负整数；"
            "候选录制窗口起点为 max(0, 口令起点 - 前留白)"
        ),
    )
    lead_out_ms: int = Field(
        ...,
        description="后留白毫秒数，必须为非负整数；候选录制窗口终点为口令结束时刻 + 后留白",
    )
    budget_ms: int = Field(
        ...,
        description=(
            "回放时长预算（毫秒），必须为正数；"
            "跨通道合并后的录制窗口总时长不得超过它"
        ),
    )


class ReplaySegment(BaseModel):
    start_ms: int = Field(..., description="合并片段起点（毫秒，半开区间左端）")
    end_ms: int = Field(..., description="合并片段结束时刻（毫秒，半开区间右端）")


class ReplayPlanResponse(BaseModel):
    selected_ids: list[str] = Field(
        ...,
        description=(
            "入选候选口令 id，按 id 升序；预算内优先级总和最大、"
            "并列条数最多、再并列入选 id 升序列表字典序最小"
        ),
    )
    segments: list[ReplaySegment] = Field(
        ...,
        description=(
            "入选候选全部录制窗口跨通道合并（重叠或首尾相接）后的片段，"
            "按起点升序"
        ),
    )
    total_duration_ms: int = Field(
        ..., description="合并片段总时长（毫秒），即计入预算的录制时长"
    )


class ErrorBody(BaseModel):
    code: Literal[
        "INVALID_REQUEST",
        "DUPLICATE_ID",
        "INVALID_ID",
        "INVALID_CHANNEL",
        "INVALID_DURATION",
        "MISSING_ANCHOR",
        "DEPENDENCY_CYCLE",
        "NEGATIVE_START",
        "CHANGE_TARGET_NOT_FOUND",
        "INVALID_FIXED_START",
        "INVALID_ACTUAL_END",
        "INSUFFICIENT_MARGIN",
        "INVALID_INTERVAL",
        "CALIBRATION_TARGET_NOT_FOUND",
        "CHANNEL_MISMATCH",
        "ROUTING_TARGET_NOT_FOUND",
        "DUPLICATE_BACKUP_CHANNEL",
        "TOO_MANY_BACKUP_TARGETS",
        "NO_FEASIBLE_ROUTING",
        "INTERRUPTION_TARGET_NOT_FOUND",
        "NO_FEASIBLE_INTERRUPTION",
        "INVALID_HORIZON",
        "INVALID_PRIORITY",
        "TOO_MANY_FILLERS",
        "TOO_MANY_GAPS",
        "INVALID_SWITCH",
        "DUPLICATE_SWITCH",
        "TOO_MANY_SWITCHES",
        "CONDITION_TARGET_NOT_FOUND",
        "UNKNOWN_SWITCH",
        "NO_SAFE_COMBINATION",
        "PRELOAD_TARGET_NOT_FOUND",
        "DUPLICATE_COMMAND_LINK",
        "INVALID_READY_MS",
        "TOO_MANY_PRELOAD_ASSETS",
        "REPLAY_TARGET_NOT_FOUND",
        "INVALID_REPLAY_PADDING",
        "INVALID_REPLAY_BUDGET",
        "TOO_MANY_REPLAY_CANDIDATES",
        "RECONCILE_BUSY",
        "RECONCILE_TIMEOUT",
    ]
    message: str
    details: dict | None = None
