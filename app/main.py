"""FastAPI 入口：直播导播口令编排 API。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .branches import select_branch
from .calibrate import calibrate_runs
from .compiler import CompileError, compile_commands, preview_change
from .fillers import plan_fillers
from .interruptions import plan_interruption
from .merge import merge_versions
from .preload import plan_preload
from .rebalance import rebalance_schedule
from .reconcile import reconcile_runs
from .replays import plan_replays
from .routing import assign_routing
from .schemas import (
    BranchSelectRequest,
    BranchSelectResponse,
    CalibrateRequest,
    CalibrateResponse,
    CommandItem,
    CompileResponse,
    ErrorBody,
    FillerPlanRequest,
    FillerPlanResponse,
    InterruptionPlanRequest,
    InterruptionPlanResponse,
    MergeRequest,
    MergeResponse,
    PreviewRequest,
    PreviewResponse,
    PreloadRequest,
    PreloadResponse,
    RebalanceRequest,
    RebalanceResponse,
    ReconcileRequest,
    ReconcileResponse,
    ReplayPlanRequest,
    ReplayPlanResponse,
    RoutingAssignRequest,
    RoutingAssignResponse,
)

app = FastAPI(
    title="直播导播口令编排 API",
    version="1.11.0",
    description=(
        "接收导播口令数组，解析锚点依赖得到每条口令的起止毫秒时间，"
        "并列出同通道半开区间重叠的冲突 id 对；"
        "支持预览单条口令改动带来的时间传导与冲突对增减；"
        "并按可压缩余量比例分摊节目单延误，保证固定开场时刻不被移动；"
        "直播结束后还可将计划口令与实播记录按通道保序对账，"
        "最小化起止偏差与漏播、额外播出罚分之和；"
        "亦可逐通道估计整数时钟偏移，按双端容差划分一致与异常记录；"
        "亦可在不改变任何起止时刻的前提下，把口令全局改派到按 id 指定的"
        "备用通道，求改派口令最少的无冲突方案；"
        "还可对 base / left / right 三份口令版本按 id 和字段做无状态三方合并；"
        "亦可在给定起点窗口内规划临时插播，求撤销口令最少、并列起点最早的方案；"
        "还可在目标通道视界内的空档中联合分配填充素材，"
        "求填充总时长最大、并列优先级最高、再并列 id 序列字典序最小的排布；"
        "亦可在至多 8 个布尔开关的组合空间中选择启用的口令子集，"
        "求翻转开关数最少、并列取值序列字典序最小的安全组合；"
        "还可在口令起点截止、素材就绪时刻与单台串行加载器约束下规划素材预载，"
        "求按时加载条数最多、并列优先级总和最大、再并列入选 id 升序列表字典序最小，"
        "同一入选集合取最早完工、并列加载 id 顺序字典序最小的方案；"
        "亦可在至多 12 个带优先级的候选口令中挑选回放录制集合："
        "每个候选生成 [max(0, 起点-前留白), 终点+后留白) 的全局录制窗口，"
        "跨通道重叠或首尾相接的窗口合并后才计入回放时长预算，"
        "求预算内优先级总和最大、并列入选条数最多、"
        "再并列入选 id 升序列表字典序最小的集合。"
    ),
)


@app.exception_handler(CompileError)
async def compile_error_handler(_request: Request, exc: CompileError) -> JSONResponse:
    """业务编排错误统一返回 422 + 结构化错误码。"""
    return JSONResponse(status_code=422, content=exc.to_dict())


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """请求体结构错误同样使用统一错误格式。"""
    return JSONResponse(
        status_code=422,
        content={
            "code": "INVALID_REQUEST",
            "message": "请求体不符合接口约定的结构",
            # errors 上下文中可能含异常对象，经 jsonable_encoder 保证可序列化
            "details": {"errors": jsonable_encoder(exc.errors())},
        },
    )


@app.get("/health", tags=["meta"], summary="健康检查")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/compile",
    response_model=CompileResponse,
    tags=["compile"],
    summary="编排口令：解析时间并检测通道冲突",
    responses={422: {"model": ErrorBody, "description": "请求非法或编排失败"}},
)
async def compile(items: list[CommandItem]) -> dict:
    # 核心逻辑不依赖 Pydantic，直接喂入 dict 以便独立测试与复用
    return compile_commands([item.model_dump() for item in items])


@app.post(
    "/preview",
    response_model=PreviewResponse,
    tags=["compile"],
    summary="预览改动：对比改动前后的时间变化与冲突对增减",
    responses={422: {"model": ErrorBody, "description": "请求非法或编排失败"}},
)
async def preview(body: PreviewRequest) -> dict:
    change = body.change.model_dump(exclude_none=True)
    return preview_change(
        [item.model_dump() for item in body.commands], change
    )


@app.post(
    "/schedule/rebalance",
    response_model=RebalanceResponse,
    tags=["schedule"],
    summary="节目单延误分摊：按可压缩余量比例分摊延误，守住固定开场时刻",
    responses={422: {"model": ErrorBody, "description": "请求非法或可压缩余量不足"}},
)
async def rebalance(body: RebalanceRequest) -> dict:
    # 核心逻辑不依赖 Pydantic，直接喂入 dict 以便独立测试与复用
    return rebalance_schedule([item.model_dump() for item in body.segments])


@app.post(
    "/runs/reconcile",
    response_model=ReconcileResponse,
    tags=["runs"],
    summary="实播对账：计划口令与实播记录的保序最优匹配",
    responses={422: {"model": ErrorBody, "description": "请求非法或对账失败"}},
)
async def reconcile(body: ReconcileRequest) -> dict:
    # 核心逻辑不依赖 Pydantic，直接喂入 dict 以便独立测试与复用
    return reconcile_runs(
        [item.model_dump() for item in body.commands],
        [record.model_dump() for record in body.records],
        tolerance_ms=body.tolerance_ms,
        miss_penalty=body.miss_penalty,
        extra_penalty=body.extra_penalty,
    )


@app.post(
    "/runs/calibrate",
    response_model=CalibrateResponse,
    tags=["runs"],
    summary="时钟校准：逐通道估计整数时钟偏移并划分一致/异常记录",
    responses={422: {"model": ErrorBody, "description": "请求非法或校准失败"}},
)
async def calibrate(body: CalibrateRequest) -> dict:
    # 核心逻辑不依赖 Pydantic，直接喂入 dict 以便独立测试与复用
    return calibrate_runs(
        [item.model_dump() for item in body.commands],
        [record.model_dump() for record in body.records],
        tolerance_ms=body.tolerance_ms,
    )


@app.post(
    "/routing/assign",
    response_model=RoutingAssignResponse,
    tags=["routing"],
    summary="通道改派：不改起止时刻，求改派口令最少的无冲突方案",
    responses={422: {"model": ErrorBody, "description": "请求非法或无可行方案"}},
)
async def assign(body: RoutingAssignRequest) -> dict:
    # 核心逻辑不依赖 Pydantic，直接喂入 dict 以便独立测试与复用
    return assign_routing(
        [item.model_dump() for item in body.commands],
        [spec.model_dump() for spec in body.backups],
    )


@app.post(
    "/versions/merge",
    response_model=MergeResponse,
    tags=["versions"],
    summary="口令三方合并：按 id 和字段合并三份口令版本并校验编排",
    responses={422: {"model": ErrorBody, "description": "请求非法、版本内重复 id 或合并口令编排失败"}},
)
async def merge(body: MergeRequest) -> dict:
    # 请求无状态：核心逻辑不依赖 Pydantic，直接喂入 dict 以便独立测试与复用
    return merge_versions(
        [item.model_dump() for item in body.base],
        [item.model_dump() for item in body.left],
        [item.model_dump() for item in body.right],
    )


@app.post(
    "/fillers/plan",
    response_model=FillerPlanResponse,
    tags=["fillers"],
    summary="空档填充：在视界空档内联合分配素材，求填充总时长最大的排布",
    responses={422: {"model": ErrorBody, "description": "请求非法、素材非法或空档超限"}},
)
async def filler_plan(body: FillerPlanRequest) -> dict:
    # 核心逻辑不依赖 Pydantic，直接喂入 dict 以便独立测试与复用
    return plan_fillers(
        [item.model_dump() for item in body.commands],
        channel=body.channel,
        horizon_end_ms=body.horizon_end_ms,
        fillers=[item.model_dump() for item in body.fillers],
    )


@app.post(
    "/interruptions/plan",
    response_model=InterruptionPlanResponse,
    tags=["interruptions"],
    summary="插播规划：在起点窗口内求撤销口令最少、并列起点最早的插播方案",
    responses={422: {"model": ErrorBody, "description": "请求非法或窗口内无可行起点"}},
)
async def interruption_plan(body: InterruptionPlanRequest) -> dict:
    # 核心逻辑不依赖 Pydantic，直接喂入 dict 以便独立测试与复用
    return plan_interruption(
        [item.model_dump() for item in body.commands],
        channel=body.channel,
        duration_ms=body.duration_ms,
        earliest_start_ms=body.earliest_start_ms,
        latest_start_ms=body.latest_start_ms,
        cancellable_ids=list(body.cancellable_ids),
    )


@app.post(
    "/branches/select",
    response_model=BranchSelectResponse,
    tags=["branches"],
    summary="分支选择：枚举开关组合，求翻转最少且无冲突的口令启用方案",
    responses={422: {"model": ErrorBody, "description": "请求非法或无安全组合"}},
)
async def branch_select(body: BranchSelectRequest) -> dict:
    # 核心逻辑不依赖 Pydantic，直接喂入 dict 以便独立测试与复用
    return select_branch(
        [item.model_dump() for item in body.commands],
        [item.model_dump() for item in body.switches],
        [item.model_dump() for item in body.conditions],
    )


@app.post(
    "/assets/preload",
    response_model=PreloadResponse,
    tags=["assets"],
    summary="素材预载：单台串行加载器上求按时加载条数最多的加载方案",
    responses={422: {"model": ErrorBody, "description": "请求非法或素材关联非法"}},
)
async def assets_preload(body: PreloadRequest) -> dict:
    # 核心逻辑不依赖 Pydantic，直接喂入 dict 以便独立测试与复用
    return plan_preload(
        [item.model_dump() for item in body.commands],
        [item.model_dump() for item in body.assets],
    )


@app.post(
    "/replays/plan",
    response_model=ReplayPlanResponse,
    tags=["replays"],
    summary="回放规划：预算内挑选候选口令，求优先级总和最大的录制集合",
    responses={422: {"model": ErrorBody, "description": "请求非法或候选口令不存在"}},
)
async def replay_plan(body: ReplayPlanRequest) -> dict:
    # 核心逻辑不依赖 Pydantic，直接喂入 dict 以便独立测试与复用
    return plan_replays(
        [item.model_dump() for item in body.commands],
        [item.model_dump() for item in body.candidates],
        lead_in_ms=body.lead_in_ms,
        lead_out_ms=body.lead_out_ms,
        budget_ms=body.budget_ms,
    )
