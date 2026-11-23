# data-qd-201 · 晶圆测试坐标与合格裸片

多个设备坐标下的测试bin通过正交整数映射关联到规范裸片；必需测试的失败、缺测和skip联合决定可选取裸片坐标。

本仓主要交付为Python业务API与下游可消费的数据，不以通用CLI、报表或后台模板为产品。可观察字段、单位、身份、上限及失败边界见 [数据契约](docs/contract.md)。

## 独立安装与验证

Python >=3.11，实际验证CPython3.12.14、uv0.12.19。运行和测试仅标准库；构建固定setuptools80.9.0、wheel0.45.1。各仓独立 `.venv`，可以共享公共下载缓存，不共享site-packages。

从本仓根目录执行：

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
.venv/bin/python -I examples/run_sample.py
```

以上命令在本地现有公共缓存上真实执行通过，无需新增下载。新机器缓存为空时，先准备匹配Python与锁定公共构建包，或在可联网安装环境移除安装命令中的offline；API、测试与样例运行不需要网络。示例创建 `run/sample.json`，目标已存在会拒绝覆盖；重复运行请先自行保留产物并选择新的演示工作副本。

## 使用与实际结果

```python
from wafer_fusion import process
from wafer_fusion.io import load, publish

result = process(load("fixtures/input.json"))
publish("run/another-sample.json", result)
```

4个裸片、2次测试：1个可选取、1个失败、2个未完整测试；旋转设备记录的原始坐标保留。

安装后的样例摘要：

```json
{"failed": 1, "incomplete": 2, "pickable": 1, "runs": 2, "sites": 4}
```

已通过 6 项业务测试，另实际核对已有目标、符号链接目标、JSON重复键、非法业务输入和临时发布文件清理。数值和排序语义见契约，不以文件哈希代替业务结果断言。

## 校准融合 API process_calibrated

设备复位后每道测试只提供若干候选坐标变换时，使用 `wafer_fusion.process_calibrated(data)`。相邻工位的基准点 links（三站有效窗口）约束哪些候选组合构成可行校准；输出每颗裸片在所有可行校准下实际可达的 `possible_statuses`，选片坐标只包含在每种可行校准下都 pass 的裸片。无可行校准或输入非法时抛 `ValueError`。纯离线、标准库实现，不修改入参；字段、判定与排序口径见 [数据契约](docs/contract.md)。

```python
from wafer_fusion import process_calibrated

result = process_calibrated(payload)
result["schema"]              # "wafer-calibration-v1"
result["supported_candidates"]
result["dies"]                # die_id / at / possible_statuses
result["pick_coordinates"]
```

测试均使用合成数据，不操作选片设备：

```sh
.venv/bin/python -I -m unittest discover -s tests -v
.venv/bin/python -I tests/scale_regression.py
```

`tests/scale_regression.py` 按固定验收规格构造 19881 个规范裸片、20 道测试、每道 9801 条读数与 8 个候选、49 条基准 links；构造完毕后仅计 `process_calibrated` 的单进程 CPU 时间（`time.process_time`），预算 30 秒，当前 CPython 3.11 实测约 2 秒，并全量核对支持候选、原点必选、轴向点必 fail 与越界裸片仅 incomplete。另以独立暴力枚举在 700 个随机小案例上逐字段交叉验证（脚本在仓库外的临时验证目录，不属于交付物）。

## 当前边界与来源

不管理库存、订单、复测时间版本、自动设备控制或统计看板。

全部输入样例原创合成，MIT许可，见 [来源](fixtures/SOURCES.md)。仅复用MIT通用构建、字段校验与原子新建IO脚手架；业务实现独立编写。当前精确公共依赖镜像准备查询结果为 `true`；这不影响已通过的宿主离线独立环境验证，也不构成容器验收记录。本次没有构建或启动容器。
