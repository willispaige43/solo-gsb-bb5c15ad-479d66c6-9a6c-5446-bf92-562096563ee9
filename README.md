# 直播导播口令编排 API

基于 **Python + FastAPI** 的导播口令编排服务：接收一组带锚点依赖的直播导播口令，
解析每条口令在时间轴上的起止毫秒，并列出同一通道内半开区间重叠的冲突口令对；
直播进行中还可对节目单做延误分摊，在不移动固定开场时刻的前提下把延误按各段
可压缩余量比例分配给未播段；直播结束后可将计划口令与实播记录按通道保序对账，
以最小的起止偏差与漏播、额外播出罚分还原实际播出情况；还可按实播记录逐通道
估计一个整数时钟偏移，以双端容差划分一致与异常记录，定位采集时钟的整体漂移；
还可在不改变任何起止时刻的前提下，把口令全局改派到按 id 指定的备用通道，
求改派口令最少、并列时通道名序列字典序最小的无冲突方案；亦可对
base / left / right 三份口令版本按 id 和字段做无状态三方合并，无分歧时直接
复用编排器校验合并结果；还可在给定起点窗口内为指定通道规划一条临时插播，
求需要撤销的现有口令最少、并列时起点最早的插播方案；还可在目标通道
`[0, horizon_end_ms)` 的空档内联合分配至多 12 条填充素材，求填充总时长最大、
并列时优先级总和最大、再并列时素材 id 序列字典序最小的排布；还可在至多 8 个
布尔开关张成的组合空间中选择启用的口令子集：按口令 id 的开关取值条件筛选，
排除锚点未启用与同通道冲突的组合，求相对当前取值翻转开关数最少、并列时取值
序列（false 小于 true）字典序最小的安全组合；还可为至多 12 条预载素材安排
单台串行加载器：素材只能在就绪时刻之后开始、且必须在所关联口令的起点前完成，
求按时加载条数最多、并列优先级总和最大、再并列入选 id 升序列表字典序最小，
同一入选集合取最早完工、并列加载 id 顺序字典序最小的加载方案；还可从至多
12 个带非负整数优先级的候选口令中挑选回放录制集合：每个候选生成
`[max(0, 起点-前留白), 终点+后留白)` 的全局录制窗口，跨通道重叠或首尾相接的
窗口合并后才计入回放时长预算，求预算内优先级总和最大、并列入选条数最多、
再并列入选 id 升序列表字典序最小的集合。

- 在线接口文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：`GET /health`
- 编排接口：`POST /compile`
- 改动预览接口：`POST /preview`
- 节目单延误分摊接口：`POST /schedule/rebalance`
- 实播对账接口：`POST /runs/reconcile`
- 时钟校准接口：`POST /runs/calibrate`
- 通道改派接口：`POST /routing/assign`
- 口令三方合并接口：`POST /versions/merge`
- 临时插播规划接口：`POST /interruptions/plan`
- 空档填充规划接口：`POST /fillers/plan`
- 开关分支选择接口：`POST /branches/select`
- 素材预载接口：`POST /assets/preload`
- 回放窗口预算规划接口：`POST /replays/plan`

## 一条命令启动

需要已安装 Docker（含 Compose 插件）。在本目录执行：

```bash
docker compose up --build -d
```

启动后访问 <http://localhost:8000/docs> 即可在线调试。也可以用 curl 验证：

```bash
curl -s http://localhost:8000/health
# {"status":"ok"}
```

停止服务：

```bash
docker compose down
```

> 镜像在构建阶段执行 `pip install -r requirements.txt`，**启动容器时依赖已安装完毕**，
> 无需进入容器手动安装。

## 必要配置

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| 容器监听端口 | 固定 `8000`（由 `uvicorn --port 8000` 指定） | `8000` |
| 宿主机映射端口 | Compose 读取环境变量 `PUBLISH_PORT`，映射为 `${PUBLISH_PORT}:8000` | `8000` |
| 对账超时 | `RECONCILE_TIMEOUT_SECONDS`：单个对账任务（含排队）的最长秒数，超时返回 `504` | `10` |
| 依赖安装 | `Dockerfile` 构建时执行 `pip install -r requirements.txt` | 自动 |

如需改用宿主机 9000 端口：

```bash
PUBLISH_PORT=9000 docker compose up --build -d
```

如需调整对账超时（例如 30 秒）：

```bash
RECONCILE_TIMEOUT_SECONDS=30 docker compose up --build -d
```

服务无数据库、无外部依赖；除上述可选环境变量外不需要额外配置。

## 接口约定：`POST /compile`

请求体为口令数组，每一项字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 口令唯一 id；不能为空，不能使用保留锚点名 `START` |
| `channel` | string | 是 | 通道标识；只有同通道口令之间才检测冲突 |
| `anchor` | string | 否 | 锚点，缺省 `START`；可填 `START`（时间原点 0 ms）或另一条口令的 id |
| `offset_ms` | integer | 否 | 相对 **锚点结束时刻** 的偏移（毫秒），缺省 `0`，允许为负 |
| `duration_ms` | integer | 是 | 持续时长（毫秒），必须为正数 |

时间与冲突规则：

- 虚拟锚点 `START` 的起点和结束时刻均为 `0`。
- 口令 `x` 的起点 = 锚点口令的结束时刻 + `offset_ms`，结束时刻 = 起点 + `duration_ms`。
- 允许负偏移，但解析出的起点不得小于 `0`，否则返回 `NEGATIVE_START`。
- `timeline` 按 `(start_ms, id)` 升序排列。
- 同一通道内两个半开区间 `[start_ms, end_ms)` 发生重叠即为冲突；
  **首尾相接（前一个 end 等于后一个 start）不算冲突**；不同通道互不影响。
- 冲突对对内按 id 排序（`a < b`），所有冲突对之间按 `(a, b, channel)` 排序。

### 请求示例

```bash
curl -s http://localhost:8000/compile \
  -H 'Content-Type: application/json' \
  -d '[
    {"id": "a", "channel": "main", "anchor": "START", "offset_ms": 0,    "duration_ms": 1000},
    {"id": "b", "channel": "main", "anchor": "a",     "offset_ms": 0,    "duration_ms": 500},
    {"id": "c", "channel": "main", "anchor": "a",     "offset_ms": -200, "duration_ms": 500},
    {"id": "d", "channel": "sub",  "anchor": "START", "offset_ms": 0,    "duration_ms": 1000}
  ]'
```

对应时间：

- `a`：`[0, 1000)`
- `b`：相对 `a` 结束时刻（1000）+ 0 → `[1000, 1500)`，与 `a` 首尾相接，不冲突
- `c`：相对 `a` 结束时刻 -200 → `[800, 1300)`，与 `a`、`b` 均在 `main` 通道重叠
- `d`：`[0, 1000)`，但属于 `sub` 通道，不参与冲突

响应示例（`200`）：

```json
{
  "timeline": [
    {
      "id": "a",
      "channel": "main",
      "anchor": "START",
      "offset_ms": 0,
      "duration_ms": 1000,
      "start_ms": 0,
      "end_ms": 1000
    },
    {
      "id": "d",
      "channel": "sub",
      "anchor": "START",
      "offset_ms": 0,
      "duration_ms": 1000,
      "start_ms": 0,
      "end_ms": 1000
    },
    {
      "id": "c",
      "channel": "main",
      "anchor": "a",
      "offset_ms": -200,
      "duration_ms": 500,
      "start_ms": 800,
      "end_ms": 1300
    },
    {
      "id": "b",
      "channel": "main",
      "anchor": "a",
      "offset_ms": 0,
      "duration_ms": 500,
      "start_ms": 1000,
      "end_ms": 1500
    }
  ],
  "conflicts": [
    {
      "channel": "main",
      "a": "a",
      "b": "c",
      "a_interval": [0, 1000],
      "b_interval": [800, 1300]
    },
    {
      "channel": "main",
      "a": "b",
      "b": "c",
      "a_interval": [1000, 1500],
      "b_interval": [800, 1300]
    }
  ]
}
```

## 接口约定：`POST /preview`

在不落地任何数据的前提下，预览「修改一条现有口令的 `offset_ms` / `duration_ms`」
对整条编排的影响。服务端会分别编译**原编排**和**改动后编排**，再对比差异。

请求体为一个对象：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `commands` | array | 是 | 口令数组，每一项的字段与 `POST /compile` 的请求体完全一致 |
| `change` | object | 是 | 一项改动，见下表 |

`change` 字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 要改动的现有口令 id（必须存在于 `commands` 中） |
| `offset_ms` | integer | 二选一 | 新的偏移；不填则保留原值 |
| `duration_ms` | integer | 二选一 | 新的持续时长（仍须为正数）；不填则保留原值 |

`offset_ms` 与 `duration_ms` **至少提供一个**；未提供的字段以及其他字段
（`channel`、`anchor` 等）一律保留原值，原请求数据不会被修改。原编排与改动后编排
均沿用与 `/compile` 相同的字段校验与 `422` 错误格式。

响应字段：

- `changed_ids`：时间起止（`start_ms` / `end_ms`）发生变化的口令 id，
  **包含锚点依赖的传导**（被改动口令的下游口令会一起移动），按 id 升序。
- `added_conflicts`：改动后**新增**的同通道冲突对，区间为改动后版本。
- `removed_conflicts`：改动后**消失**的同通道冲突对，区间为原版本。

冲突对以 `(channel, a, b)` 判定身份（同一条冲突对若只是区间变化，不算新增也不算消失）；
每个冲突对仍携带 `a_interval` / `b_interval`，两个列表均按 `(a, b, channel)` 排序。

### 请求示例

```bash
curl -s http://localhost:8000/preview \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [
      {"id": "a", "channel": "main", "anchor": "START", "offset_ms": 0,    "duration_ms": 1000},
      {"id": "b", "channel": "main", "anchor": "a",     "offset_ms": 0,    "duration_ms": 500},
      {"id": "c", "channel": "main", "anchor": "a",     "offset_ms": -200, "duration_ms": 500},
      {"id": "d", "channel": "sub",  "anchor": "START", "offset_ms": 0,    "duration_ms": 1000}
    ],
    "change": {"id": "a", "offset_ms": 100}
  }'
```

改动把 `a` 的起点推迟到 100 ms：

- `a`：`[0, 1000)` → `[100, 1100)`。
- `b` 锚定 `a` 的结束时刻：`[1000, 1500)` → `[1100, 1600)`（依赖传导）。
- `c` 锚定 `a` 的结束时刻：`[800, 1300)` → `[900, 1400)`（依赖传导）。
- `d` 不依赖 `a`，时间不变。

原有的两个冲突对 `(main, a, c)`、`(main, b, c)` 改动后仍然重叠，身份不变。
响应示例（`200`）：

```json
{
  "changed_ids": ["a", "b", "c"],
  "added_conflicts": [],
  "removed_conflicts": []
}
```

下面这个改动把 `c` 的持续时长缩短为 100 ms：

```bash
curl -s http://localhost:8000/preview \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [
      {"id": "a", "channel": "main", "anchor": "START", "offset_ms": 0,    "duration_ms": 1000},
      {"id": "b", "channel": "main", "anchor": "a",     "offset_ms": 0,    "duration_ms": 500},
      {"id": "c", "channel": "main", "anchor": "a",     "offset_ms": -200, "duration_ms": 500},
      {"id": "d", "channel": "sub",  "anchor": "START", "offset_ms": 0,    "duration_ms": 1000}
    ],
    "change": {"id": "c", "duration_ms": 100}
  }'
```

`c` 由 `[800, 1300)` 缩短为 `[800, 900)`：与 `a`（`[0, 1000)`）仍重叠，
但与 `b`（`[1000, 1500)`）不再重叠，冲突对 `(b, c)` 消失，区间携带原版本：

```json
{
  "changed_ids": ["c"],
  "added_conflicts": [],
  "removed_conflicts": [
    {
      "channel": "main",
      "a": "b",
      "b": "c",
      "a_interval": [1000, 1500],
      "b_interval": [800, 1300]
    }
  ]
}
```

把 `c` 反过来延长（例如 `duration_ms: 800` → `[800, 1600)`）则不会产生新冲突对；
而把原本首尾相接的口令延长到越过对方起点时，新冲突会出现在 `added_conflicts` 中，
区间携带改动后的版本。

## 接口约定：`POST /schedule/rebalance`

直播进行中，已结束段的实际结束时刻往往偏离计划，节目单整体随之延误或提前。
本接口在**不移动任何固定开场时刻**的前提下，把延误按各段**可压缩余量**
（计划时长 − 最短时长）的比例分摊给固定点之前的未播段，返回调整后的各段时长。

请求体为一个对象：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `segments` | array | 是 | 按播出顺序排列的节目段，允许为空数组 |

`segments` 每一项字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 节目段唯一 id；不能为空 |
| `planned_duration_sec` | integer | 是 | 计划时长（秒），正数且不短于 `min_duration_sec` |
| `min_duration_sec` | integer | 是 | 最短时长（秒），正数；延误分摊时的压缩下限 |
| `fixed_start_at_sec` | integer | 否 | 固定开场时刻（秒，相对节目单原点 0）；不可移动，缺省表示非固定点 |
| `actual_end_at_sec` | integer | 否 | 已结束段的实际结束时刻（秒）；缺省表示该段未播 |

分摊规则：

- **固定点切窗**：带 `fixed_start_at_sec` 的段是固定点，它把节目单切成若干窗口，
  每个窗口内的段必须在窗口末尾的固定点时刻之前播完；最后一个固定点之后的段
  没有窗口约束，保持计划时长。
- **可用时间**：窗口内已结束段的最后一个 `actual_end_at_sec` 决定未播段的可用时间
  （可用时间 = 固定点时刻 − 该实际结束时刻；窗口内无已结束段时从窗口起点算起）。
- **延误分摊**：窗口内未播段计划总时长超出可用时间的部分即延误，按各段可压缩余量
  比例分摊压缩；整数秒余数按**小数部分从大到小**分配，**同分按节目顺序**（靠前者先得）；
  任何段不会被压到 `min_duration_sec` 以下（余量为 0 的段不参与分摊）。
- **提前不拉伸**：可用时间有富余时不拉长任何段，富余秒数作为该固定点的
  `remaining_sec` 返回（可追回时间）。
- **已结束段**必须是节目单的连续前缀，其实际结束时刻单调不减；已结束段不参与压缩，
  响应中保持计划时长。
- **余量不足**：延误超过窗口内可压缩余量总和时，返回 `422` +
  `INSUFFICIENT_MARGIN`，`details.unrecoverable_sec` 为无法追回的秒数。

响应字段：

- `segments`：按节目顺序排列，每项含 `id`、`planned_duration_sec`、
  `adjusted_duration_sec`（分摊后时长）、`compressed_sec`（被压缩的秒数）、
  `finished`（是否已结束）。
- `fixed_points`：每个「前面有窗口」的固定点一项，含 `segment_id`、
  `fixed_start_at_sec`、`absorbed_sec`（窗口内被压缩吸收的延误秒数）、
  `remaining_sec`（固定点前剩余秒数，发生压缩时为 0）。

### 请求示例（延误按比例分摊）

`opening` 计划 600s 但实际 660s 才结束（晚 60s），`news` 必须在 1800s 准点开场：

```bash
curl -s http://localhost:8000/schedule/rebalance \
  -H 'Content-Type: application/json' \
  -d '{
    "segments": [
      {"id": "opening",   "planned_duration_sec": 600, "min_duration_sec": 300, "actual_end_at_sec": 660},
      {"id": "interview", "planned_duration_sec": 900, "min_duration_sec": 600},
      {"id": "music",     "planned_duration_sec": 600, "min_duration_sec": 300},
      {"id": "news",      "planned_duration_sec": 300, "min_duration_sec": 300, "fixed_start_at_sec": 1800}
    ]
  }'
```

可用时间 = 1800 − 660 = 1140s，计划还需 900 + 600 = 1500s，延误 360s；
`interview` 与 `music` 余量均为 300s，各摊 180s。响应示例（`200`）：

```json
{
  "segments": [
    {"id": "opening",   "planned_duration_sec": 600, "adjusted_duration_sec": 600, "compressed_sec": 0,   "finished": true},
    {"id": "interview", "planned_duration_sec": 900, "adjusted_duration_sec": 720, "compressed_sec": 180, "finished": false},
    {"id": "music",     "planned_duration_sec": 600, "adjusted_duration_sec": 420, "compressed_sec": 180, "finished": false},
    {"id": "news",      "planned_duration_sec": 300, "adjusted_duration_sec": 300, "compressed_sec": 0,   "finished": false}
  ],
  "fixed_points": [
    {"segment_id": "news", "fixed_start_at_sec": 1800, "absorbed_sec": 360, "remaining_sec": 0}
  ]
}
```

### 请求示例（整数秒余数分配）

延误 7s、三段余量各 3s：精确分摊值均为 `7 × 3 / 9 ≈ 2.33`，每段先分 2s，
余 1s；小数部分相同，按节目顺序补给最靠前的 `a`（`a` 压 3s，`b`、`c` 各压 2s）：

```bash
curl -s http://localhost:8000/schedule/rebalance \
  -H 'Content-Type: application/json' \
  -d '{
    "segments": [
      {"id": "a",    "planned_duration_sec": 103, "min_duration_sec": 100},
      {"id": "b",    "planned_duration_sec": 103, "min_duration_sec": 100},
      {"id": "c",    "planned_duration_sec": 103, "min_duration_sec": 100},
      {"id": "news", "planned_duration_sec": 50,  "min_duration_sec": 50, "fixed_start_at_sec": 302}
    ]
  }'
```

```json
{
  "segments": [
    {"id": "a",    "planned_duration_sec": 103, "adjusted_duration_sec": 100, "compressed_sec": 3, "finished": false},
    {"id": "b",    "planned_duration_sec": 103, "adjusted_duration_sec": 101, "compressed_sec": 2, "finished": false},
    {"id": "c",    "planned_duration_sec": 103, "adjusted_duration_sec": 101, "compressed_sec": 2, "finished": false},
    {"id": "news", "planned_duration_sec": 50,  "adjusted_duration_sec": 50,  "compressed_sec": 0, "finished": false}
  ],
  "fixed_points": [
    {"segment_id": "news", "fixed_start_at_sec": 302, "absorbed_sec": 7, "remaining_sec": 0}
  ]
}
```

### 请求示例（余量不足，422）

`opening` 拖到 1700s 才结束，距离 1800s 的 `news` 只剩 100s，而 `interview`
计划 500s、最多只能压缩 50s：

```bash
curl -s http://localhost:8000/schedule/rebalance \
  -H 'Content-Type: application/json' \
  -d '{
    "segments": [
      {"id": "opening",   "planned_duration_sec": 600, "min_duration_sec": 600, "actual_end_at_sec": 1700},
      {"id": "interview", "planned_duration_sec": 500, "min_duration_sec": 450},
      {"id": "news",      "planned_duration_sec": 300, "min_duration_sec": 300, "fixed_start_at_sec": 1800}
    ]
  }'
```

响应（`422`）：延误 400s，余量只有 50s，仍有 350s 无法追回：

```json
{
  "code": "INSUFFICIENT_MARGIN",
  "message": "固定点 news（1800s）前可压缩余量不足，仍有 350 秒无法追回",
  "details": {
    "unrecoverable_sec": 350,
    "required_compression_sec": 400,
    "available_margin_sec": 50,
    "fixed_point": {"segment_id": "news", "fixed_start_at_sec": 1800}
  }
}
```

## 接口约定：`POST /runs/reconcile`

直播结束（或进行中复盘）时，把**计划口令**与**实播记录**对账：在每个通道内做
保持顺序的一对一匹配，使「匹配对起止偏差绝对值之和 + 未匹配罚分」最小。

请求体为一个对象：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `commands` | array | 是 | 计划口令数组，每一项的字段与 `POST /compile` 的请求体完全一致 |
| `records` | array | 是 | 实播记录数组，见下表 |
| `tolerance_ms` | integer | 是 | 匹配容差（毫秒），非负；起点偏差与终点偏差均不超过它才允许匹配 |
| `miss_penalty` | integer | 是 | 漏播罚分，非负；每条未匹配的计划口令计一次 |
| `extra_penalty` | integer | 是 | 额外播出罚分，非负；每条未匹配的实播记录计一次 |

`records` 每一项字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 实播记录唯一 id；不能为空，各记录之间不可重复 |
| `channel` | string | 是 | 通道标识；只有同通道的计划与实播才可匹配 |
| `actual_start_ms` | integer | 是 | 实际起点（毫秒），不得为负 |
| `actual_end_ms` | integer | 是 | 实际结束时刻（毫秒），必须大于 `actual_start_ms` |

匹配规则：

- 计划口令先按与 `/compile` 相同的锚点解析求出计划起止毫秒；随后每个通道内，
  计划项与实播记录**分别按 `(起点, id)` 升序排列**。
- 通道内做**保持顺序的一对一匹配**（类似序列比对：计划第 i 项与实播第 j 项匹配后，
  后续匹配的下标只能更大），不同通道互不影响。
- 仅当同一通道、且 `|起点偏差|` 与 `|终点偏差|` 都不超过 `tolerance_ms` 时
  才允许匹配；该匹配对的代价为两个偏差绝对值之和。
- **总罚分** = 各匹配对的起止偏差绝对值之和 + `miss_penalty` × 漏播计划数
  + `extra_penalty` × 额外实播数，取总罚分最小的匹配。
- **同分裁决**：总罚分相同时，依次取 ① 匹配对数更多者；
  ② 匹配 id 对 `(plan_id, actual_id)` 序列（按通道内匹配顺序）字典序更小者。

响应字段：

- `matches`：匹配对列表，按 `(plan_id, actual_id)` 升序；每项含 `plan_id`、
  `actual_id`、`channel`、`planned_interval`、`actual_interval`、
  `start_deviation_ms`、`end_deviation_ms`。偏差为带符号值（**实际 − 计划**），
  总罚分按偏差绝对值计算。
- `missed_plan_ids`：未匹配的计划口令 id（漏播），按 id 升序。
- `extra_actual_ids`：未匹配的实播记录 id（额外播出），按 id 升序。
- `total_penalty`：总罚分。

执行模型（并发与超时）：

- 对账计算在**应用生命周期管理的单工作进程**中执行：应用启动时创建、关闭时
  回收；事件循环只负责等待结果，长对账期间 `GET /health` 等接口照常响应。
- 同一时刻最多容纳 **2 个未结束任务**（执行中 + 排队）；满额时新请求**立即**
  返回 `503` + `RECONCILE_BUSY`，不提交计算。
- 任务被接纳即按 `RECONCILE_TIMEOUT_SECONDS`（默认 `10` 秒）计时，
  **排队时间也计入**；到期返回 `504` + `RECONCILE_TIMEOUT`。
- 超时或请求取消时：仍在排队的任务直接撤销并立即释放名额；已在执行的任务
  等实际结束才释放名额，迟到的结果直接丢弃。

`503` 响应示例：

```json
{
  "code": "RECONCILE_BUSY",
  "message": "对账任务已满：最多同时容纳 2 个未结束任务（执行中 + 排队），请稍后重试",
  "details": { "max_inflight": 2 }
}
```

`504` 响应示例：

```json
{
  "code": "RECONCILE_TIMEOUT",
  "message": "对账未在 10 秒内完成（含排队时间）",
  "details": { "timeout_seconds": 10.0 }
}
```

### 请求示例（正常对账）

```bash
curl -s http://localhost:8000/runs/reconcile \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [
      {"id": "a", "channel": "main", "anchor": "START", "offset_ms": 0, "duration_ms": 1000},
      {"id": "b", "channel": "main", "anchor": "a",     "offset_ms": 0, "duration_ms": 500},
      {"id": "c", "channel": "sub",  "anchor": "START", "offset_ms": 0, "duration_ms": 800}
    ],
    "records": [
      {"id": "r1", "channel": "main", "actual_start_ms": 10,   "actual_end_ms": 1020},
      {"id": "r2", "channel": "main", "actual_start_ms": 1010, "actual_end_ms": 1490},
      {"id": "r3", "channel": "sub",  "actual_start_ms": 0,    "actual_end_ms": 900}
    ],
    "tolerance_ms": 50,
    "miss_penalty": 200,
    "extra_penalty": 300
  }'
```

- 计划：`a` 为 `[0, 1000)`，`b` 为 `[1000, 1500)`，`c` 为 `[0, 800)`。
- `main` 通道：`a ↔ r1` 偏差 10/20（代价 30）、`b ↔ r2` 偏差 10/−10（代价 20）。
- `sub` 通道：`r3` 终点偏差 100 超过容差 50，不可匹配 → `c` 漏播（200）、
  `r3` 额外播出（300）。
- 总罚分 = 30 + 20 + 200 + 300 = **550**。响应示例（`200`）：

```json
{
  "matches": [
    {
      "plan_id": "a",
      "actual_id": "r1",
      "channel": "main",
      "planned_interval": [0, 1000],
      "actual_interval": [10, 1020],
      "start_deviation_ms": 10,
      "end_deviation_ms": 20
    },
    {
      "plan_id": "b",
      "actual_id": "r2",
      "channel": "main",
      "planned_interval": [1000, 1500],
      "actual_interval": [1010, 1490],
      "start_deviation_ms": 10,
      "end_deviation_ms": -10
    }
  ],
  "missed_plan_ids": ["c"],
  "extra_actual_ids": ["r3"],
  "total_penalty": 550
}
```

### 请求示例（非法实播区间，422）

实播记录起点为负、或结束时刻不大于起点时返回 `422` + `INVALID_INTERVAL`：

```bash
curl -s http://localhost:8000/runs/reconcile \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [],
    "records": [
      {"id": "r1", "channel": "main", "actual_start_ms": 900, "actual_end_ms": 500}
    ],
    "tolerance_ms": 50,
    "miss_penalty": 200,
    "extra_penalty": 300
  }'
```

```json
{
  "code": "INVALID_INTERVAL",
  "message": "实播区间非法：actual_start_ms 不得为负，且 actual_end_ms 必须大于 actual_start_ms",
  "details": { "ids": ["r1"] }
}
```

## 接口约定：`POST /runs/calibrate`

直播结束后，若怀疑某条通道的采集时钟相对计划时间轴存在整体漂移，可用本接口
**逐通道估计一个整数时钟偏移**（毫秒）。实播记录与计划口令按 **id 一一对应**：
记录 `id` 必须出现在计划口令中，且 `channel` 必须与该口令一致，否则返回 `422`。
计划起止时刻复用 `POST /compile` 的编排器解析，实播记录的字段校验与
`POST /runs/reconcile` 完全相同。

请求体为一个对象：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `commands` | array | 是 | 计划口令数组，每一项的字段与 `POST /compile` 的请求体完全一致 |
| `records` | array | 是 | 实播记录数组，字段与 `POST /runs/reconcile` 的 `records` 一致；`id` 必须出现在计划口令中且通道一致 |
| `tolerance_ms` | integer | 是 | 双端容差（毫秒），非负 |

校准规则：

- 对通道内每条记录，把计划起点、终点分别加上候选偏移 `o` 后与实播起止比较：
  仅当 `|actual_start_ms − (start_ms + o)|` 与 `|actual_end_ms − (end_ms + o)|`
  **都不超过** `tolerance_ms`（双端容差，含等号）时，该记录对偏移 `o` 计为一致。
  等价地，每条记录对应一个闭区间形式的候选整数偏移集合。
- 每个通道独立选择 **一致记录数最多** 的整数偏移；并列时先取 **绝对值最小** 者，
  再取 **数值较小** 者。按事件点扫描求解，不逐毫秒枚举。
- 按选定偏移，通道内记录被划分为 `consistent_ids`（一致）与 `anomalous_ids`
  （异常），均按 id 升序。
- 某通道 **所有记录** 都无法被任何整数偏移满足双端容差时（通常意味着实播时长
  与计划时长之差超过 `2 × tolerance_ms`），该通道标为 `uncalibratable`，
  `offset_ms` 为 `null`，全部记录计入 `anomalous_ids`。
- 只有实播记录出现过的通道才会返回；请求无状态、不落数据，服务端不会修改
  `commands` 与 `records`。

响应字段：

- `channels`：各通道校准结果，按通道名升序；每项含：
  - `channel`：通道标识。
  - `status`：`calibrated`（已估计出偏移）或 `uncalibratable`（无法校准）。
  - `offset_ms`：估计出的整数时钟偏移（毫秒）；无法校准时为 `null`。
  - `consistent_ids` / `anomalous_ids`：按估计偏移划分的一致 / 异常记录 id，
    均按 id 升序。

### 请求示例（逐通道校准）

`main` 通道三条口令整体漂移约 +30ms（`d` 例外），`sub` 通道的 `c` 实播时长
比计划长 100ms，双端容差 10ms 下没有任何整数偏移能同时满足两端：

```bash
curl -s http://localhost:8000/runs/calibrate \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [
      {"id": "a", "channel": "main", "anchor": "START", "offset_ms": 0, "duration_ms": 1000},
      {"id": "b", "channel": "main", "anchor": "a",     "offset_ms": 0, "duration_ms": 500},
      {"id": "d", "channel": "main", "anchor": "b",     "offset_ms": 0, "duration_ms": 500},
      {"id": "c", "channel": "sub",  "anchor": "START", "offset_ms": 0, "duration_ms": 800}
    ],
    "records": [
      {"id": "a", "channel": "main", "actual_start_ms": 30,   "actual_end_ms": 1030},
      {"id": "b", "channel": "main", "actual_start_ms": 1040, "actual_end_ms": 1540},
      {"id": "d", "channel": "main", "actual_start_ms": 1500, "actual_end_ms": 2000},
      {"id": "c", "channel": "sub",  "actual_start_ms": 0,    "actual_end_ms": 900}
    ],
    "tolerance_ms": 10
  }'
```

- `a` 的起点 / 终点偏差为 30 / 30，候选偏移 `[20, 40]`；`b` 偏差 40 / 40，
  候选 `[30, 50]`；`d` 偏差 0 / 0，候选 `[-10, 10]`。
- `main` 通道一致数最多的偏移落在 `[30, 40]`（`a`、`b` 一致），按绝对值最小
  取 **30**；`d` 在该偏移下不满足双端容差，计为异常。
- `sub` 通道 `c` 的起点偏差 0、终点偏差 100，没有任何整数偏移能让两端同时
  落入 ±10ms，标为 `uncalibratable`。

响应示例（`200`）：

```json
{
  "channels": [
    {
      "channel": "main",
      "status": "calibrated",
      "offset_ms": 30,
      "consistent_ids": ["a", "b"],
      "anomalous_ids": ["d"]
    },
    {
      "channel": "sub",
      "status": "uncalibratable",
      "offset_ms": null,
      "consistent_ids": [],
      "anomalous_ids": ["c"]
    }
  ]
}
```

### 请求示例（记录 id 未出现在计划中，422）

```bash
curl -s http://localhost:8000/runs/calibrate \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [
      {"id": "a", "channel": "main", "anchor": "START", "offset_ms": 0, "duration_ms": 1000}
    ],
    "records": [
      {"id": "x", "channel": "main", "actual_start_ms": 0, "actual_end_ms": 1000}
    ],
    "tolerance_ms": 10
  }'
```

```json
{
  "code": "CALIBRATION_TARGET_NOT_FOUND",
  "message": "实播记录 id 未出现在计划口令中: x",
  "details": { "ids": ["x"] }
}
```

记录 `id` 在计划中但 `channel` 与该口令不一致时，返回 `422` +
`CHANNEL_MISMATCH`，`details.mismatches` 给出每条不一致记录的计划通道与
实播通道。

## 接口约定：`POST /routing/assign`

在**不改变任何口令起止时刻**的前提下，把口令全局改派到按 id 指定的备用通道，
消除同通道的时间重叠冲突。请求复用 `POST /compile` 的口令数组（先用同一套
编排器解析锚点、校验时间），另附按口令 id 声明的备用通道列表。

请求体为一个对象：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `commands` | array | 是 | 口令数组，每一项的字段与 `POST /compile` 的请求体完全一致 |
| `backups` | array | 是 | 按口令 id 声明的备用通道列表，**至多 12 项** |

`backups` 每一项字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 口令 id，必须存在于 `commands` 中且不能为空；同一 id 只能声明一次 |
| `backup_channels` | array&lt;string&gt; | 是 | 备用通道名列表，至少 1 个；不能为空字符串、不能重复，也不应再列入原通道 |

改派规则：

- **只换通道、不动时间**：改派后每条口令的 `start_ms` / `end_ms`、`anchor`、
  `offset_ms`、`duration_ms` 均保持 `/compile` 的解析结果不变，只有 `channel`
  可能变化；`timeline` 的排序仍按 `(start_ms, id)`。
- **未列出的口令只能留在原通道**；已列出的口令可落到**原通道或任一备用通道**
  （原通道始终可选，无需也不应写进 `backup_channels`）。最多 **12** 条口令
  可以拥有备用通道。
- **半开区间重叠的口令不得分到同一通道**：`[start_ms, end_ms)` 与
  `[start_ms', end_ms')` 重叠（`start < end'` 且 `start' < end`）即冲突；
  **首尾相接（前一个 end 等于后一个 start）可以共用同一通道**；不同通道互不影响。
- **全局最优**：在所有无冲突方案中，先取**改派口令数最少**（通道与原通道不同
  的口令数）的方案；改派数并列时，取「口令按 **id 升序**排列后对应通道名序列」
  **字典序最小**的方案。
- 响应 `changed_ids` 为实际改派通道的口令 id（新通道与原通道不同），按 id 升序。
- 不存在任何无冲突方案时返回 `422` + `NO_FEASIBLE_ROUTING`，`details.reason`
  区分原因：`FIXED_OVERLAP`（不可改派口令自身重叠）、`NO_AVAILABLE_CHANNEL`
  （某口令的原通道与全部备用通道都被占用）、`INSUFFICIENT_CHANNEL_CAPACITY`
  （可移动口令之间无法安排）。

### 请求示例（最少改派 + 字典序裁决）

`a`、`b` 在 `main` 通道时间重叠，二者都有备用通道 `alt`：

```bash
curl -s http://localhost:8000/routing/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [
      {"id": "a", "channel": "main", "anchor": "START", "offset_ms": 0,   "duration_ms": 100},
      {"id": "b", "channel": "main", "anchor": "START", "offset_ms": 50,  "duration_ms": 100}
    ],
    "backups": [
      {"id": "a", "backup_channels": ["alt"]},
      {"id": "b", "backup_channels": ["alt"]}
    ]
  }'
```

最少只需改派 1 条。两个 1-move 方案按 id 排序的通道名序列分别为
`[alt, main]`（a 改派）与 `[main, alt]`（b 改派），通道名直接按字典序比较，
`[alt, main]` 更小，故改派 `a`。响应示例（`200`）：

```json
{
  "timeline": [
    {
      "id": "a",
      "channel": "alt",
      "anchor": "START",
      "offset_ms": 0,
      "duration_ms": 100,
      "start_ms": 0,
      "end_ms": 100
    },
    {
      "id": "b",
      "channel": "main",
      "anchor": "START",
      "offset_ms": 50,
      "duration_ms": 100,
      "start_ms": 50,
      "end_ms": 150
    }
  ],
  "changed_ids": ["a"]
}
```

### 请求示例（未列出的口令不能动）

`f` 固定在 `main` 且与 `a` 时间重叠，`a` 的原通道与备用通道 `blocked` 都被
占用，只有 `free` 空闲：

```bash
curl -s http://localhost:8000/routing/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [
      {"id": "f", "channel": "main",    "anchor": "START", "offset_ms": 0,  "duration_ms": 100},
      {"id": "g", "channel": "blocked", "anchor": "START", "offset_ms": 0,  "duration_ms": 100},
      {"id": "a", "channel": "main",    "anchor": "START", "offset_ms": 50, "duration_ms": 100}
    ],
    "backups": [
      {"id": "a", "backup_channels": ["blocked", "free"]}
    ]
  }'
```

```json
{
  "timeline": [
    {"id": "f", "channel": "main",    "anchor": "START", "offset_ms": 0,  "duration_ms": 100, "start_ms": 0,   "end_ms": 100},
    {"id": "a", "channel": "free",    "anchor": "START", "offset_ms": 50, "duration_ms": 100, "start_ms": 50,  "end_ms": 150},
    {"id": "g", "channel": "blocked", "anchor": "START", "offset_ms": 0,  "duration_ms": 100, "start_ms": 0,   "end_ms": 100}
  ],
  "changed_ids": ["a"]
}
```

### 请求示例（无可行方案，422）

三条口令两两重叠，原通道均为 `main`，备用通道只有共享的 `alt`
（2 个通道放不下 3 条两两重叠的口令）：

```bash
curl -s http://localhost:8000/routing/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [
      {"id": "a", "channel": "main", "anchor": "START", "offset_ms": 0,  "duration_ms": 100},
      {"id": "b", "channel": "main", "anchor": "START", "offset_ms": 10, "duration_ms": 100},
      {"id": "c", "channel": "main", "anchor": "START", "offset_ms": 20, "duration_ms": 100}
    ],
    "backups": [
      {"id": "a", "backup_channels": ["alt"]},
      {"id": "b", "backup_channels": ["alt"]},
      {"id": "c", "backup_channels": ["alt"]}
    ]
  }'
```

```json
{
  "code": "NO_FEASIBLE_ROUTING",
  "message": "不存在满足时间重叠约束的无冲突改派方案",
  "details": {
    "reason": "INSUFFICIENT_CHANNEL_CAPACITY"
  }
}
```

## 接口约定：`POST /versions/merge`

对 **base（基线）/ left / right** 三份口令版本做无状态三方合并：先把三份口令各自
补全缺省字段（`anchor` 缺省 `START`、`offset_ms` 缺省 `0`），再按 **id 和字段**
合并。单侧改动直接采纳；双侧改为相同值也采纳；双侧对同一字段改成不同值、以及
一侧删除而另一侧修改时，返回按 `(id, field)` 排序的分歧，**不生成合并结果**。
无分歧时再调用现有编排器校验合并口令（缺失锚点、依赖环、负起点等按原错误码
返回 `422`），成功时返回按 id 排序的合并口令及与 `/compile` 同结构的时间线和冲突对。

请求体为一个对象：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `base` | array | 是 | 基线口令数组，每一项的字段与 `POST /compile` 的请求体完全一致 |
| `left` | array | 是 | 左侧版本口令数组，格式同 `base` |
| `right` | array | 是 | 右侧版本口令数组，格式同 `base` |

合并规则（以 id 标识同一条口令，逐字段比较）：

- **单侧改动直接采纳**：只有一侧相对 base 修改某字段、另一侧未改，取修改侧的值；
  单侧新增的口令直接进入合并结果；单侧删除（另一侧保留且未改动）直接采纳删除。
- **双侧一致采纳**：两侧都未改动则保留 base 值；两侧都删除则该口令删除；
  两侧把同一字段改成**相同值**时采纳该值。
- **不同字段合并到同一口令**：两侧分别修改同一条口令的**不同字段**时，两处改动
  合并进同一条口令。
- **字段级分歧**（HTTP `200`，`status="diverged"`，不返回合并口令）：
  - `MODIFY_MODIFY`：base 中存在的口令，两侧把同一字段改成**不同值**；
  - `ADD_ADD`：两侧各自新增了相同 id 的口令，但某字段值不同；
  - `DELETE_MODIFY`：一侧删除整条口令、另一侧修改了字段——每个被修改字段各记一条。
- 分歧一律按 `(id, field)` 升序返回；每条分歧含 `base_value` / `left_value` /
  `right_value`，删除侧或缺省 base（双侧新增）对应值为 `null`。
- 某一版本内部出现重复 id 时返回 `422` + `DUPLICATE_ID`
  （`details.side` 指明是哪一份：`base` / `left` / `right`）；其余字段级问题
  （空 id、空通道、非正时长）由编排校验按原错误码返回。

成功响应（`status="merged"`）字段：

- `commands`：合并后的口令数组（已补全缺省字段），**按 id 升序**。
- `timeline` / `conflicts`：对合并口令调用编排器的结果，结构与排序规则与
  `POST /compile` 完全一致。

### 请求示例（无分歧：不同字段自动合并）

`left` 把 `a` 改到 `alt` 通道并新增 `b`；`right` 把 `a` 的时长延长到 1500
并删除 `d`。两侧改的是 `a` 的不同字段，自动合并：

```bash
curl -s http://localhost:8000/versions/merge \
  -H 'Content-Type: application/json' \
  -d '{
    "base": [
      {"id": "a", "channel": "main", "anchor": "START", "offset_ms": 0, "duration_ms": 1000},
      {"id": "d", "channel": "sub",  "anchor": "START", "offset_ms": 0, "duration_ms": 1000}
    ],
    "left": [
      {"id": "a", "channel": "alt",  "anchor": "START", "offset_ms": 0, "duration_ms": 1000},
      {"id": "b", "channel": "alt",  "anchor": "a",     "offset_ms": 0, "duration_ms": 500},
      {"id": "d", "channel": "sub",  "anchor": "START", "offset_ms": 0, "duration_ms": 1000}
    ],
    "right": [
      {"id": "a", "channel": "main", "anchor": "START", "offset_ms": 0, "duration_ms": 1500}
    ]
  }'
```

响应示例（`200`）：合并口令按 id 排序为 `a`、`b`；`timeline` 中 `b` 锚定合并后
`a` 的结束时刻 1500：

```json
{
  "status": "merged",
  "commands": [
    {"id": "a", "channel": "alt", "anchor": "START", "offset_ms": 0, "duration_ms": 1500},
    {"id": "b", "channel": "alt", "anchor": "a",     "offset_ms": 0, "duration_ms": 500}
  ],
  "timeline": [
    {"id": "a", "channel": "alt", "anchor": "START", "offset_ms": 0,    "duration_ms": 1500, "start_ms": 0,    "end_ms": 1500},
    {"id": "b", "channel": "alt", "anchor": "a",     "offset_ms": 0,    "duration_ms": 500,  "start_ms": 1500, "end_ms": 2000}
  ],
  "conflicts": []
}
```

### 请求示例（存在分歧，200）

`x` 的通道两侧改成了不同值；`y` 被 left 删除、right 却改了时长；两侧还各自新增了
同 id 的 `z` 但时长不同：

```bash
curl -s http://localhost:8000/versions/merge \
  -H 'Content-Type: application/json' \
  -d '{
    "base": [
      {"id": "x", "channel": "main", "duration_ms": 1000},
      {"id": "y", "channel": "main", "duration_ms": 1000}
    ],
    "left": [
      {"id": "x", "channel": "alt",  "duration_ms": 1000},
      {"id": "z", "channel": "main", "duration_ms": 100}
    ],
    "right": [
      {"id": "x", "channel": "sub",  "duration_ms": 1000},
      {"id": "y", "channel": "main", "duration_ms": 50},
      {"id": "z", "channel": "main", "duration_ms": 200}
    ]
  }'
```

响应示例（`200`，不包含 `commands` / `timeline` / `conflicts`）：

```json
{
  "status": "diverged",
  "divergences": [
    {"id": "x", "field": "channel",     "kind": "MODIFY_MODIFY", "base_value": "main", "left_value": "alt", "right_value": "sub"},
    {"id": "y", "field": "duration_ms", "kind": "DELETE_MODIFY", "base_value": 1000,   "left_value": null,  "right_value": 50},
    {"id": "z", "field": "duration_ms", "kind": "ADD_ADD",       "base_value": null,   "left_value": 100,   "right_value": 200}
  ]
}
```

合并口令本身编排非法时（例如单侧新增的口令锚定了一个谁都没有的 id），与
`/compile` 一样返回 `422` + `MISSING_ANCHOR`；依赖环返回 `DEPENDENCY_CYCLE`、
负起点返回 `NEGATIVE_START`，错误体结构与「错误处理」一节一致。

## 接口约定：`POST /interruptions/plan`

直播进行中需要在某条通道上临时插播一条固定时长的口令时，本接口在给定的整数
毫秒起点窗口 `[earliest_start_ms, latest_start_ms]`（**闭区间，两端可取**）内
挑选插播起点：现有口令先复用 `POST /compile` 的编排器校验并解析起止时刻，再对
每个候选起点计算需要撤销的口令集合，选**撤销条数最少、并列时起点最早**的方案。

请求体为一个对象：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `commands` | array | 是 | 现有口令数组，每一项的字段与 `POST /compile` 的请求体完全一致 |
| `channel` | string | 是 | 插播目标通道；只有同通道口令参与重叠判定，不能为空 |
| `duration_ms` | integer | 是 | 插播持续时长（毫秒），必须为正数 |
| `earliest_start_ms` | integer | 是 | 允许的最早起点（毫秒，闭区间左端，含该点），不得为负 |
| `latest_start_ms` | integer | 是 | 允许的最晚起点（毫秒，闭区间右端，含该点），不得小于 `earliest_start_ms` |
| `cancellable_ids` | array&lt;string&gt; | 是 | 允许撤销的现有口令 id 列表（可为空数组）；id 必须存在于 `commands`，不可重复、不能为空串 |

撤销与可行性规则：

- 插播占用半开区间 `[start_ms, start_ms + duration_ms)`；同通道内与它**半开区间
  重叠**的现有口令必须撤销，**首尾相接不算重叠**，不同通道互不影响。
- 撤销还必须**递归包含所有锚点后继**：直接或间接以被撤口令为 `anchor` 的口令
  一并撤销（跨通道同样递归）。等价地，一条口令被撤，当且仅当它的某条锚点链上
  存在一条同通道、与插播重叠的口令。
- 撤销集合中只要出现一个不在 `cancellable_ids` 内的口令，该起点即**不可行**。
- 在窗口内所有可行起点中，先取撤销条数最少者；条数并列时取**起点最早**者。
- 服务端**不逐毫秒枚举**：撤销集合只在现有口令的起止事件处变化，整体按事件点
  扫描（O(n log n)），即便窗口宽达 2⁵³ 毫秒也即时返回。
- 请求无状态、不落数据，服务端不会修改 `commands`。

响应字段：

- `start_ms`：选定的插播起点（毫秒）。
- `cancelled_ids`：需要撤销的全部口令 id（含锚点后继递归），按 id 升序。
- `timeline`：撤销后**剩余**口令的时间线，结构与排序与 `/compile` 完全一致
  （按 `(start_ms, id)` 升序，不含插播口令本身）。

### 请求示例（撤销含锚点后继传导）

`a` 为 `main` 通道 `[0, 1000)`；`b` 锚定 `a`，为 `main` 通道 `[1000, 1500)`；
`c` 在 `sub` 通道 `[0, 1000)`。插播 200 ms：

- 起点 800：插播 `[800, 1000)` 与 `a` 重叠，撤销闭包为 `{a, b}`（b 是 a 的
  锚点后继），共 2 条。
- 起点 1000：插播 `[1000, 1200)` 与 `a` 首尾相接、只与 `b` 重叠，只撤 1 条，
  为全局最优。

```bash
curl -s http://localhost:8000/interruptions/plan \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [
      {"id": "a", "channel": "main", "anchor": "START", "offset_ms": 0, "duration_ms": 1000},
      {"id": "b", "channel": "main", "anchor": "a",     "offset_ms": 0, "duration_ms": 500},
      {"id": "c", "channel": "sub",  "anchor": "START", "offset_ms": 0, "duration_ms": 1000}
    ],
    "channel": "main",
    "duration_ms": 200,
    "earliest_start_ms": 0,
    "latest_start_ms": 1499,
    "cancellable_ids": ["a", "b"]
  }'
```

响应示例（`200`）：

```json
{
  "start_ms": 1000,
  "cancelled_ids": ["b"],
  "timeline": [
    {
      "id": "a",
      "channel": "main",
      "anchor": "START",
      "offset_ms": 0,
      "duration_ms": 1000,
      "start_ms": 0,
      "end_ms": 1000
    },
    {
      "id": "c",
      "channel": "sub",
      "anchor": "START",
      "offset_ms": 0,
      "duration_ms": 1000,
      "start_ms": 0,
      "end_ms": 1000
    }
  ]
}
```

跨通道的锚点后继同样递归撤销：若还有一条 `x` 在 `sub` 通道锚定 `b`，起点 1000
的 `cancelled_ids` 即为 `["b", "x"]`。

### 请求示例（无撤销起点）

窗口包含一个与所有同通道口令都不重叠的空档时，最早的空档起点胜出，
`cancelled_ids` 为空数组。例如插播 200 ms、窗口起点放到 1500（与 `b` 的结束
时刻首尾相接）：

```json
{
  "start_ms": 1500,
  "cancelled_ids": [],
  "timeline": [
    {"id": "a", "channel": "main", "anchor": "START", "offset_ms": 0, "duration_ms": 1000, "start_ms": 0,    "end_ms": 1000},
    {"id": "c", "channel": "sub",  "anchor": "START", "offset_ms": 0, "duration_ms": 1000, "start_ms": 0,    "end_ms": 1000},
    {"id": "b", "channel": "main", "anchor": "a",     "offset_ms": 0, "duration_ms": 500,  "start_ms": 1000, "end_ms": 1500}
  ]
}
```

### 请求示例（无可行起点，422）

窗口内每个起点的撤销闭包都包含不可撤销口令时返回 `422` +
`NO_FEASIBLE_INTERRUPTION`。下例中 `b` 不在 `cancellable_ids` 内：起点 800 时
它经 `a` 的后继闭包被触及，起点 1000 时与插播直接重叠，窗口 `[0, 1499]` 内
没有可行起点：

```bash
curl -s http://localhost:8000/interruptions/plan \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [
      {"id": "a", "channel": "main", "anchor": "START", "offset_ms": 0, "duration_ms": 1000},
      {"id": "b", "channel": "main", "anchor": "a",     "offset_ms": 0, "duration_ms": 500}
    ],
    "channel": "main",
    "duration_ms": 200,
    "earliest_start_ms": 0,
    "latest_start_ms": 1499,
    "cancellable_ids": ["a"]
  }'
```

```json
{
  "code": "NO_FEASIBLE_INTERRUPTION",
  "message": "通道 main 在闭区间 [0, 1499] 内不存在可行插播起点：撤销闭包中包含不可撤销口令",
  "details": {
    "reason": "UNREVOCABLE_OVERLAP",
    "channel": "main",
    "duration_ms": 200,
    "earliest_start_ms": 0,
    "latest_start_ms": 1499,
    "blocking_ids": ["b"]
  }
}
```

`details.blocking_ids` 给出窗口内任意起点的撤销闭包中可能出现的全部不可撤销
口令 id（按 id 升序），便于调用方定位阻塞来源。若把窗口右端放宽到 1500，
则起点 1500 与所有口令首尾相接，`cancelled_ids` 为空即可成功。

## 接口约定：`POST /fillers/plan`

在节目空档中自动排入填充素材（备播片、垫片等）：现有口令先复用 `POST /compile`
的编排器校验并解析起止时刻，取**目标通道**口令的占用区间裁剪到
`[0, horizon_end_ms)` 并求并集，其补集即为该通道的**半开空档**（至多 8 个）；
随后把至多 12 条素材在全部空档间**联合分配**（每条素材最多使用一次），
求填充效果最好的排布。

请求体为一个对象：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `commands` | array | 是 | 现有口令数组，每一项的字段与 `POST /compile` 的请求体完全一致 |
| `channel` | string | 是 | 目标通道标识；只有同通道口令参与占用计算，不能为空 |
| `horizon_end_ms` | integer | 是 | 规划视界结束时刻（毫秒），必须为正数；规划范围为半开区间 `[0, horizon_end_ms)` |
| `fillers` | array | 是 | 填充素材列表，**至多 12 条** |

`fillers` 每一项字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 素材唯一 id；不能为空，各素材之间不可重复 |
| `duration_ms` | integer | 是 | 素材时长（毫秒），必须为正整数 |
| `priority` | integer | 是 | 素材优先级，必须为非负整数 |

空档与分配规则：

- 目标通道口令的半开区间 `[start_ms, end_ms)` 先裁剪到 `[0, horizon_end_ms)`
  （结束时刻晚于视界的按视界截断，起点晚于视界的不参与），再求并集；
  `[0, horizon_end_ms)` 减去并集即得空档。空档数**超过 8 个**时返回
  `422` + `TOO_MANY_GAPS`。
- 每条素材最多使用一次；同一空档内素材时长之和不得超过空档长度；素材在
  **全部空档间联合分配**（而不是逐个空档独立贪心），保证全局最优。
- 目标依次为：① **填充总时长**（全部入选素材时长之和）最大；② 并列时
  **priority 总和**最大；③ 再并列时「按空档时间顺序、空档内按 id 升序展开」
  的素材 id 序列**字典序最小**。
- 各空档内素材按 **id 升序**从空档起点**紧贴排布**（前一条的结束时刻即后一条的
  起点），空档剩余时长集中留在空档末尾。
- 请求无状态、不落数据，服务端不会修改 `commands` 与 `fillers`。

响应字段：

- `placements`：入选素材的排布，按空档时间顺序、空档内按 id 升序；每项含
  `id`、`start_ms`、`end_ms`。
- `gaps`：全部空档（含未放入素材的），按时间顺序；每项含 `start_ms`、`end_ms`
  （半开区间）与填充后的 `remaining_ms`。

### 请求示例（跨空档联合分配）

口令 `p` 占用 `main` 通道的 `[5, 6)`，视界 12 ms 内产生空档 `[0, 5)`（容量 5）
与 `[6, 12)`（容量 6）。三条素材总时长 10：唯一装下全部素材的划分是
`{a}` 进第一个空档、`{b, c}` 进第二个空档：

```bash
curl -s http://localhost:8000/fillers/plan \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [
      {"id": "p", "channel": "main", "anchor": "START", "offset_ms": 5, "duration_ms": 1}
    ],
    "channel": "main",
    "horizon_end_ms": 12,
    "fillers": [
      {"id": "a", "duration_ms": 4, "priority": 1},
      {"id": "b", "duration_ms": 3, "priority": 2},
      {"id": "c", "duration_ms": 3, "priority": 0}
    ]
  }'
```

响应示例（`200`）：

```json
{
  "placements": [
    {"id": "a", "start_ms": 0, "end_ms": 4},
    {"id": "b", "start_ms": 6, "end_ms": 9},
    {"id": "c", "start_ms": 9, "end_ms": 12}
  ],
  "gaps": [
    {"start_ms": 0, "end_ms": 5, "remaining_ms": 1},
    {"start_ms": 6, "end_ms": 12, "remaining_ms": 0}
  ]
}
```

若两条时长 6 的素材 `a`、`b` 竞争两个容量均为 6 的空档，`{a}|{b}` 与 `{b}|{a}`
的填充总时长与优先级总和都相同，按第三目标取 id 序列 `[a, b]` 字典序更小的
方案：`a` 进靠前的空档、`b` 进靠后的空档。

### 请求示例（空档超限，422）

8 条口令把视界切成 9 个空档时返回 `422` + `TOO_MANY_GAPS`：

```json
{
  "code": "TOO_MANY_GAPS",
  "message": "通道 main 在 [0, 20) 内的空档数 9 超过上限 8",
  "details": { "channel": "main", "count": 9, "limit": 8 }
}
```

## 接口约定：`POST /branches/select`

按条件选择启用哪些口令：接收现有口令数组、至多 8 个名称互异的**有序**布尔
开关（各含当前取值），以及按口令 id 给出的开关取值条件；未设置条件的口令
始终启用。服务端枚举全部开关组合，只保留「启用口令的锚点也启用、且同通道
无半开区间重叠」的**安全组合**，再从中选出相对当前取值翻转开关数最少的
组合，返回选中取值、被翻转的开关名与最终时间线。

请求体为一个对象：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `commands` | array | 是 | 现有口令数组，每一项的字段与 `POST /compile` 的请求体完全一致 |
| `switches` | array | 是 | 有序布尔开关列表，**至多 8 个**，名称互异；允许为空数组 |
| `conditions` | array | 是 | 按口令 id 给出的开关取值条件；未列出的口令始终启用，允许为空数组 |

`switches` 每一项字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `name` | string | 是 | 开关名；不能为空，各开关之间不可重复 |
| `current` | boolean | 是 | 开关当前取值；翻转数相对它计算 |

`conditions` 每一项字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 口令 id，必须存在于 `commands` 中且不能为空；同一 id 只能声明一次 |
| `requires` | object&lt;string, boolean&gt; | 是 | 开关名 → 要求取值；全部成立时该口令才启用，空对象等价于未设置条件 |

组合筛选与裁决规则：

- **启用判定**：某组合下，口令未设置条件、或其 `requires` 中每个开关的取值
  都与组合一致时启用，其余口令禁用。
- **锚点闭合**：启用口令的 `anchor` 指向被禁用口令时，该组合**无效**
  （`START` 为虚拟锚点，始终可用）。
- **冲突排除**：口令起止时刻复用编排器解析（与 `/compile` 相同），启用口令
  中同通道半开区间存在重叠（首尾相接不算）的组合**不安全**，一并排除；
  不同通道互不影响。
- **全局裁决**：在安全组合中先取相对 `current` **翻转开关数最少**者；
  并列时按 `switches` 输入顺序展开取值序列，取 **false 小于 true** 的
  字典序最小者。
- 不存在安全组合时返回 `422` + `NO_SAFE_COMBINATION`；条件引用未知口令 id
  或未知开关名同样返回 `422`。
- 请求无状态、不落数据，服务端不会修改 `commands`、`switches` 与 `conditions`。

响应字段：

- `values`：选中的开关取值（对象，按 `switches` 输入顺序）。
- `flipped`：相对当前取值被翻转的开关名，按 `switches` 输入顺序。
- `timeline`：选中取值下最终启用口令的时间线，结构与排序与 `/compile` 一致
  （按 `(start_ms, id)` 升序，只含启用口令）。

### 请求示例（锚点闭合 + 冲突排除）

`x` 仅在 `show_x=true` 时启用，但与 `a` 在 `main` 通道时间重叠；`y` 锚定
`x`，仅在 `show_y=true` 时启用。四个组合中：`(true, true)` 与 `(true, false)`
因 `x` 与 `a` 冲突被排除；`(false, true)` 因 `y` 的锚点 `x` 未启用而无效；
只有 `(false, false)` 安全：

```bash
curl -s http://localhost:8000/branches/select \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [
      {"id": "a", "channel": "main", "anchor": "START", "offset_ms": 0,   "duration_ms": 1000},
      {"id": "b", "channel": "main", "anchor": "a",     "offset_ms": 0,   "duration_ms": 500},
      {"id": "x", "channel": "main", "anchor": "START", "offset_ms": 200, "duration_ms": 500},
      {"id": "y", "channel": "sub",  "anchor": "x",     "offset_ms": 0,   "duration_ms": 300}
    ],
    "switches": [
      {"name": "show_x", "current": true},
      {"name": "show_y", "current": true}
    ],
    "conditions": [
      {"id": "x", "requires": {"show_x": true}},
      {"id": "y", "requires": {"show_y": true}}
    ]
  }'
```

响应示例（`200`）：两个开关都被翻转，`x`、`y` 不启用，时间线只剩 `a`、`b`：

```json
{
  "values": {"show_x": false, "show_y": false},
  "flipped": ["show_x", "show_y"],
  "timeline": [
    {"id": "a", "channel": "main", "anchor": "START", "offset_ms": 0, "duration_ms": 1000, "start_ms": 0,    "end_ms": 1000},
    {"id": "b", "channel": "main", "anchor": "a",     "offset_ms": 0, "duration_ms": 500,  "start_ms": 1000, "end_ms": 1500}
  ]
}
```

### 请求示例（无安全组合，422）

`a`、`b` 都未设置条件（始终启用）且在 `main` 通道时间重叠，任何开关组合都
无法消除冲突：

```bash
curl -s http://localhost:8000/branches/select \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [
      {"id": "a", "channel": "main", "anchor": "START", "offset_ms": 0,   "duration_ms": 1000},
      {"id": "b", "channel": "main", "anchor": "START", "offset_ms": 500, "duration_ms": 1000}
    ],
    "switches": [{"name": "show_x", "current": true}],
    "conditions": []
  }'
```

```json
{
  "code": "NO_SAFE_COMBINATION",
  "message": "不存在满足全部启用条件、锚点依赖闭合且无同通道冲突的开关组合",
  "details": { "combinations": 2 }
}
```

条件引用未知开关或未知口令 id 时分别返回 `UNKNOWN_SWITCH` 与
`CONDITION_TARGET_NOT_FOUND`（`details` 给出未知名称列表）。

## 接口约定：`POST /assets/preload`

直播开始前用**单台加载器**串行预载素材（片头、字幕板等）：现有口令先复用
`POST /compile` 的编排器校验并解析起止时刻，每条素材通过 `command_id` 关联
一条现有口令，**该口令解析出的起点即素材加载截止时刻**（允许恰好完成）。
服务端在就绪时刻与截止时刻的双重约束下挑选并排序素材，可跳过无法按时完成者。

请求体为一个对象：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `commands` | array | 是 | 现有口令数组，每一项的字段与 `POST /compile` 的请求体完全一致 |
| `assets` | array | 是 | 预载素材列表，**至多 12 条**，允许为空数组 |

`assets` 每一项字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 素材唯一 id；不能为空，各素材之间不可重复 |
| `command_id` | string | 是 | 关联口令 id；必须存在于 `commands`，且每条口令至多被一条素材关联 |
| `ready_ms` | integer | 是 | 素材最早可开始加载时刻（毫秒），必须为非负整数 |
| `load_ms` | integer | 是 | 素材串行加载时长（毫秒），必须为正整数 |
| `priority` | integer | 是 | 素材优先级，必须为非负整数 |

加载与裁决规则：

- **单台加载器、不可中断**：从时刻 `0` 起一次只加载一条素材，加载不可打断或并行；
  若下一条素材尚未就绪（当前时刻早于其 `ready_ms`），加载器空转等待。
- 素材开始时刻 = `max(加载器当前时刻, ready_ms)`，结束时刻 = 开始 + `load_ms`；
  **结束时刻必须不晚于关联口令的起点**，否则该素材不能入选（可跳过）。
- 全部素材的 `ready_ms` 都非负、加载器从 `0` 起工作，因此不存在「素材本身无法
  加载」的情况，跳过只可能由截止时刻约束引起。
- 目标依次为：① **按时加载条数最多**；② 并列时入选素材 **`priority` 总和最大**；
  ③ 再并列时「入选 id 升序列表」**字典序最小**；④ 同一入选集合取**总完工时刻最早**
  的加载顺序；⑤ 完工时刻仍相同时取「加载 id 顺序」**字典序最小**。
- 响应 `loads` 为入选素材的加载安排，**按加载器实际加载顺序排列**（可能与 id
  顺序不同），每项含 `id`、`start_ms`、`end_ms`；`skipped_ids` 为未入选素材 id，
  按 id 升序。
- 请求无状态、不落数据，服务端不会修改 `commands` 与 `assets`。

### 请求示例（就绪等待 + 最早完工排序）

口令 `a`、`b`、`c` 的起点分别为 1000、2000、2500。素材 `y` 到 500ms 才就绪：
全部 3 条均可按时，最早完工的加载顺序是 `x → z → y`（先加载已就绪的 `z`，
避免空转到 500，总完工 1500；若按 `x → y → z` 则需空转，总完工 1600）：

```bash
curl -s http://localhost:8000/assets/preload \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [
      {"id": "a", "channel": "main", "anchor": "START", "offset_ms": 1000, "duration_ms": 1000},
      {"id": "b", "channel": "main", "anchor": "a",     "offset_ms": 0,    "duration_ms": 500},
      {"id": "c", "channel": "main", "anchor": "b",     "offset_ms": 0,    "duration_ms": 500}
    ],
    "assets": [
      {"id": "x", "command_id": "a", "ready_ms": 0,   "load_ms": 400, "priority": 1},
      {"id": "y", "command_id": "b", "ready_ms": 500, "load_ms": 600, "priority": 2},
      {"id": "z", "command_id": "c", "ready_ms": 0,   "load_ms": 500, "priority": 3}
    ]
  }'
```

- `x`：`0 → 400`，截止 `a` 起点 1000。
- `z`：`400 → 900`，截止 `c` 起点 2500。
- `y`：`900 → 1500`（已过就绪时刻 500，无需等待），截止 `b` 起点 2000。

响应示例（`200`）：

```json
{
  "loads": [
    {"id": "x", "start_ms": 0,   "end_ms": 400},
    {"id": "z", "start_ms": 400, "end_ms": 900},
    {"id": "y", "start_ms": 900, "end_ms": 1500}
  ],
  "skipped_ids": []
}
```

### 请求示例（跳过素材换取更多按时条数）

口令 `b`、`c`、`d` 起点分别为 500、1000、1500，`e` 起点同为 1500（不同通道）。
短素材 `y`、`z`、`w` 各 500ms，首尾相接恰好全部按时；长素材 `x` 需 1000ms，
选入它至多再带 1 条短素材（共 2 条），故最优跳过 `x`、按时加载 3 条：

```bash
curl -s http://localhost:8000/assets/preload \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [
      {"id": "b", "channel": "main", "anchor": "START", "offset_ms": 500,  "duration_ms": 100},
      {"id": "c", "channel": "main", "anchor": "START", "offset_ms": 1000, "duration_ms": 100},
      {"id": "d", "channel": "main", "anchor": "START", "offset_ms": 1500, "duration_ms": 100},
      {"id": "e", "channel": "sub",  "anchor": "START", "offset_ms": 1500, "duration_ms": 100}
    ],
    "assets": [
      {"id": "x", "command_id": "e", "ready_ms": 0, "load_ms": 1000, "priority": 0},
      {"id": "y", "command_id": "b", "ready_ms": 0, "load_ms": 500,  "priority": 0},
      {"id": "z", "command_id": "c", "ready_ms": 0, "load_ms": 500,  "priority": 0},
      {"id": "w", "command_id": "d", "ready_ms": 0, "load_ms": 500,  "priority": 0}
    ]
  }'
```

```json
{
  "loads": [
    {"id": "y", "start_ms": 0,    "end_ms": 500},
    {"id": "z", "start_ms": 500,  "end_ms": 1000},
    {"id": "w", "start_ms": 1000, "end_ms": 1500}
  ],
  "skipped_ids": ["x"]
}
```

### 请求示例（素材非法，422）

关联口令不存在时返回 `422` + `PRELOAD_TARGET_NOT_FOUND`；两条素材关联同一
口令时返回 `422` + `DUPLICATE_COMMAND_LINK`：

```json
{
  "code": "PRELOAD_TARGET_NOT_FOUND",
  "message": "素材关联了不存在的口令 id: ghost",
  "details": { "command_ids": ["ghost"] }
}
```

## 接口约定：`POST /replays/plan`

直播复盘时需要从现有口令中挑选若干条录制回放：现有口令先复用 `POST /compile`
的编排器校验并解析起止时刻，每个候选口令生成一条**全局录制窗口**
`[max(0, 起点 - lead_in_ms), 终点 + lead_out_ms)`（前留白受时间原点 0 截断）；
全部入选候选的窗口**跨通道**求并集——半开区间重叠或首尾相接即合并为同一片段，
合并后的总时长才计入回放时长预算。服务端在预算内挑选优先级总和最大的候选集合。

请求体为一个对象：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `commands` | array | 是 | 现有口令数组，每一项的字段与 `POST /compile` 的请求体完全一致 |
| `candidates` | array | 是 | 回放候选口令列表，**至多 12 条**，允许为空数组 |
| `lead_in_ms` | integer | 是 | 前留白毫秒数，必须为非负整数 |
| `lead_out_ms` | integer | 是 | 后留白毫秒数，必须为非负整数 |
| `budget_ms` | integer | 是 | 回放时长预算（毫秒），必须为正数 |

`candidates` 每一项字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 候选口令 id；必须存在于 `commands` 中，不能为空，各候选之间不可重复 |
| `priority` | integer | 是 | 候选优先级，必须为非负整数 |

窗口与裁决规则：

- 每个候选的录制窗口为 `[max(0, start_ms - lead_in_ms), end_ms + lead_out_ms)`，
  与所在通道无关（全局窗口，跨通道同样参与合并）。
- 入选集合的全部窗口求并集：半开区间重叠或**首尾相接**（前一条结束时刻等于
  后一条起点）即合并为同一片段；合并片段总时长 = 各片段长度之和，
  不得超过 `budget_ms`。
- 目标依次为：① 入选 `priority` 总和最大；② 并列时入选条数最多；
  ③ 再并列时入选 id 升序列表字典序最小。
- 空集合始终可行（总时长 0）；预算装不下任何候选时返回空集合。
- 请求无状态、不落数据，服务端不会修改 `commands` 与 `candidates`。

响应字段：

- `selected_ids`：入选候选口令 id，按 id 升序。
- `segments`：入选窗口合并后的片段，按起点升序；每项含 `start_ms`、`end_ms`
  （半开区间）。
- `total_duration_ms`：合并片段总时长（毫秒），即计入预算的录制时长。

### 请求示例（跨通道合并后才计预算）

口令 `a`（`main` 通道 `[0, 100)`）与 `b`（`sub` 通道 `[50, 150)`）的窗口
跨通道重叠，合并后只占 150ms 预算；`c` 的窗口 `[1000, 1100)` 与之分离：

```bash
curl -s http://localhost:8000/replays/plan \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [
      {"id": "a", "channel": "main", "anchor": "START", "offset_ms": 0,    "duration_ms": 100},
      {"id": "b", "channel": "sub",  "anchor": "START", "offset_ms": 50,   "duration_ms": 100},
      {"id": "c", "channel": "main", "anchor": "START", "offset_ms": 1000, "duration_ms": 100}
    ],
    "candidates": [
      {"id": "a", "priority": 2},
      {"id": "b", "priority": 2},
      {"id": "c", "priority": 4}
    ],
    "lead_in_ms": 0,
    "lead_out_ms": 0,
    "budget_ms": 150
  }'
```

`{a, b}` 合并总时长 150、优先级和 4、条数 2；单条 `c` 总时长 100、优先级 4、
条数 1。优先级和相同，按条数取 `{a, b}`。响应示例（`200`）：

```json
{
  "selected_ids": ["a", "b"],
  "segments": [
    {"start_ms": 0, "end_ms": 150}
  ],
  "total_duration_ms": 150
}
```

若把预算收紧到 100，则 `{a, b}` 不再可行，单条 `c`（优先级 4）胜出，
`selected_ids` 为 `["c"]`、`segments` 为 `[{"start_ms": 1000, "end_ms": 1100}]`；
再把预算收紧到 50 则任何候选都装不下，返回空集合（`selected_ids` 与
`segments` 为空、`total_duration_ms` 为 0）。

### 请求示例（候选不存在，422）

```bash
curl -s http://localhost:8000/replays/plan \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [
      {"id": "a", "channel": "main", "anchor": "START", "offset_ms": 0, "duration_ms": 100}
    ],
    "candidates": [{"id": "ghost", "priority": 1}],
    "lead_in_ms": 0,
    "lead_out_ms": 0,
    "budget_ms": 1000
  }'
```

```json
{
  "code": "REPLAY_TARGET_NOT_FOUND",
  "message": "候选口令 id 不存在于现有口令中: ghost",
  "details": { "ids": ["ghost"] }
}
```

## 错误处理

所有业务错误均返回 HTTP `422`，响应体统一为：

```json
{
  "code": "DEPENDENCY_CYCLE",
  "message": "检测到依赖环: a -> b -> a",
  "details": { "cycle": ["a", "b", "a"] }
}
```

例外：`POST /runs/reconcile` 的并发名额与超时控制分别返回 `503`
（`RECONCILE_BUSY`）与 `504`（`RECONCILE_TIMEOUT`），响应体结构相同。

错误码：

| `code` | 触发条件 |
| --- | --- |
| `INVALID_REQUEST` | 请求体不是合法的口令数组（字段缺失、类型错误、多余字段等） |
| `DUPLICATE_ID` | 数组中出现重复 `id` |
| `INVALID_ID` | `id` 为空，或使用了保留锚点名 `START` |
| `INVALID_CHANNEL` | `channel` 为空 |
| `INVALID_DURATION` | `duration_ms` 不是正数 |
| `MISSING_ANCHOR` | `anchor` 指向一个不存在的口令 id |
| `DEPENDENCY_CYCLE` | 锚点依赖构成环 |
| `NEGATIVE_START` | 解析出的口令起点小于 `0`（起点不允许为负） |
| `CHANGE_TARGET_NOT_FOUND` | 仅 `POST /preview`：`change.id` 在口令数组中不存在 |
| `INVALID_FIXED_START` | 仅 `POST /schedule/rebalance`：固定开场时刻为负，或未按节目顺序严格递增 |
| `INVALID_ACTUAL_END` | 仅 `POST /schedule/rebalance`：实际结束时刻为负、已结束段不构成连续前缀、结束时刻非单调不减，或已结束固定点段结束时刻早于其固定开场时刻 |
| `INSUFFICIENT_MARGIN` | 仅 `POST /schedule/rebalance`：固定点前可压缩余量不足，`details.unrecoverable_sec` 为无法追回的秒数 |
| `INVALID_INTERVAL` | 仅 `POST /runs/reconcile`、`POST /runs/calibrate`：实播记录起点为负，或结束时刻不大于起点 |
| `CALIBRATION_TARGET_NOT_FOUND` | 仅 `POST /runs/calibrate`：实播记录 `id` 未出现在计划口令中 |
| `CHANNEL_MISMATCH` | 仅 `POST /runs/calibrate`：实播记录通道与对应计划口令的通道不一致 |
| `ROUTING_TARGET_NOT_FOUND` | 仅 `POST /routing/assign`：`backups[].id` 引用了不存在于 `commands` 的口令 id |
| `DUPLICATE_BACKUP_CHANNEL` | 仅 `POST /routing/assign`：某条口令的备用通道列表存在重复通道名（含把原通道又列入） |
| `TOO_MANY_BACKUP_TARGETS` | 仅直接调用核心逻辑时：携带备用通道的口令超过 12 条（HTTP 层由请求结构校验提前返回 `INVALID_REQUEST`） |
| `NO_FEASIBLE_ROUTING` | 仅 `POST /routing/assign`：不存在满足时间重叠约束的无冲突方案，`details.reason` 为 `FIXED_OVERLAP` / `NO_AVAILABLE_CHANNEL` / `INSUFFICIENT_CHANNEL_CAPACITY` |
| `INTERRUPTION_TARGET_NOT_FOUND` | 仅 `POST /interruptions/plan`：`cancellable_ids` 引用了不存在于 `commands` 的口令 id |
| `NO_FEASIBLE_INTERRUPTION` | 仅 `POST /interruptions/plan`：起点窗口内每个可行起点的撤销闭包都含不可撤销口令，`details.reason=UNREVOCABLE_OVERLAP`，`details.blocking_ids` 为阻塞口令 |
| `INVALID_HORIZON` | 仅 `POST /fillers/plan`：`horizon_end_ms` 不是正数 |
| `INVALID_PRIORITY` | 仅 `POST /fillers/plan`：素材 `priority` 为负数 |
| `TOO_MANY_FILLERS` | 仅直接调用核心逻辑时：素材超过 12 条（HTTP 层由请求结构校验提前返回 `INVALID_REQUEST`） |
| `TOO_MANY_GAPS` | 仅 `POST /fillers/plan`：目标通道在 `[0, horizon_end_ms)` 内的空档超过 8 个，`details.count` / `details.limit` 给出实际数与上限 |
| `INVALID_SWITCH` | 仅 `POST /branches/select`：开关名为空 |
| `DUPLICATE_SWITCH` | 仅 `POST /branches/select`：开关名重复 |
| `TOO_MANY_SWITCHES` | 仅直接调用核心逻辑时：布尔开关超过 8 个（HTTP 层由请求结构校验提前返回 `INVALID_REQUEST`） |
| `CONDITION_TARGET_NOT_FOUND` | 仅 `POST /branches/select`：条件引用了不存在于 `commands` 的口令 id |
| `UNKNOWN_SWITCH` | 仅 `POST /branches/select`：条件引用了未在 `switches` 中定义的开关名 |
| `NO_SAFE_COMBINATION` | 仅 `POST /branches/select`：枚举全部开关组合后不存在安全组合，`details.combinations` 为枚举的组合总数 |
| `PRELOAD_TARGET_NOT_FOUND` | 仅 `POST /assets/preload`：素材 `command_id` 引用了不存在于 `commands` 的口令 id |
| `DUPLICATE_COMMAND_LINK` | 仅 `POST /assets/preload`：两条素材关联了同一条口令 id，`details.command_ids` 给出重复的口令 id |
| `INVALID_READY_MS` | 仅 `POST /assets/preload`：素材 `ready_ms` 为负数 |
| `TOO_MANY_PRELOAD_ASSETS` | 仅直接调用核心逻辑时：素材超过 12 条（HTTP 层由请求结构校验提前返回 `INVALID_REQUEST`） |
| `REPLAY_TARGET_NOT_FOUND` | 仅 `POST /replays/plan`：候选 `id` 引用了不存在于 `commands` 的口令 id |
| `INVALID_REPLAY_PADDING` | 仅 `POST /replays/plan`：`lead_in_ms` 或 `lead_out_ms` 为负数 |
| `INVALID_REPLAY_BUDGET` | 仅 `POST /replays/plan`：`budget_ms` 不是正数 |
| `TOO_MANY_REPLAY_CANDIDATES` | 仅直接调用核心逻辑时：候选超过 12 条（HTTP 层由请求结构校验提前返回 `INVALID_REQUEST`） |
| `RECONCILE_BUSY` | 仅 `POST /runs/reconcile`（HTTP `503`）：未结束对账任务（执行中 + 排队）已达 2 个，新请求未提交计算 |
| `RECONCILE_TIMEOUT` | 仅 `POST /runs/reconcile`（HTTP `504`）：对账未在 `RECONCILE_TIMEOUT_SECONDS` 内完成（含排队时间） |

> `POST /versions/merge` 的字段级分歧不是错误：无论是否合并成功都返回 HTTP `200`，
> 由响应体的 `status`（`merged` / `diverged`）区分；仅版本内部重复 id
> （`DUPLICATE_ID`）或合并口令编排非法（`MISSING_ANCHOR` / `DEPENDENCY_CYCLE` /
> `NEGATIVE_START` 等原错误码）时返回 `422`。

`POST /schedule/rebalance` 额外约定：

- `planned_duration_sec` 短于 `min_duration_sec`、或任一时长非正数时，返回
  `INVALID_DURATION`；重复 `id`、空 `id` 分别返回 `DUPLICATE_ID`、`INVALID_ID`。
- 已结束段的实际结束时刻越过下一个固定点、且窗口内已无可压缩的段时，同样返回
  `INSUFFICIENT_MARGIN`（无法追回的时间已成事实）。

`POST /preview` 额外约定：

- `change` 缺少 `id`、`offset_ms` / `duration_ms` 一个都没给、字段类型错误或出现多余字段时，
  返回 `INVALID_REQUEST`。
- 原编排本身非法时，直接返回原编排对应的错误码；改动后编排非法（如把时长改成 0、起点改为负）时，
  返回改动后编排对应的错误码（如 `INVALID_DURATION`、`NEGATIVE_START`）。

`POST /runs/reconcile` 额外约定：

- `commands` 的全部校验与错误码和 `/compile` 完全一致（含重复 id、缺失锚点、依赖环、
  负起点等）；`records` 中重复 `id`、空 `id`、空 `channel` 分别返回
  `DUPLICATE_ID`、`INVALID_ID`、`INVALID_CHANNEL`。
- `tolerance_ms`、`miss_penalty`、`extra_penalty` 为负数、字段缺失或类型错误，
  以及请求出现多余字段时，返回 `INVALID_REQUEST`。
- `actual_start_ms` 为负、或 `actual_end_ms` 不大于 `actual_start_ms` 时，
  返回 `INVALID_INTERVAL`。
- 计划口令与实播记录同时非法时，先返回计划侧（`commands`）的错误码。
- 计算在应用生命周期管理的单工作进程中执行：未结束任务（执行中 + 排队）达到
  2 个时新请求立即返回 `503` + `RECONCILE_BUSY`（不提交计算）；自接纳起超过
  `RECONCILE_TIMEOUT_SECONDS`（默认 10 秒，排队时间计入）返回 `504` +
  `RECONCILE_TIMEOUT`；超时或请求取消时，排队中的任务撤销并立即释放名额，
  执行中的任务等实际结束才释放名额，迟到结果丢弃。

`POST /runs/calibrate` 额外约定：

- `commands` 的全部校验与错误码和 `/compile` 完全一致；`records` 的字段级校验
  （重复 `id`、空 `id`、空 `channel`、非法区间）与 `/runs/reconcile` 完全一致，
  计划口令与实播记录同时非法时，先返回计划侧的错误码。
- 记录 `id` 未出现在计划口令中时返回 `CALIBRATION_TARGET_NOT_FOUND`
  （`details.ids` 为未出现的 id，按 id 升序）；记录通道与计划口令不一致时返回
  `CHANNEL_MISMATCH`（`details.mismatches` 给出计划通道与实播通道）；
  两类问题同时存在时先报 `CALIBRATION_TARGET_NOT_FOUND`。
- `tolerance_ms` 为负数、字段缺失或类型错误，以及请求出现多余字段时，
  返回 `INVALID_REQUEST`。
- 响应只包含实播记录出现过的通道，按通道名升序；`consistent_ids` 与
  `anomalous_ids` 均按 id 升序。
- 请求无状态、不落数据；服务端不会修改 `commands` 与 `records`。

`POST /routing/assign` 额外约定：

- `commands` 的全部校验与错误码和 `/compile` 完全一致（含重复 id、缺失锚点、
  依赖环、负起点等），且编排非法时优先返回编排错误码。
- `backups` 中 id 为空返回 `INVALID_ID`；同一 id 重复声明返回 `DUPLICATE_ID`；
  id 不存在于 `commands` 返回 `ROUTING_TARGET_NOT_FOUND`。
- 备用通道名为空字符串返回 `INVALID_CHANNEL`；同一条声明内通道名重复、或把
  原通道又列入备用列表，返回 `DUPLICATE_BACKUP_CHANNEL`。
- `backups` 超过 12 项、`backup_channels` 为空列表、字段缺失/类型错误/多余字段，
  返回 `INVALID_REQUEST`。
- 无可行方案返回 `NO_FEASIBLE_ROUTING`：不可改派的口令在原通道自身时间重叠时
  `reason=FIXED_OVERLAP`；某条口令的原通道与全部备用通道都被时间重叠口令占用时
  `reason=NO_AVAILABLE_CHANNEL`（`details.ids` 给出对应口令）；其余情形为
  `INSUFFICIENT_CHANNEL_CAPACITY`。
- 服务端不会修改请求数据；改派不改变任何起止时刻，`timeline` 仅 `channel` 可能变化。

`POST /versions/merge` 额外约定：

- 三份口令均先按 `/compile` 的缺省规则补全 `anchor`、`offset_ms` 再比较，因此
  省略缺省字段与显式给出缺省值视为相同内容。
- 某份版本（`base` / `left` / `right`）内部出现重复 id 时返回 `DUPLICATE_ID`，
  `details.side` 标明版本名；检测顺序为 `base` → `left` → `right`。
- 无分歧时合并口令的全部编排校验与错误码和 `/compile` 完全一致（缺失锚点、依赖环、
  负起点、空 id / 通道、非正时长等），编排非法时不返回合并口令。
- 字段级分歧（`MODIFY_MODIFY` / `ADD_ADD` / `DELETE_MODIFY`）始终返回 HTTP `200`
  且 `status="diverged"`，响应只有 `status` 与 `divergences` 两个字段。
- 请求无状态、不落数据；服务端不会修改三份请求数组。

`POST /interruptions/plan` 额外约定：

- `commands` 的全部校验与错误码和 `/compile` 完全一致（含重复 id、缺失锚点、
  依赖环、负起点等），且编排非法时优先返回编排错误码。
- `duration_ms` 非正返回 `INVALID_DURATION`；`channel` 为空返回
  `INVALID_CHANNEL`；`earliest_start_ms` / `latest_start_ms` 为负返回
  `INVALID_REQUEST`；`latest_start_ms < earliest_start_ms` 同样返回
  `INVALID_REQUEST`。
- `cancellable_ids` 中出现空串返回 `INVALID_ID`；重复 id 返回 `DUPLICATE_ID`；
  引用不存在的口令 id 返回 `INTERRUPTION_TARGET_NOT_FOUND`。
- 字段缺失、类型错误、多余字段等结构问题返回 `INVALID_REQUEST`。
- 起点窗口内不存在任何可行起点时返回 `NO_FEASIBLE_INTERRUPTION`，
  `details.reason` 固定为 `UNREVOCABLE_OVERLAP`，`details.blocking_ids` 列出窗口内
  任意起点撤销闭包中可能出现的全部不可撤销口令 id。
- 起点选择只扫描现有口令起止事件对应的候选区间，不逐毫秒枚举；
  `timeline` 为剩余口令的 `/compile` 原解析结果，不含插播口令本身。
- 请求无状态、不落数据；服务端不会修改 `commands` 与 `cancellable_ids`。

`POST /fillers/plan` 额外约定：

- `commands` 的全部校验与错误码和 `/compile` 完全一致（含重复 id、缺失锚点、
  依赖环、负起点等），且编排非法时优先返回编排错误码。
- `channel` 为空返回 `INVALID_CHANNEL`；`horizon_end_ms` 非正返回
  `INVALID_HORIZON`。
- 素材 `id` 为空返回 `INVALID_ID`；重复 `id` 返回 `DUPLICATE_ID`；
  `duration_ms` 非正返回 `INVALID_DURATION`；`priority` 为负返回
  `INVALID_PRIORITY`。
- 素材超过 12 条、字段缺失 / 类型错误 / 多余字段等结构问题返回
  `INVALID_REQUEST`（直接调用核心逻辑时素材超限返回 `TOO_MANY_FILLERS`）。
- 空档超过 8 个返回 `TOO_MANY_GAPS`（`details.count` / `details.limit`
  给出实际空档数与上限）。
- 分配按「空档 × 素材子集」动态规划求解（素材 ≤ 12、空档 ≤ 8），
  第三目标的 id 序列字典序经整数编码参与比较，不逐方案枚举。
- 请求无状态、不落数据；服务端不会修改 `commands` 与 `fillers`。

`POST /branches/select` 额外约定：

- `commands` 的全部校验与错误码和 `/compile` 完全一致（含重复 id、缺失锚点、
  依赖环、负起点等），且编排非法时优先返回编排错误码。
- 开关名为空返回 `INVALID_SWITCH`；开关名重复返回 `DUPLICATE_SWITCH`；
  开关超过 8 个、字段缺失 / 类型错误 / 多余字段等结构问题返回
  `INVALID_REQUEST`（直接调用核心逻辑时开关超限返回 `TOO_MANY_SWITCHES`）。
- 条件中口令 id 为空返回 `INVALID_ID`；同一口令重复声明条件返回
  `DUPLICATE_ID`；条件引用不存在的口令 id 返回 `CONDITION_TARGET_NOT_FOUND`；
  条件引用未定义的开关返回 `UNKNOWN_SWITCH`；口令 id 与开关同时未知时，
  先报 `CONDITION_TARGET_NOT_FOUND`。
- 组合枚举、翻转数与取值序列字典序均按 `switches` 的输入顺序；
  响应的 `values` 与 `flipped` 也按该顺序给出。
- 不存在安全组合时返回 `NO_SAFE_COMBINATION`，`details.combinations`
  为枚举的组合总数（即 `2 ^ 开关个数`）。
- 请求无状态、不落数据；服务端不会修改 `commands`、`switches` 与 `conditions`。

`POST /assets/preload` 额外约定：

- `commands` 的全部校验与错误码和 `/compile` 完全一致（含重复 id、缺失锚点、
  依赖环、负起点等），且编排非法时优先返回编排错误码。
- 素材 `id` 为空返回 `INVALID_ID`；重复 `id` 返回 `DUPLICATE_ID`；
  `command_id` 为空串同样返回 `INVALID_ID`（`details.ids` 为对应素材 id）；
  `ready_ms` 为负返回 `INVALID_READY_MS`；`load_ms` 非正返回
  `INVALID_DURATION`；`priority` 为负返回 `INVALID_PRIORITY`。
- 两条素材关联同一口令返回 `DUPLICATE_COMMAND_LINK`
  （`details.command_ids` 给出被重复关联的口令 id）；`command_id` 不存在于
  `commands` 返回 `PRELOAD_TARGET_NOT_FOUND`（`details.command_ids` 给出未知
  口令 id）；两类问题同时存在时先报 `DUPLICATE_COMMAND_LINK`。
- 素材超过 12 条、字段缺失 / 类型错误 / 多余字段等结构问题返回
  `INVALID_REQUEST`（直接调用核心逻辑时素材超限返回 `TOO_MANY_PRELOAD_ASSETS`）。
- 素材子集按位掩码动态规划求解（素材 ≤ 12）：先用「枚举最后加载素材」的子集
  DP 求每个子集的最早可行完工时刻，入选集合按 (条数, priority 总和, 入选 id
  升序列表编码) 裁决；确定集合后逐位置用「给定起始时刻的子集最早完工」DP
  恢复最早总完工、并列加载 id 顺序字典序最小的排列，不逐排列枚举。
- 素材的截止时刻取**关联口令的起点**（允许恰好完成）；`loads` 按实际加载顺序
  排列，加载器可在未就绪素材前空转等待。
- 请求无状态、不落数据；服务端不会修改 `commands` 与 `assets`。

`POST /replays/plan` 额外约定：

- `commands` 的全部校验与错误码和 `/compile` 完全一致（含重复 id、缺失锚点、
  依赖环、负起点等），且编排非法时优先返回编排错误码。
- `lead_in_ms` / `lead_out_ms` 为负返回 `INVALID_REPLAY_PADDING`；
  `budget_ms` 非正返回 `INVALID_REPLAY_BUDGET`。
- 候选 `id` 为空返回 `INVALID_ID`；重复 `id` 返回 `DUPLICATE_ID`；
  `priority` 为负返回 `INVALID_PRIORITY`；候选 `id` 不存在于 `commands`
  返回 `REPLAY_TARGET_NOT_FOUND`（`details.ids` 给出未知 id，按 id 升序）。
- 候选超过 12 条、字段缺失 / 类型错误 / 多余字段等结构问题返回
  `INVALID_REQUEST`（直接调用核心逻辑时候选超限返回
  `TOO_MANY_REPLAY_CANDIDATES`）。
- 候选窗口与通道无关：跨通道重叠或首尾相接同样合并；空集合始终可行，
  预算装不下任何候选时返回空集合（`selected_ids` 与 `segments` 为空、
  `total_duration_ms` 为 0）。
- 候选至多 12 条，服务端按位掩码枚举全部子集（至多 4096 个），逐子集合并
  窗口并计算合并总时长，第三目标的 id 列表字典序经整数编码参与比较。
- 请求无状态、不落数据；服务端不会修改 `commands` 与 `candidates`。

## 本地开发（不使用 Docker）

要求 Python 3.11+：

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

核心编排逻辑位于 `app/compiler.py`，节目单延误分摊逻辑位于 `app/rebalance.py`，
实播对账逻辑位于 `app/reconcile.py`（其单工作进程调度位于
`app/reconcile_worker.py`），时钟校准逻辑位于 `app/calibrate.py`，
通道改派逻辑位于 `app/routing.py`，
口令三方合并逻辑位于 `app/merge.py`，临时插播规划逻辑位于
`app/interruptions.py`，空档填充规划逻辑位于 `app/fillers.py`，
开关分支选择逻辑位于 `app/branches.py`，素材预载规划逻辑位于
`app/preload.py`，回放窗口预算规划逻辑位于 `app/replays.py`，
均仅使用标准库，可直接运行单元测试：

```bash
python3 -m unittest discover -s tests
```

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── branches.py        # 纯标准库的开关分支选择（组合枚举 + 锚点闭合 / 冲突过滤 + 最少翻转裁决）
│   ├── calibrate.py       # 纯标准库的时钟校准（候选偏移区间扫描 + 一致数最多裁决）
│   ├── compiler.py        # 纯标准库的依赖解析 / 时间计算 / 冲突检测
│   ├── fillers.py         # 纯标准库的空档填充规划（空档并集 + 子集 DP 联合分配）
│   ├── interruptions.py   # 纯标准库的临时插播规划（锚点森林欧拉序 + 线段树事件扫描）
│   ├── merge.py           # 纯标准库的口令三方合并（按 id、字段合并 + 复用编排器校验）
│   ├── preload.py         # 纯标准库的素材预载规划（单台串行加载器 + 子集 DP 最早完工与字典序裁决）
│   ├── rebalance.py       # 纯标准库的节目单延误分摊（固定点切窗 + 余量比例分摊）
│   ├── reconcile.py       # 纯标准库的实播对账（通道内保序一对一匹配 + 最小罚分 DP）
│   ├── reconcile_worker.py # /runs/reconcile 的单工作进程调度（1 执行 + 1 排队名额、超时与取消）
│   ├── replays.py         # 纯标准库的回放窗口预算规划（全局窗口合并 + 子集枚举三目标裁决）
│   ├── routing.py         # 纯标准库的通道改派（位掩码分支限界：最少改派 + 字典序裁决）
│   ├── schemas.py         # Pydantic 请求与响应模型
│   └── main.py            # FastAPI 入口（/compile、/preview、/schedule/rebalance、/runs/reconcile、/runs/calibrate、/routing/assign、/versions/merge、/interruptions/plan、/fillers/plan、/branches/select、/assets/preload、/replays/plan、/health、统一错误处理）
├── tests/
│   ├── test_branches.py       # 开关分支选择核心逻辑单元测试（标准库 unittest）
│   ├── test_calibrate.py      # 时钟校准核心逻辑单元测试（标准库 unittest）
│   ├── test_compiler.py       # 编排核心逻辑单元测试（标准库 unittest）
│   ├── test_fillers.py        # 空档填充规划核心逻辑单元测试（含暴力枚举对照 fuzz）
│   ├── test_interruptions.py  # 临时插播规划核心逻辑单元测试（含逐毫秒暴力对照 fuzz）
│   ├── test_merge.py          # 三方合并核心逻辑单元测试（标准库 unittest）
│   ├── test_preload.py        # 素材预载规划核心逻辑单元测试（含子集 × 全排列暴力对照 fuzz）
│   ├── test_rebalance.py      # 延误分摊核心逻辑单元测试（标准库 unittest）
│   ├── test_reconcile.py      # 实播对账核心逻辑单元测试（标准库 unittest）
│   ├── test_reconcile_worker.py # 对账工作进程调度的 HTTP 级测试（名额、超时、健康检查不被阻塞）
│   ├── test_replays.py        # 回放窗口预算规划核心逻辑单元测试（含暴力枚举对照 fuzz）
│   └── test_routing.py        # 通道改派核心逻辑单元测试（标准库 unittest）
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```
