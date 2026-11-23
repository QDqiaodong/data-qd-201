# 晶圆裸片数据契约

使用者：晶圆测试数据工程师。process(data) 是纯Python业务API，返回规范坐标裸片数据和可供选片步骤读取的 pick_coordinates；不操作设备。所有样例合成，无真实生产数据。

输入 wafer 为ASCII唯一批内标识。sites 为1..20000个唯一整数[x,y]坐标，每个分量范围±10000。runs 为1..20个必需测试，每项id唯一，matrix 为2×2整系数正交矩阵（系数-1/0/1，每行单位正交），offset 为整数坐标。规范位置 = matrix × 设备at + offset。不自动推断旋转、单位或原点；允许镜像，全部位置必须属于sites。

每个run的bins有1..100个ASCII代码，值为pass/fail/skip。records为0..20000条{at,bin}，设备坐标不可重复，bin必须声明；总记录<=200000。即使重复记录内容一致也拒绝，不隐式作复测；不存在的记录是missing，skip是实际观测的另一分类，不能混淆。

任一测试fail则裸片status=fail；无fail但有missing或skip则incomplete；全部pass才pass且纳入pick_coordinates。blocked_by保留每个非pass原因，即使fail已主导结果也保留缺测线索。die_id由wafer和规范坐标组成；裸片按y再x排序，观测按runs声明顺序，源坐标与bin保留。没有库存、订单、良率看板、复测优先级或时间版本。后续复测政策、reticle分组、选片图适配可复用run/die/bin身份。

严格拒绝未知字段、JSON重复键、布尔充当整数等非法值。io.publish仅创建新JSON，目标存在或符号链接时失败，计算错误不发布。examples/run_sample.py只是固定样例调用，不提供通用命令行产品。
