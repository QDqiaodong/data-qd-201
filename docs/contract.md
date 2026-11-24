# 晶圆裸片数据契约

使用者：晶圆测试数据工程师。process(data) 是纯Python业务API，返回规范坐标裸片数据和可供选片步骤读取的 pick_coordinates；不操作设备。所有样例合成，无真实生产数据。

输入 wafer 为ASCII唯一批内标识。sites 为1..20000个唯一整数[x,y]坐标，每个分量范围±10000。runs 为1..20个必需测试，每项id唯一，matrix 为2×2整系数正交矩阵（系数-1/0/1，每行单位正交），offset 为整数坐标。规范位置 = matrix × 设备at + offset。不自动推断旋转、单位或原点；允许镜像，全部位置必须属于sites。

每个run的bins有1..100个ASCII代码，值为pass/fail/skip。records为0..20000条{at,bin}，设备坐标不可重复，bin必须声明；总记录<=200000。即使重复记录内容一致也拒绝，不隐式作复测；不存在的记录是missing，skip是实际观测的另一分类，不能混淆。

任一测试fail则裸片status=fail；无fail但有missing或skip则incomplete；全部pass才pass且纳入pick_coordinates。blocked_by保留每个非pass原因，即使fail已主导结果也保留缺测线索。die_id由wafer和规范坐标组成；裸片按y再x排序，观测按runs声明顺序，源坐标与bin保留。没有库存、订单、良率看板、复测优先级或时间版本。后续复测政策、reticle分组、选片图适配可复用run/die/bin身份。

严格拒绝未知字段、JSON重复键、布尔充当整数等非法值。io.publish仅创建新JSON，目标存在或符号链接时失败，计算错误不发布。examples/run_sample.py只是固定样例调用，不提供通用命令行产品。

## process_calibrated(data) 校准融合契约

离线纯Python API（`wafer_fusion.process_calibrated`），不连接设备、不引入第三方运行时依赖，不修改入参。用于设备复位后每道测试只拿到若干候选坐标变换、并由相邻工位基准点links约束的场景。旧 `process(data)` 的输入、排序、异常与返回结构保持不变；新API沿用同一套规范裸片身份、bin语义（pass/fail/skip）与fail优先级。

### 输入

顶层恰为 `wafer`、`sites`、`runs`、`links`。wafer/sites 与旧契约同边界（1..20000个唯一sites，坐标分量±10000）。runs仍按声明顺序表示工位顺序（1..20道），每道测试恰含 `id`、`bins`、`records`、`candidates`；bins、records沿用旧结构、旧校验与旧条数限制（单测试0..20000条、设备坐标重复拒绝、未知bin拒绝、全部测试合计≤200000条），skip与缺测（missing）仍严格区分。

每道测试有1..8个candidates，每个恰含 `id`、`matrix`、`offset`。候选ID在同一测试内唯一，变换（matrix/offset组合）在同一测试内唯一；不同测试可复用同一候选ID。matrix/offset仍按旧接口校验：2×2整系数正交矩阵（系数-1/0/1、行单位且互相正交），允许镜像；offset为±10000整数坐标。结构非法的候选或记录一律拒绝，即使它最终不参与结果。候选若把该测试任一记录映射到未声明的sites位置，则该候选被排除、不参与校准，但不连累同测试的其他候选。

links为0..256条已确认基准点对应，每项恰含 `left`、`right`，两端各为 `{"run": 测试ID, "at": [x,y]}`，at为该设备本地整数坐标、与records同边界。left工位必须早于right工位，且两者在runs中的序号差≤2（三站有效窗口）。同一对端点不可重复；同一对工位允许多条不同对应。基准点不必是裸片、不必出现在records中，只比较两端变换后的规范坐标是否相等，不检查其是否属于sites。引用未知run、顺序颠倒、超窗、坐标越界或重复端点均抛 `ValueError`。

### 可行校准与“可能”的口径

一种可行校准必须为每道测试选恰好一个未被排除的候选，并同时满足全部links；不允许按裸片在同一测试内拼用不同变换。测试bin不用于淘汰候选。不存在任何可行校准时抛 `ValueError`（而不是返回零颗可选的成功结果）；任何非法字段、ID、值、引用或重复同样抛 `ValueError`。

输出中每个“可能”状态都必须由一套完整、同时满足全部校准证据的变换组合见证；各道测试分别可用的候选不能随意拼接。引擎在工位序号图（带宽2）上做成对约束动态规划，候选支持集与每颗裸片的可达状态都由完整校准的可行性查询得出，不是局部逐站判断。

### 输出

可JSON序列化字典，顶层恰为：

- `schema`：固定 `"wafer-calibration-v1"`。
- `wafer`：与输入一致的晶圆标识。
- `supported_candidates`：按runs声明顺序，每项 `{"run": 测试ID, "candidates": [候选ID,...]}`，只列至少出现在一种可行校准中的候选，ID按字典序；被越界排除或被links淘汰的候选不出现。
- `dies`：按先y后x排序，每项恰含 `die_id`、`at`、`possible_statuses`。die_id与at沿用旧口径。单种校准下：任一测试fail则fail；无fail但有skip或missing则incomplete；全部pass才pass。possible_statuses是该裸片在所有可行校准中实际可达状态的去重集合，按 `pass`、`incomplete`、`fail` 的相对顺序输出。
- `pick_coordinates`：只列possible_statuses恰为 `["pass"]` 的裸片坐标，顺序与dies一致。校准仍不唯一不会扣下在每种可行校准下都pass的坐标；只要存在一种符合证据的解释使裸片fail或缺测，该坐标就不会被选片程序收到。

重排sites、records、candidates或links不改变输出。小规模结果必须全量核对；`tests/scale_regression.py` 给出可复现的单进程CPU时间规模回归（构造不计入，仅计 `process_calibrated` 调用，预算30秒），运行方式：`python -I tests/scale_regression.py`。

## 曝光图编译 API compile_exposure_map(data)（离线）

`wafer_fusion.compile_exposure_map` 把晶圆几何与逐层曝光记录编译成可直接作为 `process_calibrated` 输入 `sites` 的规范裸片坐标，同时为每个圆内候选保留曝光归属与排除原因。纯标准库、单进程、不连接设备、不引入第三方运行时依赖，不修改入参；非法输入一律抛 `ValueError`。既有 API 行为不变。

### 输入

顶层恰含 `wafer`、`radius`、`die`、`lattice`、`layers`：

- `wafer`：沿用仓库 ASCII ID 约定（`[A-Za-z0-9_:-]{1,64}`）。所有坐标以晶圆中心为原点，单位就是输入给出的整数坐标单位，不做单位换算。
- `radius`：1..10000 的正整数（布尔不是整数，拒绝）。
- `die`：恰含正整数 `width`、`height`，各不大于 20000。
- `lattice`：恰含整数二元数组 `origin`（分量 ±10000）与正整数二元数组 `pitch`（1..10000）。候选中心为 `origin + [i*pitch_x, j*pitch_y]`，i、j 为任意整数；原点、间距与全部生成坐标均落在 ±10000 边界内，越界结构拒绝。
- `layers`：1..8 个不同层 ID。每层恰含 `id`、`shots`，`shots` 为 1..128 个 shot；每个 shot 恰含层内唯一 `id`（ASCII ID）、`bounds=[min_x,min_y,max_x,max_y]`（四分量均为 ±10000 整数，严格 `min<max`，闭合矩形）和 `state`（仅 `ready`/`hold`）。层 ID、shot ID 重复均拒绝；未知字段、布尔充当整数、错误长度数组一律拒绝。

几何口径：候选只包括中心在圆内或圆周上（`x²+y² ≤ radius²`）的不同网格点，按中心 y 后 x 排序。圆内候选中心上限 100000，超限拒绝（正 pitch 下网格点天然互异，重复结构同样拒绝）。裸片是以候选中心为中心、边平行坐标轴的闭矩形；完整位于晶圆内当且仅当四个角都在圆内或圆周上。宽高允许奇数：全部比较在二倍整数坐标上进行（裸片边为 `2x±width`、`2y±height`，圆判据为 `(2x)²+(2y)² ≤ 4·radius²`），圆周相切判为在内，任何结论都不经过浮点舍入。

### 曝光归属与原因

shot 矩形闭合；一个 shot 完整覆盖某裸片当且仅当裸片闭矩形四边都落在 shot bounds 内（边界相切算覆盖）。shots 数组顺序没有优先级含义：同一裸片在一层被多个 shot 完整覆盖就是归属歧义，输出保留全部匹配 ID，绝不挑第一个、最近者或按 ID 消解。层身份只由层 ID 决定，输出一律按层 ID 排序；shot_ids 按字典序排列。即使裸片被晶圆边缘截断，仍逐shot计算并返回它与每一层的完整覆盖关系。

每个 die 项的 `reasons` 按固定顺序排列：先 `wafer_edge`（裸片任一角在圆外），再按层 ID 排列该层原因——无完整覆盖 shot 为 `{"layer","reason":"uncovered"}`；完整覆盖 shot 多于一个为 `ambiguous`（保留全部 ID）；唯一覆盖 shot 状态为 `hold` 为 `held`（记录该 shot ID 于 exposures）。一层的 hold 只影响实际被该 shot 唯一覆盖的裸片，不牵连同层其他 shot 或其他层；不完整、未覆盖、多重覆盖的位置全部留在 `dies`，不静默丢弃。

### 输出与判定

可 JSON 序列化字典，顶层恰为 `schema`（固定 `wafer-exposure-map-v1`）、`wafer`、`sites`、`dies`：

- `dies`：每个圆内候选中心一项，按 y 后 x 排序，恰含 `at`（二元整数数组）、`status`（`probeable`/`excluded`）、`exposures`（按层 ID 排序，每项恰含 `layer`、`shot_ids`）、`reasons`。
- 仅当裸片完整位于晶圆内、且每层恰有一个完整覆盖 shot 且这些 shot 全部 `ready` 时为 `probeable`；任一条件不满足为 `excluded`。
- `sites`：只含 `probeable` 裸片中心，顺序与 `dies` 中对应项一致，可原样作为 `process_calibrated` 的 `sites`（1..20000，超限拒绝）；`sites` 与 `probeable` 判定严格一致。
- 重排 `layers` 或 `shots` 不改变完整 JSON 结果；输入在成功或拒绝后均不被修改。

`tests/test_exposure.py` 全量核对小晶圆几何（含圆周相切与奇数宽高）、uncovered/ambiguous/held、原因排序、校验与入参不变性，并把 `sites` 接入 `process_calibrated` 验证坐标与顺序；`tests/exposure_scale_regression.py` 按固定规格构造 8 层、每层 128 个 shot、圆内候选 ≥80000 的合成输入，只计 `compile_exposure_map` 的进程 CPU 时间（`time.process_time`），预算 15 秒，全量（不采样、不截断）核对候选条数、`sites` 与 `probeable` 坐标完全相等以及重排不变性，运行方式：`python -I tests/exposure_scale_regression.py`。

## 复测档案 API WaferArchive（离线持久化）

`wafer_fusion.WaferArchive` 面向跨班次交接：一份本地档案目录只对应一份合法的 `process_calibrated` 首测输入，外加 links 修订史、签发工单与回传报告。纯标准库、单写者、不接设备、不引入第三方运行时依赖；非法业务输入一律抛 `ValueError` 且不修改调用方输入。档案状态为目录内单个 `archive.json`（schema `wafer-archive-v1`），写入采用同目录临时文件 + `fsync` + 原子替换 + 目录 `fsync`；被拒绝的写入或提交前中断不会留下可查询到的半条修订、半份报告或被改变的选片结果，重开时清理遗留临时文件。

### 建档与修订

- `WaferArchive.create(path, payload, revision_id)`：校验完整首测 payload（沿用全部既有结构、候选排除与可行校准约束），在新目录建档并保存初版 links 与调用方给定的初版修订 ID；目录已存在且内容与初版 ID 一致时重开返回同一档案，内容不一致抛错，绝不覆盖。
- `submit_revision(revision_id, links)`：以完整 links 替换当前对应关系，不允许改动 wafer、sites、runs 顺序、候选变换、bin 表或首测记录；新修订按原校准约束重新求解，无可行解释则拒绝、旧版继续有效。档案至多 256 个修订（含初版）。
- 修订 ID 是业务身份而非内容指纹：不同 ID 即使 links 相同也是两个修订；同 ID 重交相同 links（行顺序不计）取回原修订回执且**不**改变当前版；同 ID 内容不同拒绝。
- 一版的校准解释只由首测记录与该版 links 决定。复测记录不参与候选淘汰：融合引擎始终基于首测数据构建（越界候选排除、links 约束与可行校准均不含复测信息），复测只替换逐候选标签查找所用的本地记录映射，因此不能先用复测把越界候选去掉再证明工单合法。

### 签发（下发）

`issue_order({"order_id", "run_id", "dies"})`：工单 ID 在档案内唯一，run 必须存在，`dies` 为非空规范裸片坐标集合（顺序不计，重复拒绝，必须都是声明的 sites）。不得拆单或扩大范围。签发条件：在当前修订的**每一种完整可行校准**下，批准集合都能被同一组符合旧坐标边界（分量 ±10000）的本地坐标精确覆盖。实现上只需对该工位每个“受支持候选”（至少属于一套完整可行校准，即引擎 supported 掩码内的候选）求批准集合的逆像：所有受支持候选的逆像恰好是同一本地集合时签发；只检查多解释并集、或把各站局部候选任意拼接都不满足口径。整组一致即可，不要求组内每个本地坐标对应唯一裸片。

回执含 `order_id`、单调递增 `serial`（档案内从 1 开始）、`run_id`、规范排序（y 后 x）的 `approved` 与 `local_pins`、`revision_id`；单据此后不变。同 ID 同请求（集合顺序不计）重放取原单据，不重新认定；同 ID 不同请求（不同 run 或不同裸片集合）拒绝。档案至多 1024 份工单；全部已签发清单累计至多 200000 行，重叠位置在这项预算中分别计数。坐标配额按一片晶圆上各工位“首测位置 ∪ 已签发全部落针位置”的并集计：每站 20000、全片 200000；超额的新操作拒绝，不影响已有请求重放。

### 收件（报告）

`receive_report(order_id, rows)`：每份工单至多一份最终报告，行结构沿用首测的 `{"at", "bin"}`。报告必须恰好覆盖该工单下发清单的全部本地位置（不多不少、无重复位置），bin 必须属于该工位 bin 表；未知工单、短缺/额外位置、重复位置、未知 bin、与已存报告内容不同均拒绝。行顺序不计：重交同一份报告返回原收件结果，不生成第二份。即使工单在当前修订下已不能证明范围，形状正确的报告仍原样存档（历史可取回），只是不参与当前选片；没有报告的工单不替换任何观测。

### 查询：当前结果与历史单据

- `current_result()` 每次按**当前修订**重新认定每份工单：对每种完整可行校准（实现上对每个受支持候选）复核签发时的整组落针是否仍恰好映到原批准集合；不成立则**整份**工单暂停，不能只留看似未受影响的行，证据恢复后无需重交即可重新生效。
- 返回 schema `wafer-archive-result-v1`，包含：`revision_id`（当前修订）；与 `process_calibrated` 同义的 `supported_candidates`、`dies`（按 y 后 x，`possible_statuses` 口径完全一致）、`pick_coordinates`（只有在所有可行校准下都 pass 的裸片）；`work_orders` 按签发序号给出每份工单的 `order_id/serial/run_id/issued_revision_id/scope_holds/reported`，下游据此区分“尚未返回”与“已收但不可用”；`effective_retests` 按 runs 声明顺序、再按本地 y 后 x 列出每条生效复测行的 `run/at/bin/source_order` 以便核查回退。
- 生效行选取：仅从 `scope_holds` 且已收件的工单中，为每个 `(run, 本地坐标)` 取**签发序号最大**的报告行，与收件顺序无关；较新工单无报告或被整体停用时，较早的有效行重新露出。没有复测覆盖的本地位置回退首测记录，首测也没有则为缺测；复测 bin 完整替换被覆盖位置的旧读数（skip 可以替换 fail），同一位置的新旧读数绝不合并统计。
- `history_order(order_id)` 取回不可变的原始工单（含签发修订、批准集合、本地清单）与已存档的原始报告；后续认定变化不写入旧单据。重取旧单不代表可以重新下发。

### 重开、重放与失败边界

`WaferArchive(path)` 重新打开既有档案；调用按顺序执行，不要求多写者。合法写入在返回成功前持久生效；提交完成但调用方丢失应答时，用同一 ID 重试（建档、修订、签发、收件均幂等）取回原结果，后来的修订或收件不改变其历史返回含义。所有对外坐标与 ID 的校验沿用既有边界；返回结构均可 JSON 序列化。本 API 不提供通用 CLI、界面或设备连接。

## 首测收件与交接账本 FirstTestLedger（离线）

`wafer_fusion.FirstTestLedger` 负责探针台首测的离线收件：设备先把原始读数写进自身缓冲，再分批、乱序经同一传输通道送到接收端，同一通道可能交错混入不同晶圆的读数。账本把读数收下、按片封账成可独立建档的交接包，并维护设备唯一允许执行的通道清理游标。纯标准库、单写者、按序调用，不接真实设备，不引入第三方运行时依赖；非法业务输入一律抛 `ValueError`，不修改调用方输入。

### 账本、源身份与收件

- `FirstTestLedger.create(path, pending_limit)`：新建账本或重开既有账本。`pending_limit` 为跨晶圆共用的待封账行数上限，正整数；重开时上限不同即拒绝。状态为目录内单个 `ledger.json`（schema `wafer-first-test-ledger-v1`）。
- `FirstTestLedger(path)` 重开既有账本。
- 读数结构恰为 `{stream, seq, wafer, run, at, bin}`。`(stream, seq)` 是账本内的源身份，`stream` 为 ASCII ID，`seq` 为正的 64 位有符号整数（1..2^63-1），每通道从 1 编号，不同通道的序号互不相干；`wafer`、`run` 沿用既有 ID 约定，`at` 为既有设备本地整数坐标，`bin` 为既有 ASCII ID。ID 与本地坐标沿用原约定，收件阶段不校验 `run`/`bin` 是否属于某配置。
- `receive(readings)` 接受一个批次（数组，或单个读数对象），允许乱序、允许重复源身份。同一源身份、内容（`wafer/run/at/bin`）完全相同的补发无副作用，已封账也如此；同一源身份内容不同则拒绝。一条新读数占一行待封账额度；封账成功后其晶圆的待封账行立即释放，即使设备尚不能清理。同源重放不占额度。
- 批次整体原子：批次内任一读数非法、冲突、属于已封账晶圆的新源身份，或使待封账行数超过上限，则整批拒绝，不留部分收件。已封账晶圆不再收任何新源身份。成功返回 `{"accepted", "replayed", "pending_rows"}`。

### 封账（按片）

`seal(request)` 的 `request` 为一片晶圆的首测配置，即既有校准输入去掉各 run 的 `records`，再加 `sources`：

- `wafer`、`sites`、`links` 沿用既有校准输入；`runs` 的每项恰含 `id`、`bins`、`candidates`（不得含 `records`，记录由源身份还原）。
- `sources` 为按工位列出的完整源身份清单，每个 `runs` 中的工位都必须出现（可为空数组），每项恰为 `{stream, seq}`。

封账校验：每个源身份只能出现一次；清单必须已收到且归属该晶圆与该工位，既不允许多列、漏列，也不允许列入其他晶圆的身份；该片已收但未列入清单的读数不能被静默丢下。同一工位中两条不同源身份落在同一本地位置时，即使 `bin` 相同也拒绝封账（不当作设备补发）。用清单中的读数还原原始 `{at, bin}` 记录，完整入参必须被既有校准入口 `process_calibrated` 接受（含 bins/records/候选/links 的全部结构与可行性、规模边界）；不可用的数据不释放额度、不推进游标、不留下可查询的包。

成功后按 `wafer` 可取回不可变、可 JSON 序列化的交接包：`{schema: "wafer-first-test-handoff-v1", wafer, payload, lineage}`，其中 `payload` 是可直接交给 `WaferArchive.create` 的完整首测入参，`lineage` 为逐行血缘 `{run, at, stream, seq}`。记录与血缘均按工位声明顺序、工位内先 y 后 x 排列，与收件顺序、清单顺序无关。

封账重试按 `wafer` 识别：配置结构相同（数组顺序计入比较）且各工位源身份集合相同，返回原包；同一 wafer 的不同请求拒绝。

### 交接包与清理游标

- `handoff(wafer)`：取回已封账晶圆的交接包；未封账抛 `ValueError`。
- `cleanup_cursor(stream)`：该通道从 1 起、每一条记录均已进入持久交接包的最大连续前缀末端；空前缀为 0。仅收到、封账请求未成功、或序号尚未到达，都不能跨过去；较后晶圆可先封账，不必等游标追上。计算只遍历已封账序号，不逐个枚举未到达的序号。
- `confirm_cleanup(stream, cursor)`：确认设备可永久删除该通道至 `cursor` 为止的原始记录。`cursor` 必须落在当前已挣得的连续前缀内，且不得小于该通道已确认值（重复确认同值为幂等空操作）；不满足则拒绝。返回 `{"stream", "cursor"}`。
- `confirmed_cleanup(stream)`：取回该通道最后确认的清理游标，未确认为 0。

### 持久化与失败边界

写入沿用档案口径：同目录临时文件 + `fsync` + 原子替换 + 目录 `fsync`；重开时清理遗留临时文件。拒绝或提交前中断不得留下半个包、提前释放额度或提前确认清理；若提交完成但应答丢失，重开后的等价重试仍返回原包。成功写入在重开后可查。本 API 不改变既有校准、档案与复测接口，不提供通用 CLI、界面或设备连接。

## 双裸片装片批次 API MountingBatch（离线持久化）

`wafer_fusion.MountingBatch` 把 1～16 份晶圆的完整 `process_calibrated` 入参与候选裸片工艺分档融合成整批 L/R 配组，并管理装片位的领用凭据、装片回执与报废后的下一轮配组。只处理单个工装批次的数据处理与本地持久化，不控制设备；纯标准库、单写者按序调用，不新增运行时第三方依赖，不修改调用方入参。非法结构、重复、越界与业务冲突一律抛 `ValueError`。既有 API 行为不变。

### 建批 `MountingBatch.create(path, request)` / 重开 `MountingBatch(path)`

`request` 顶层恰含四个字段：

- `wafers`：1～16 份完整校准入参（即 `process_calibrated` 的输入结构，含 `wafer/sites/runs/links`），各份 `wafer` 标识不重复。建批时逐份调用既有校准引擎；裸片资格只来自该引擎给出的 `pick_coordinates`（在每种可行校准下都 pass），不读取、也不接受调用方声称的通过标记，不会只挑一种可行校准替代完整判定。某份入参本身无可行校准或结构非法时整批拒绝。
- `grading`：0～256 行工艺分档，每行恰含 `wafer`、`at`（规范坐标 [x,y]，分量 ±10000）、`side`（仅 `"L"`/`"R"`）、`source_group`（工艺来源组 ID，沿用 ASCII ID 约定）、`coefficient`（整数，±1,000,000，布尔不算整数）。每个 `(wafer, at)` 至多一行；坐标必须是该片校准入参 `sites` 声明的坐标，否则拒绝。系数在 ±1,000,000 内。
- `positions`：1～32 个不重复装片位 ID（ASCII ID），按工装声明顺序排列，配组结果按此顺序顺次分配。
- `max_coefficient_diff`：本批统一最大系数差，0～2,000,000 的整数；系数绝对差等于上限也允许配对。

资格口径：分档行只在其坐标属于该片 `pick_coordinates` 时 `eligible=true`。有分档但未入选片坐标的裸片合法保留在输入里（`eligible=false`），永不参与配组；没有分档的合格裸片同样不参与。分档与校准资格在本批内固定。不同晶圆的同一坐标是不同裸片，身份键为 `(wafer, y, x)`。

批次在独立本地目录创建，目录内单个 `mounting.json`（schema `wafer-mounting-batch-v1`）。目标目录已存在且含本批次状态时，仅当请求等价（重排晶圆列表或分档行不影响等价性，内部按规范顺序签名）才重开返回同一批次；请求不同或目录存在但无状态文件均抛错，绝不覆盖。

### 预览 `preview_plan()`（只读，不写状态、不领料）

对所有「尚未装成、也没有未结领用凭据」的装片位一起配组。已耗用裸片永不回池；未结凭据固定的两颗对其他位置保留、不可被借用。配对条件：一颗 L、一颗 R，`source_group` 不同，且 `|系数差| ≤ max_coefficient_diff`；每颗裸片至多进入一个配对。

返回 schema `wafer-mounting-plan-v1`：

- `complete`：布尔。能补齐全部待配组装片位时为 `true`，否则 `false`。
- `positions`：本次参与配组的待配组装片位 ID（工装顺序）。
- `pairs`：排好序的配对列表，每项 `{left, right, coefficient_diff}`，其中 `left/right` 为裸片视图 `{wafer, at, side, source_group, coefficient}`。
- `assignments`：把 `pairs` 顺次分给 `positions` 的结果，每项 `{position_id, left, right}`。
- `total_coefficient_diff`：完整方案的系数差总和；不可补齐时为 `null`。

优化口径：先要求覆盖全部待配组装片位的完整方案（基数 k 的二分图匹配），在所有完整方案中取总系数差最小者。并列平局时，每颗裸片按 `(wafer ID, y, x)` 比较、每对按 `(L 裸片, R 裸片)` 比较，在排好序的配对列表之间取字典序最小者，再按工装声明顺序顺次分配。实现把该字典序口径精确编码为整数边权（超递增 2 的幂偏好项），由一次最小费用流求出唯一确定性最优解，不做近似扰动。不可补齐时 `complete=false` 且 `pairs/assignments` 均为空，不返回任何可领用的部分方案；没有待配组装片位时返回空方案（总和 0）。重排晶圆列表与分档行不改变结果。

### 领用凭据 `issue_credential(request)`

`request` 恰含 `request_id`（批内唯一 ASCII ID）、`position_id`、`pair`（`{left, right}`，各为 `{wafer, at}`）。

- 新请求：当且仅当该对仍是该装片位在**当前完整方案**中的配对时签发。预览已失效（裸片被耗用、被其他未结凭据锁定或装片位状态变化）时拒绝，绝不静默改发其他裸片；当前不可补齐、装片位已有未结凭据或已装成终结时同样拒绝。
- 一次签发只固定该装片位的一对，不固定预览中其余位置；自签发起这两颗被保留，其他请求不得借用。凭据的 `credential_id`（批内单调序号 `C0001…`）、装片位与当时批准的两颗裸片不可变。
- 同一 `request_id` 携带完全相同的请求体再次调用：取回原凭据，即使它已终结、装片位已补发或当前已不可补齐；这不是重新授权。相同 ID 的不同请求体（不同装片位或不同裸片）拒绝。

返回凭据视图 `{credential_id, serial, request_id, position_id, left, right, state, terminal}`，其中 `state` 为 `"open"`/`"mounted"`/`"scrapped"`，`terminal` 未终结时为 `null`。

### 装片回执 `submit_mount_report(request)`

`request` 恰含 `credential_id`、`outcome`（`"mounted"`/`"scrapped"`）、`touched`（已碰胶面裸片 `{wafer, at}` 列表）、`untouched`（明确确认未碰胶的裸片列表）。回执是终态，两个集合必须完整准确地恰好覆盖凭据中的两颗裸片（不重不漏、不重叠、不得引用凭据外裸片）；现场尚不知道碰胶范围时不应提交，未结凭据与两颗裸片继续保留，不能靠超时或重新预览释放。

- `mounted`：`touched` 须恰为凭据两颗，装片位终结（mounted），两颗耗用。
- `scrapped`：允许零、一或两颗在 `touched`；所列裸片耗用并永久记入 `consumed`；未列入 `touched` 的裸片当且仅当出现在 `untouched` 中才退回配组池，原装片位重新纳入待配组。报废是实物记录，即使它使剩余装片位无法配齐也照常受理，不拒收、不撤销。
- 未知凭据、凭据外裸片、集合未覆盖两颗、同颗同时出现在两个集合、mounted 却声明 untouched，以及同一凭据的矛盾终态，一律拒绝。
- 完全相同的终态回执重复提交（集合顺序不计）取回原确认，不再产生新领用、不改变耗用事实。

返回确认 `{credential_id, request_id, position_id, outcome, touched, untouched, consumed, position_status}`，`position_status` 为 `"mounted"` 或 `"waiting"`。

### 查询（跨班重开后均可调用）

- `positions()`：按工装顺序返回每个装片位 `{position_id, status, open_credential_id, mounted}`，`status` 为 `"waiting"`/`"locked"`（有未结凭据）/`"mounted"`；可据此区分历史凭据与当前未结凭据。
- `open_credentials()`：当前未结凭据视图列表。
- `credential_history()`：按签发序号返回全部凭据（含终态），历史与未结可区分。
- `consumed_dies()`：全部已耗用裸片（含装成与报废碰胶），每项为裸片视图加 `credential_id` 与 `reason`。
- `preview_plan()`：基于当前未结凭据与已耗用集合的当前方案。
- `grading_view()`：全部分档行及 `eligible` 标志；`batch_info()`：批次固定信息摘要。所有返回结构均可 JSON 序列化。

### 持久化、原子性与重放

状态写入沿用档案口径：同目录临时文件 + `fsync` + 原子替换 + 目录 `fsync`，重开清理遗留临时文件。签发与配对锁定一起生效；终态确认、已耗用集合与装片位的终结/重新开放在同一次提交内一起生效。提交前中断后重开看不到半次变化；已完整提交但答复丢失时，凭原 `request_id`（领用）或原终态回执（报告）重放即可取回当时的凭据或确认。只支持一个调用方顺序操作，不提供通用 CLI、界面或设备连接。
