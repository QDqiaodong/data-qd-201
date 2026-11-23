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
