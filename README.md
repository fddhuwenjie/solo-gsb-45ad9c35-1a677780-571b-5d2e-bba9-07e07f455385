# 文档修订批注续接 API

基于 **Python + FastAPI + SQLite** 的批注跨版本续接与审阅依赖编排服务。

为文档中的批注保存锚点（**选区 + 左右上下文 + 按 Unicode 码点计的原始偏移**），
当文档提交新版全文时，自动把每条批注续接到新文本中的最佳落点；
歧义时交人工确认，所有版本与迁移结果**只追加**，可随时审计任意两版间每条批注的范围、状态与匹配依据。

同时支持**审阅依赖编排**：批量提交同一文档内「前置批注 → 后续批注」的依赖关系
（整批校验跨文档引用、自依赖、重复边与环，任一失败则全部不落库），
审阅计划按依赖图与完成记录把批注划分为**可处理 / 被阻塞 / 已完成**，
并按拓扑层级给出可并行批次；完成记录只追加、重复提交幂等，
完成前置批注后立即解锁符合条件的后续批注。

还支持**可离线核验的迁移归档**：在同一个 SQLite 读事务中导出文档元数据、
全部版本、批注、迁移记录与人工确认的规范 JSON 及其 SHA-256 摘要；
核验只依据归档与另行保存的摘要本身，**不查当前数据库、不写入任何数据**，
缺行、串文档、篡改或摘要不符时返回带 JSON 路径定位的错误。

最后支持**文档发布门禁**：按名称只追加地保存嵌套发布策略修订
（`all` / `any` / `not` 组合 `location_count` / `review_count` 叶子，
未知字段、空组合、`not` 子项不是一个、深度超过 8 或节点超过 64 时整条拒绝）；
评估接口在**同一个 SQLite 读事务**内锁定策略修订、目标文档版本与当前审阅状态，
返回与策略**同形**、`all`/`any` **不短路**的解释树
（叶子列出实际值、阈值与参与计数的批注 ID），相同快照下结果与 ID 顺序稳定。

此外支持**批注驱动的脱敏副本**：请求指定文档版本、批注 ID 集合与占位文本，
在**同一个 SQLite 读事务**（同一快照）中读取版本全文与各批注在该版上的生效
位置；只接受状态为 `resolved` / `confirmed` 的有效范围，批注跨文档、缺少位置、
位置 `ambiguous`（含顺延占位）/ `unresolved` 或请求 ID 重复时**整批**返回带
请求下标定位的错误，不猜测、不输出半成品。重叠或首尾相接的范围合并为最大区间，
按起点顺序用同一占位文本替换，每个占位只出现一次并列出覆盖它的批注 ID；
响应返回脱敏全文及源 / 结果 Unicode 码点半开区间映射（**未遮盖片段也可追溯**）。
接口只读、**不写入任何数据**，相同快照与相同请求必然得到字节一致的结果。

## 快速开始

一条命令在 8000 端口启动（二选一）：

```bash
docker compose up --build
# 或
docker build -t annotation-api . && docker run -p 8000:8000 annotation-api
```

启动后：

- 交互式 API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

数据通过 Compose 命名卷 `annotation-data` 持久化（SQLite 文件 `/data/app.db`）。

### 本地开发（不用 Docker）

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000   # 数据库默认写入 ./data/app.db
pytest                                       # 运行测试（162 个用例）
```

可用环境变量 `DB_PATH` 指定 SQLite 文件路径。

## 核心概念

### 锚点（Anchor）

创建批注时保存：

| 字段 | 含义 |
|---|---|
| `selection` | 选区文本 |
| `left_context` / `right_context` | 选区左右上下文 |
| `start` / `end` | 原始偏移，**按 Unicode 码点计**（`end` 不含；emoji 等 BMP 外字符算 1 个码点） |

> ⚠️ 前端若用 JavaScript 计算偏移，注意 JS 字符串按 UTF-16 码元计，
> 遇到 emoji 等字符会与本服务的码点偏移不一致，需先换算。

创建时服务端会严格校验 `text[start:end] == selection` 及左右上下文与基准文本一致，否则返回 422。

### 续接算法

提交新版全文时，对每条批注：

1. 在新文本中**枚举选区的全部精确落点**（允许重叠）；
2. 对每个落点计算得分 = **左上下文后缀匹配长度 + 右上下文前缀匹配长度**（码点数）；
3. 按得分降序（同分按起点升序）排序：
   - **唯一最高分** → `resolved`，自动续接到该落点；
   - **并列最高分** → `ambiguous`，返回全部候选范围，等待人工确认；
   - **找不到选区** → `unresolved`（选区在后续版本重新出现时会自动恢复）。

### 状态机

```
resolved ──迁移──→ resolved / ambiguous / unresolved
ambiguous ──未确认──→ 下一版顺延占位（carried_forward=true，不参与自动迁移）
ambiguous ──人工确认──→ confirmed ──下一版起恢复自动迁移──→ resolved / ...
unresolved ──选区重现──→ resolved
```

- `ambiguous` 批注在确认前**不参与**后续版本的自动迁移，每个新版本只追加一条顺延占位记录；
- 确认是对某一版歧义记录从候选范围中选定一个范围，确认结果同样只追加、不可更改；
- 同一歧义记录至多一条确认：检查与写入在同一个写事务中完成，且 `confirmations.location_id`
  有唯一索引兜底，并发确认时后到者稳定返回 409，不会覆盖首条或留下重复行；
- 确认占位记录（顺延产生的）会被拒绝（409），请确认原始歧义记录。

### 只追加（append-only）

- 版本：每次提交全文生成新版本号，旧版本内容不可改；
- 迁移结果：每条批注在每个版本上各有一条 `locations` 记录，只插入不更新；
- 确认：写入独立的 `confirmations` 表，查询时计算生效状态（`confirmed`）。

### 批注改动影响分析

`GET /documents/{id}/impact?from_version=&to_version=` 基于**两版全文**生成
**稳定的 Unicode 码点级编辑片段**（`insert` / `delete` / `replace`，按位置升序，
相同输入必然得到相同的片段序列与分类），并把**起始版**每条批注的范围投影到**目标版**：

| 分类 | 含义 |
|---|---|
| `unchanged` | 选区未被编辑触及，偏移也不变 |
| `shifted` | 选区内容未变，偏移仅受区间外（含边界）编辑影响而平移 |
| `modified` | 选区被部分删除、被替换，或选区**内部**有插入 |
| `deleted` | 原选区全部被删除（投影折叠为空区间，位于删除发生处） |
| `indeterminate` | 起始版位置歧义 / 未解析 / 缺失，**不猜测范围**，`reason` 说明原因 |

规则要点：

- **插入恰好落在选区边界**（起点或终点）**不算 `modified`**：落在起点时选区整体后移
  （`shifted`），落在终点时选区不受影响（`unchanged`）；只有严格落在选区**内部**的
  插入才是 `modified`；
- **替换**（哪怕整体替换）一律是 `modified`，只有纯粹被删除片段完全覆盖才是 `deleted`；
- 每条批注返回：原范围（`from`，含起始版迁移状态）、投影范围（`to`）、
  命中的编辑片段（`hits`，删除/替换与选区相交、插入落点在闭区间 `[start, end]`
  即命中；区间外的编辑不命中，即使它造成了平移）；
- `indeterminate` 时 `to` 为 `null`、`hits` 为空，`from` 给出起始版状态
  （`ambiguous` / `unresolved`，或起始版无迁移记录时整体为 `null`）；
- 该接口为只读的确定性计算，结果不落库，可重复调用审计。

### 审阅依赖编排

在同一文档内为批注建立「**前置批注 → 后续批注**」的审阅依赖：
后续批注须等全部前置批注完成后才可处理。

**批量提交依赖**（`POST /documents/{id}/dependencies`）：一次提交同一文档内多条关系，
整批写入前依次校验，**任一失败则任何关系都不落库**：

| 校验 | 失败响应 |
|---|---|
| 引用的批注不存在 | 404 |
| 跨文档引用（批注属于其他文档） | 422 |
| 自依赖（前置 = 后续） | 422 |
| 本批内重复边 | 422 |
| 与已落库边重复 | 409 |
| 环（已落库边 ∪ 本批新边） | 422，`detail.cycle` 给出闭环路径 |

环检测按**批注编号升序遍历**（起点升序、邻接点升序），返回**首条以环中最小编号开头
的闭环路径**，如 `{"detail": {"message": "依赖关系存在环：1 → 3 → 4 → 1", "cycle": [1, 3, 4, 1]}}`；
相同输入必然得到相同路径。依赖边只追加，不提供修改 / 删除。

**审阅计划**（`GET /documents/{id}/review-plan`）：按依赖图与完成记录把文档全部批注
划分为三类，只读计算、不落库：

- `actionable`（可处理）：未完成，且所有传递前置项均已完成（无依赖的批注天然可处理）；
- `blocked`（被阻塞）：存在尚未完成的传递前置项，
  `incomplete_prerequisites` 按编号升序列出**所有**尚未完成的传递前置项；
- `completed`（已完成）：已有完成记录。

`batches` 把未完成批注按**拓扑层级**划分为可并行批次：第 0 层即当前可处理项，
第 k 层在其前置全部落入更低层后可并行处理；同层批注互不依赖。

**完成记录**（`POST /documents/{id}/annotations/{aid}/completions`）：

- 只追加：一条批注至多一条完成记录，不提供修改 / 删除；
- 重复提交**幂等**：返回 200 与原记录（`already_completed=true`），不新增行；
- 被阻塞的批注不可完成（409，`detail.incomplete_prerequisites` 列出未完成前置项）；
- 完成前置批注后**立即解锁**符合条件的后续批注：响应的
  `unlocked_annotation_ids` 列出本次新解锁的批注，审阅计划同步反映。

### 迁移归档导出与离线核验

**导出**（`GET /documents/{id}/archive`）在**同一个 SQLite 读事务**（同一快照）
中取出文档的全部审计数据，各列表**按行 ID 升序**、字段固定：

| 归档字段 | 内容 |
|---|---|
| `format` | 归档格式标识（`annotation-archive/1`） |
| `document` | 文档元数据（id / title / created_at） |
| `versions` | 全部版本（含每版全文） |
| `annotations` | 全部批注及锚点（选区、左右上下文、原始偏移、创建版引用） |
| `locations` | 全部迁移记录（状态、范围、得分、候选落点、顺延引用） |
| `confirmations` | 全部人工确认（所选范围、备注） |

响应在 `archive` 之外另行给出 `digest`，摘要算法公开且只依赖标准库：

```python
import hashlib, json

digest = hashlib.sha256(
    json.dumps(archive, ensure_ascii=False,
               sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
```

即「**键按字典序、紧凑分隔、UTF-8 不转义**」的规范化 JSON 的 SHA-256。
摘要应在导出后**另行保存**（与归档分开存放），日后随归档一起提交核验。

**核验**（`POST /archives/verify`，请求体 `{"archive": ..., "digest": ...}`）
**不查当前数据库、不写入任何数据**，只复算摘要并检查归档内部一致性：

1. **摘要复算**：归档任一字节被改动而摘要未随之更新 → `digest_mismatch`；
2. **版本编号连续**：版本号必须从 1 开始、连续、无重复（`version_gap` /
   `duplicate_version_number`）；
3. **归属一致（防串文档、缺行）**：版本与批注的 `document_id` 必须等于归档文档
   （`cross_document`）；迁移记录引用的批注 / 版本、批注引用的创建版、
   确认引用的迁移记录、顺延引用的目标记录都必须在归档中存在
   （`dangling_*`）；
4. **每版恰有一条迁移记录**：每条批注从其**创建版到末版**，每个版本号恰有一条
   `locations` 记录（缺行 `missing_location`、多行 `duplicate_location`、
   早于创建版 `unexpected_location`）；
5. **顺延引用合法**：`carried_from` 必须指向**同一批注、较早版本的真实歧义记录**
   （该记录本身非顺延且 `status=ambiguous`，否则 `bad_carried_forward`）；
6. **人工确认合法**：确认必须指向**非顺延的歧义记录**（`bad_confirmation`），
   且所选 `[start, end)` 必须属于该记录的候选落点
   （`confirmation_not_candidate`）。

通过时返回 `{"valid": true, "document_id": ..., "digest": ..., "counts": {...}}`；
失败返回 422，`detail.errors` **一次性列出全部问题**，每条含：

| 字段 | 含义 |
|---|---|
| `error` | 机器可读错误码（见上） |
| `message` | 含业务 ID / 版本号的中文说明 |
| `path` | JSON 路径式定位，如 `versions[2]`、`annotations[1]`、`locations[5]`、`confirmations[0]` |

> 摘要不符意味着归档来源已不可信，此时只报 `digest_mismatch`，
> 不再对被篡改内容做进一步语义检查。

### 文档发布门禁

发布门禁用一棵**嵌套策略树**表达「什么情况下文档可以发布」。策略按**名称**
管理（全局命名，不绑定某一篇文档），同名提交**只追加修订**：每次合法提交
生成一个从 1 起递增的新 `revision`，旧修订不可改、不可删；评估时可以锁定
任意历史修订。

**节点语法**（每个节点恰好带下列一个键）：

| 节点 | 形态 | 语义 |
|---|---|---|
| 全部满足 | `{"all": [节点, ...]}` | 子节点全真才为真 |
| 任一满足 | `{"any": [节点, ...]}` | 子节点任一为真即可 |
| 取反 | `{"not": 节点}` | 唯一子节点为假时为真 |
| 位置计数叶子 | `{"location_count": {"status", "op", "value"}}` | 目标版本上该生效位置状态的批注数 |
| 审阅计数叶子 | `{"review_count": {"status", "op", "value"}}` | 当前该审阅状态的批注数 |

叶子字段**固定**为 `status` / `op` / `value` 三个，取值域：

- `location_count.status`：`resolved` / `confirmed` / `ambiguous` / `unresolved`
  （`confirmed` 是人工确认后的生效状态；顺延占位仍计为 `ambiguous`；
  目标版本上没有迁移记录的批注——如创建于更晚版本——不参与任何位置计数）；
- `review_count.status`：`actionable` / `blocked` / `completed`；
- `op`：`eq`（等于）/ `lte`（不超过）/ `gte`（不少于），`value` 为非负整数阈值。

**整条拒绝**的条件（422，`detail.errors` 一次性列出全部问题，带 `$...` JSON
路径定位，本次修订不落库）：

- 未知字段（节点上出现五种键以外的字段，或叶子里出现 status/op/value 以外字段）；
- 节点无法识别（不是对象、缺少类型字段、同时带多个类型字段）；
- `all` / `any` 为空组合或子项不是节点；`not` 的子项不是恰好一个节点；
- 叶子 status / op 超出取值域、value 不是非负整数、或缺少必需字段；
- 树深超过 **8**（叶子深度为 1，每层组合加 1）；节点总数超过 **64**。

**评估**（`GET /documents/{id}/gate/evaluate?policy=&revision=&version=`）：
策略修订、目标版本（缺省最新版）、目标版上全部批注的生效位置状态、当前审阅
状态在**同一个 SQLite 读事务**（同一快照）内读出。响应为：

- `passed`：根节点布尔结论；
- `explanation`：与策略**同形**的解释树，每个节点都带 `result`；
  `all` / `any` **不短路**——无论兄弟节点结果如何，每个子节点都被完整评估并
  出现在解释树中；
- 叶子解释为 `{status, op, value, actual, annotation_ids}`：`actual` 是实际计数，
  `annotation_ids` 是参与计数的批注 ID（按编号升序）；
- `revision` / `version` 缺省分别取最新修订 / 最新版本；策略、修订、版本或
  文档不存在返回 404。

相同快照（同一策略修订、同一目标版本、同一审阅状态）重复评估，结果及各
ID 列表顺序必然相同。

### 批注驱动的脱敏副本

`POST /documents/{id}/redact` 在**不写入任何数据**的前提下，按指定版本上
批注的生效位置生成脱敏文本。请求体：

| 字段 | 含义 |
|---|---|
| `version_number` | 目标文档版本号（取批注在**该版**的生效位置） |
| `annotation_ids` | 要遮盖的批注 ID 集合（非空；重复 ID 整批拒绝） |
| `placeholder` | 占位文本（非空，按 Unicode 码点整体替换选区） |

**整批校验**——以下任一情况都返回 422，`detail.errors` 一次性列出全部问题
（不生成任何脱敏内容），每条错误带 `error` 机器码、`message` 中文说明、
`path` 请求定位（`annotation_ids[i]`）与 `annotation_id`：

| 情况 | 错误码 |
|---|---|
| 批注 ID 在请求中重复 | `duplicate_annotation` |
| 批注不存在 | `annotation_not_found` |
| 批注属于其他文档（跨文档） | `cross_document` |
| 目标版本上没有迁移记录（如批注创建于更晚版本） | `location_missing` |
| 生效位置为 `ambiguous`（含歧义顺延占位）/ `unresolved` | `not_effective` |
| 生效范围缺失或越界（防御性校验） | `invalid_range` |

文档或目标版本不存在返回 404；`annotation_ids` 为空、占位为空等请求结构
问题由 schema 直接返回 422。

**替换与合并**：版本全文与全部生效位置在**同一个 SQLite 读事务**（同一快照）
内读出。人工确认过的位置以确认范围（生效状态 `confirmed`）为准。

- 重叠或**首尾相接**（`next.start == prev.end`）的范围合并为**最大区间**，
  合并区间携带覆盖它的全部批注 ID（升序去重）；
- 合并区间按**起点升序**，用同一个占位文本逐个替换：**每个占位只出现一次**；
- 响应 `placeholders[i]` 列出该次占位的文本、在结果文本中的码点半开区间
  `[result_start, result_end)` 以及覆盖它的 `annotation_ids`。

**可追溯映射**：响应 `fragments` 按顺序铺满源文本与结果文本，二者均从 0
开始、相邻片段首尾相接：

- `{"kind": "kept", source_start, source_end, result_start, result_end}`：
  未遮盖片段，源区间与结果区间等长（未遮盖片段同样可追溯）；
- `{"kind": "redacted", ...source/result 区间, annotation_ids, placeholder_index}`：
  遮盖片段，源区间是被遮盖原文，结果区间对应占位文本。

全部偏移均为 **Unicode 码点半开区间**（emoji 等 BMP 外字符算 1 个码点）。
用 `fragments` 即可从源文本重建脱敏文本，或反向核对每段原文的去向。
计算完全确定：相同快照（同一版本内容、同一批批注位置）与相同请求，
响应序列化后**字节一致**（与请求中 ID 的排列顺序无关）。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/documents` | 新建文档，`content` 即基准文本（第 1 版） |
| GET | `/documents/{id}` | 文档详情与版本列表 |
| POST | `/documents/{id}/annotations` | 在最新版上创建批注（校验锚点） |
| GET | `/documents/{id}/annotations` | 全部批注及最新生效状态 |
| GET | `/documents/{id}/annotations/{aid}` | 单条批注详情 |
| GET | `/documents/{id}/annotations/{aid}/history` | 该批注跨全部版本的范围/状态/匹配依据 |
| POST | `/documents/{id}/annotations/{aid}/confirmations` | 人工确认歧义（选定候选范围） |
| POST | `/documents/{id}/versions` | 提交新版全文，触发全部批注的续接迁移 |
| GET | `/documents/{id}/versions` | 版本列表 |
| GET | `/documents/{id}/versions/{n}` | 某版全文及该版全部迁移结果 |
| GET | `/documents/{id}/compare?from_version=&to_version=` | 任意两版间每条批注的范围、状态与匹配依据 |
| GET | `/documents/{id}/impact?from_version=&to_version=` | 批注改动影响分析：码点级编辑片段 + 起始版每条批注范围到目标版的投影与分类 |
| POST | `/documents/{id}/dependencies` | 批量提交审阅依赖关系（整批校验跨文档引用/自依赖/重复边/环，失败则全部不落库） |
| GET | `/documents/{id}/dependencies` | 文档下全部审阅依赖边 |
| GET | `/documents/{id}/review-plan` | 审阅计划：可处理/被阻塞/已完成分类 + 按拓扑层级的可并行批次 |
| POST | `/documents/{id}/annotations/{aid}/completions` | 标记批注完成（只追加、幂等，立即解锁后续批注） |
| GET | `/documents/{id}/completions` | 文档下全部完成记录（只追加） |
| GET | `/documents/{id}/archive` | 同一读事务导出规范 JSON 归档与 SHA-256 摘要 |
| POST | `/archives/verify` | 离线核验归档（不查库、不写入；缺行/串文档/篡改/摘要不符返回可定位错误） |
| POST | `/policies` | 提交发布门禁策略（语法整树校验，按名称只追加修订） |
| GET | `/policies` | 全部策略的最新修订（按名称升序） |
| GET | `/policies/{name}` | 某策略的全部修订（按修订号升序） |
| GET | `/policies/{name}/revisions/{revision}` | 取某策略的指定修订 |
| GET | `/documents/{id}/gate/evaluate?policy=&revision=&version=` | 同一读事务快照评估门禁，返回不短路的同形解释树 |
| POST | `/documents/{id}/redact` | 批注驱动的脱敏副本：合并生效范围替换为占位，返回脱敏全文与源/结果码点区间映射（只读） |

## 完整示例

```bash
B=http://localhost:8000

# 1. 新建文档（基准文本为第 1 版）
curl -X POST $B/documents -H 'Content-Type: application/json' -d '{
  "title": "产品需求文档",
  "content": "第一段：天气很好。第二段：心情不错。第三段：天气很好。"
}'
# → {"document_id": 1, ...}

# 2. 创建批注：选区“心情不错”位于码点偏移 [13, 17)
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d '{
  "body": "建议润色",
  "selection": "心情不错",
  "left_context": "第二段：",
  "right_context": "。",
  "start": 13, "end": 17
}'
# → {"annotation_id": 1, "location": {"status": "resolved", ...}}

# 3. 提交第 2 版：选区平移，唯一落点 → resolved
curl -X POST $B/documents/1/versions -H 'Content-Type: application/json' -d '{
  "content": "新增开头。第一段：天气很好。第二段：心情不错。第三段：天气很好。"
}'
# → results[0]: {"status": "resolved", "start": 18, "end": 22, "score": 5, ...}

# 4. 第 3 版出现两处相同上下文 → ambiguous，返回全部候选范围
curl -X POST $B/documents/1/versions -H 'Content-Type: application/json' -d '{
  "content": "第二段：心情不错。第二段：心情不错。"
}'
# → results[0]: {"status": "ambiguous",
#     "candidates": [{"start":4,"end":8,"score":5,...},
#                    {"start":13,"end":17,"score":5,...}]}

# 5. 歧义未确认时提交第 4 版 → 顺延占位，不参与自动迁移
curl -X POST $B/documents/1/versions -H 'Content-Type: application/json' -d '{
  "content": "第二段：心情不错。"
}'
# → results[0]: {"status": "ambiguous", "carried_forward": true}

# 6. 批注改动影响分析（确认前调用）：起始版位置歧义未确认 → indeterminate，不猜测范围
curl "$B/documents/1/impact?from_version=3&to_version=2"
# → annotations[0]: {"classification":"indeterminate",
#     "reason":"起始版第 3 版中该批注位置歧义且未人工确认（存在多个并列最高分的候选范围），不提供投影范围",
#     "from":{"status":"ambiguous","start":null,"end":null,...},"to":null,"hits":[]}

# 7. 人工确认第 3 版的歧义：选定候选 (13, 17)
curl -X POST $B/documents/1/annotations/1/confirmations \
  -H 'Content-Type: application/json' -d '{
  "version_number": 3, "start": 13, "end": 17, "note": "取后一处"
}'
# → location.status = "confirmed"

# 8. 确认后再提交新版 → 恢复自动迁移
curl -X POST $B/documents/1/versions -H 'Content-Type: application/json' -d '{
  "content": "第二段：心情不错。"
}'
# → results[0]: {"status": "resolved", "start": 4, "end": 8}

# 9. 查询任意两版间每条批注的范围、状态与匹配依据
curl "$B/documents/1/compare?from_version=1&to_version=5"

# 10. 单条批注的完整跨版本历史
curl $B/documents/1/annotations/1/history

# 11. 批注改动影响分析：第 1 版 → 第 2 版（开头插入 5 码点，选区整体平移）
curl "$B/documents/1/impact?from_version=1&to_version=2"
# → segments: [{"index":0,"kind":"insert","from_start":0,"from_end":0,
#     "to_start":0,"to_end":5,"from_text":"","to_text":"新增开头。"}]
# → annotations[0]: {"classification":"shifted",
#     "from":{"status":"resolved","start":13,"end":17,...},
#     "to":{"start":18,"end":22},"hits":[]}
```

影响分析的分类全集示例（unchanged / modified / deleted / shifted 各一条）：

```bash
# 基准文本（第 1 版）：甲：目标一。乙：目标二。丙：目标三。丁：目标四。
# 依次对“目标一/二/三/四”创建 4 条批注后，提交第 2 版：
#   甲：目标一。乙：目X标二。丁：目标四。
curl "$B/documents/2/impact?from_version=1&to_version=2"
# → segments: [{"index":0,"kind":"insert","from_start":9,"from_end":9,
#                "to_start":9,"to_end":10,"from_text":"","to_text":"X"},
#              {"index":1,"kind":"delete","from_start":11,"from_end":17,
#                "to_start":12,"to_end":12,"from_text":"。丙：目标三","to_text":""}]
# → annotations:
#     目标一 [2,5)  → unchanged，投影 [2,5)，hits []
#     目标二 [8,11) → modified，投影 [8,12)，hits [插入 X]（选区内部插入）
#     目标三 [14,17)→ deleted， 投影 [12,12)（折叠到删除处），hits [删除"。丙：目标三"]
#     目标四 [20,23)→ shifted， 投影 [15,18)，hits []（仅被区间外编辑平移）
# → summary: {"unchanged":1,"shifted":1,"modified":1,"deleted":1,"indeterminate":0}
```

审阅依赖编排完整流程示例：

```bash
B=http://localhost:8000

# 1. 新建文档并创建 4 条批注（甲/乙/丙/丁，编号依次为 1..4）
curl -X POST $B/documents -H 'Content-Type: application/json' -d '{
  "title": "审阅示例", "content": "甲乙丙丁"
}'
# → {"document_id": 1, ...}
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d \
  '{"body": "审阅甲", "selection": "甲", "start": 0, "end": 1}'   # → annotation_id 1
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d \
  '{"body": "审阅乙", "selection": "乙", "start": 1, "end": 2}'   # → annotation_id 2
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d \
  '{"body": "审阅丙", "selection": "丙", "start": 2, "end": 3}'   # → annotation_id 3
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d \
  '{"body": "审阅丁", "selection": "丁", "start": 3, "end": 4}'   # → annotation_id 4

# 2. 批量提交依赖：甲→丙、乙→丙、丙→丁（一次请求整批写入）
curl -X POST $B/documents/1/dependencies -H 'Content-Type: application/json' -d '{
  "relations": [
    {"prerequisite_id": 1, "dependent_id": 3},
    {"prerequisite_id": 2, "dependent_id": 3},
    {"prerequisite_id": 3, "dependent_id": 4}
  ]
}'
# → 201 {"created_count": 3, "dependencies": [...]}

# 3. 再提交「丁→甲」会与已落库边构成环 → 422，整批不落库，
#    返回以环中最小编号开头的闭环路径
curl -X POST $B/documents/1/dependencies -H 'Content-Type: application/json' -d '{
  "relations": [{"prerequisite_id": 4, "dependent_id": 1}]
}'
# → 422 {"detail": {"message": "依赖关系存在环：1 → 3 → 4 → 1",
#                   "cycle": [1, 3, 4, 1]}}

# 4. 审阅计划：甲、乙可处理；丙被 [1,2] 阻塞；丁被 [1,2,3] 阻塞
curl $B/documents/1/review-plan
# → summary: {"total":4,"completed":0,"actionable":2,"blocked":2,"batch_count":3}
# → blocked[0]: {"annotation_id":3, "incomplete_prerequisites":[1,2], ...}
# → blocked[1]: {"annotation_id":4, "incomplete_prerequisites":[1,2,3], ...}
# → batches: [{"level":0,"annotation_ids":[1,2]},
#             {"level":1,"annotation_ids":[3]},
#             {"level":2,"annotation_ids":[4]}]

# 5. 被阻塞的丁不可完成 → 409
curl -X POST $B/documents/1/annotations/4/completions \
  -H 'Content-Type: application/json' -d '{}'
# → 409 {"detail": {"message": "批注 4 被阻塞：存在尚未完成的前置批注",
#                   "incomplete_prerequisites": [1, 2, 3]}}

# 6. 完成前置批注：完成甲不解锁丙（乙未完成）；完成乙立即解锁丙
curl -X POST $B/documents/1/annotations/1/completions \
  -H 'Content-Type: application/json' -d '{"note": "通过"}'
# → 201 {"already_completed": false, "unlocked_annotation_ids": []}
curl -X POST $B/documents/1/annotations/2/completions \
  -H 'Content-Type: application/json' -d '{}'
# → 201 {"already_completed": false, "unlocked_annotation_ids": [3]}

# 7. 重复提交完成记录幂等：返回 200 与原记录，不新增行
curl -X POST $B/documents/1/annotations/2/completions \
  -H 'Content-Type: application/json' -d '{}'
# → 200 {"already_completed": true, "unlocked_annotation_ids": []}

# 8. 查看全部依赖边与完成记录（均只追加）
curl $B/documents/1/dependencies
curl $B/documents/1/completions
```

迁移归档导出与离线核验示例（沿用前面含歧义、顺延与确认的文档 1）：

```bash
B=http://localhost:8000

# 1. 导出：同一读事务取得元数据 / 全部版本 / 批注 / 迁移记录 / 人工确认
#    归档保存到 archive.json，摘要另行保存（只提取 digest 字符串）
curl -s $B/documents/1/archive -o archive.json
python3 -c "import json; print(json.load(open('archive.json'))['digest'])" > digest.txt
cat archive.json | python3 -m json.tool | head -20
# → {
#       "document_id": 1,
#       "format": "annotation-archive/1",
#       "digest": "9f2c...（64 位十六进制 SHA-256）",
#       "archive": {"format": "annotation-archive/1", "document": {...},
#                   "versions": [...], "annotations": [...],
#                   "locations": [...], "confirmations": [...]},
#       ...
#   }

# 2. 核验：把归档与导出时另行保存的摘要一起提交（不查当前数据库）
python3 - <<'PY'
import json, urllib.request
body = json.dumps({
    "archive": json.load(open("archive.json"))["archive"],
    "digest": open("digest.txt").read().strip(),
}).encode("utf-8")
req = urllib.request.Request(
    "http://localhost:8000/archives/verify", data=body,
    headers={"Content-Type": "application/json"})
print(urllib.request.urlopen(req).read().decode())
# → {"valid":true,"format":"annotation-archive/1","document_id":1,
#    "digest":"9f2c...","counts":{"versions":5,"annotations":1,
#                                 "locations":5,"confirmations":1}}
PY

# 3. 完全离线（不经过本服务）自行复算摘要核对
python3 - <<'PY'
import hashlib, json
data = json.load(open("archive.json"))
canonical = json.dumps(data["archive"], ensure_ascii=False,
                       sort_keys=True, separators=(",", ":"))
actual = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
print("摘要一致" if actual == open("digest.txt").read().strip() else "摘要不符")
PY

# 4. 篡改一例：改掉某版全文后用原摘要核验 → 422，定位 digest_mismatch
python3 - <<'PY'
import json, urllib.error, urllib.request
data = json.load(open("archive.json"))
data["archive"]["versions"][0]["content"] += "被人改过"
req = urllib.request.Request(
    "http://localhost:8000/archives/verify",
    data=json.dumps({"archive": data["archive"],
                     "digest": open("digest.txt").read().strip()}).encode("utf-8"),
    headers={"Content-Type": "application/json"})
try:
    urllib.request.urlopen(req)
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode())
# → 422 {"detail":{"valid":false,"errors":[
#        {"error":"digest_mismatch","message":"归档内容与摘要不符：……",
#         "path":"archive"}]}}
PY

# 5. 内容缺失一例：删掉一条迁移记录并用新摘要提交（模拟缺行的归档）
#    → 422，errors[0] 定位到具体批注与缺失版本
#    {"error":"missing_location",
#     "message":"批注 1 缺少第 4 版的迁移记录（该批注创建于第 1 版，末版为第 5 版）",
#     "path":"annotations[0]"}
```

文档发布门禁完整流程示例：

```bash
B=http://localhost:8000

# 1. 新建文档「甲乙丙」并对三个字各建一条批注（批注 1/2/3），提交第 2 版：
#    甲、乙唯一落点 → resolved；丙在新版中消失 → unresolved
curl -X POST $B/documents -H 'Content-Type: application/json' -d '{
  "title": "门禁示例", "content": "甲乙丙"
}'
i=0
for ch in 甲 乙 丙; do
  curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d "{
    \"body\": \"批注$ch\", \"selection\": \"$ch\",
    \"start\": $i, \"end\": $((i+1))
  }"
  i=$((i+1))
done
curl -X POST $B/documents/1/versions -H 'Content-Type: application/json' -d '{
  "content": "甲乙丁"
}'

# 2. 提交发布策略「放行条件」：至少 2 条 resolved，且没有 unresolved；
#    all / not / location_count 各节点恰有一个类型键，叶子字段固定
curl -X POST $B/policies -H 'Content-Type: application/json' -d '{
  "name": "放行条件",
  "strategy": {
    "all": [
      {"location_count": {"status": "resolved",   "op": "gte", "value": 2}},
      {"not": {"location_count": {"status": "unresolved", "op": "gte", "value": 1}}}
    ]
  }
}'
# → 201 {"name":"放行条件","revision":1,"strategy":{...}}

# 3. 在第 2 版上评估：有 1 条 unresolved → passed=false；
#    all 不短路：两个子节点的解释都在；叶子列出实际值/阈值/参与计数的批注 ID
curl "$B/documents/1/gate/evaluate?policy=%E6%94%BE%E8%A1%8C%E6%9D%A1%E4%BB%B6"
# → {"document_id":1,"policy_name":"放行条件","policy_revision":1,
#    "version_number":2,"passed":false,
#    "explanation":{"all":[
#      {"location_count":{"status":"resolved","op":"gte","value":2,
#                         "actual":2,"annotation_ids":[1,2]},"result":true},
#      {"not":{"location_count":{"status":"unresolved","op":"gte","value":1,
#                                "actual":1,"annotation_ids":[3]},"result":false},
#      ],"result":false}}

# 4. 策略只按名称追加修订：同名再提交得到 revision=2，旧修订原样保留
curl -X POST $B/policies -H 'Content-Type: application/json' -d '{
  "name": "放行条件",
  "strategy": {"all": [
    {"location_count": {"status": "resolved", "op": "gte", "value": 3}},
    {"review_count":   {"status": "blocked",  "op": "eq",  "value": 0}}
  ]}
}'
curl "$B/policies/%E6%94%BE%E8%A1%8C%E6%9D%A1%E4%BB%B6"
# → {"name":"放行条件","revisions":[{"revision":1,...},{"revision":2,...}]}

# 评估时可锁定历史修订与历史版本（默认均取最新）
curl "$B/documents/1/gate/evaluate?policy=%E6%94%BE%E8%A1%8C%E6%9D%A1%E4%BB%B6&revision=1&version=2"

# 5. 非法策略整条拒绝：422 一次性给出带 JSON 路径的全部错误，本次修订不落库。
#    下例同时犯了「空组合」与「review 叶子用了 location 状态」两个错误：
curl -X POST $B/policies -H 'Content-Type: application/json' -d '{
  "name": "坏策略",
  "strategy": {"all": [
    {"review_count": {"status": "resolved", "op": "eq", "value": 0}}
  ], "extra": 1}
}'
# → 422 {"detail":{"message":"门禁策略 '坏策略' 校验失败，本次修订未落库",
#                  "errors":[{"error":"unknown_field","path":"$",...}, ...]}}
# 其他典型拒绝：{"all":[]}（empty_combination，$.all）、
# {"not":[]}（invalid_not_child，$.not）、深度 > 8（depth_exceeded）、
# 节点 > 64（nodes_exceeded）、叶子 value 为 -1 / true / 1.5（invalid_value）。
```

批注驱动的脱敏副本示例：

```bash
B=http://localhost:8000

# 1. 新建文档并创建 3 条批注：姓名（1）、姓名中的一部分（2，与 1 重叠）、城市（3）
curl -X POST $B/documents -H 'Content-Type: application/json' -d '{
  "title": "脱敏示例",
  "content": "联系人：张三丰，城市：武当山。"
}'
# “张三丰”位于 [4,7)
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d '{
  "body": "姓名", "selection": "张三丰", "start": 4, "end": 7
}'
# “张三”位于 [4,6)，与批注 1 重叠
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d '{
  "body": "姓氏", "selection": "张三", "start": 4, "end": 6
}'
# “武当山”位于 [11,14)
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d '{
  "body": "城市", "selection": "武当山", "start": 11, "end": 14
}'

# 2. 生成脱敏副本：重叠的 [4,7)/[4,6) 合并为一个最大区间，只替换一次；
#    占位每次出现都在 placeholders 中列出覆盖它的批注 ID
curl -X POST $B/documents/1/redact -H 'Content-Type: application/json' -d '{
  "version_number": 1,
  "annotation_ids": [3, 1, 2],
  "placeholder": "【密】"
}'
# → 200
# {"document_id":1,"version_number":1,"placeholder":"【密】",
#  "annotation_ids":[1,2,3],"source_length":15,"redacted":"联系人：【密】，城市：【密】。",
#  "redacted_interval_count":2,
#  "fragments":[
#    {"index":0,"kind":"kept","source_start":0,"source_end":4,"result_start":0,"result_end":4},
#    {"index":1,"kind":"redacted","source_start":4,"source_end":7,"result_start":4,"result_end":7,
#     "annotation_ids":[1,2],"placeholder_index":0},
#    {"index":2,"kind":"kept","source_start":7,"source_end":11,"result_start":7,"result_end":11},
#    {"index":3,"kind":"redacted","source_start":11,"source_end":14,"result_start":11,"result_end":14,
#     "annotation_ids":[3],"placeholder_index":1},
#    {"index":4,"kind":"kept","source_start":14,"source_end":15,"result_start":14,"result_end":15}],
#  "placeholders":[
#    {"index":0,"placeholder":"【密】","annotation_ids":[1,2],"result_start":4,"result_end":7},
#    {"index":1,"placeholder":"【密】","annotation_ids":[3],"result_start":11,"result_end":14}]}
# 注：区间均为 Unicode 码点半开区间；fragments 中 kept / redacted 各自铺满源文本与结果文本，
#    未遮盖片段（kind=kept）同样可追溯；请求 ID 顺序不影响结果（响应中一律升序）。

# 3. 整批校验：重复 ID、批注不存在 / 跨文档、位置未就绪一次性全部定位返回
curl -X POST $B/documents/1/redact -H 'Content-Type: application/json' -d '{
  "version_number": 1,
  "annotation_ids": [1, 1, 999],
  "placeholder": "X"
}'
# → 422 {"detail":{"message":"脱敏请求校验失败，整批未处理（未生成脱敏副本）",
#                  "errors":[
#   {"error":"duplicate_annotation","path":"annotation_ids[1]","annotation_id":1,...},
#   {"error":"annotation_not_found","path":"annotation_ids[2]","annotation_id":999,...}]}}

# 4. ambiguous / unresolved（含歧义顺延占位）不猜测范围 → 422 not_effective；
#    人工确认后生效状态为 confirmed，即可用确认范围脱敏：
curl -X POST $B/documents/1/redact -H 'Content-Type: application/json' -d '{
  "version_number": 1, "annotation_ids": [1], "placeholder": ""
}'
# → 422（schema 拒绝空占位）

# 5. 只读：脱敏不改变任何数据；相同快照 + 相同请求多次调用结果字节一致。
```

## 项目结构

```
app/
  main.py       # FastAPI 路由与迁移流程（含改动影响分析、审阅依赖编排、归档导出/核验、发布门禁、批注驱动脱敏接口）
  matching.py   # 锚点匹配核心（枚举落点、上下文打分、排序判定）
  diffing.py    # 两版全文的码点级编辑片段、范围投影与分类（纯函数）
  redacting.py  # 脱敏副本：生效范围合并为最大区间、按起点替换与源/结果码点区间映射（纯函数）
  review.py     # 审阅依赖图算法：环检测、传递前置、拓扑分层（纯函数）
  gate.py       # 发布门禁策略树：语法校验（深度/节点数/枚举域）与快照评估、同形解释树（纯函数）
  archive.py    # 迁移归档：同一读事务导出、规范化 JSON + SHA-256、离线一致性核验（不查库）
  db.py         # SQLite 连接、建表、读写事务
  schemas.py    # 请求体模型（含门禁策略、归档结构与核验请求模型）
tests/
  test_matching.py  # 匹配算法单元测试
  test_diffing.py   # 编辑片段/投影/分类单元测试
  test_api.py       # 端到端流程测试（迁移/歧义/顺延/确认/对比）
  test_impact.py    # 改动影响分析接口端到端测试
  test_review.py    # 审阅依赖编排测试（依赖校验/环检测/审阅计划/完成记录）
  test_gate.py      # 发布门禁测试（策略语法拒绝/修订只追加/同事务快照评估/不短路解释树/稳定性）
  test_redacting.py # 批注驱动脱敏测试（范围合并/首尾相接/码点映射/整批可定位错误/字节稳定性/只读）
  test_archive.py   # 归档导出与离线核验测试（摘要篡改/缺行/串文档/顺延/确认非法）
Dockerfile
docker-compose.yml
requirements.txt        # 运行依赖
requirements-dev.txt    # 测试依赖
```
