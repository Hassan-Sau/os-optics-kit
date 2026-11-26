# 变焦研究

[os-optics-kit](../../README.zh-CN.md) · [English](../en/zoom.md) · 简体中文

变焦镜头保持同一套处方和同一个像面，通过移动镜组改变焦距。对一个固定组、一个变倍组和一个补偿组，每个焦距下
两个条件确定两个运动位置：系统有效焦距（EFL）等于要求值，近轴像落在像面上。`os-optics-kit zoom` 用引擎的
近轴分析在一个焦距范围内求解这两个条件，把解写成同一个原生 Project 的命名配置，并逐个报告样本的实际值、
行程与间隙检查和状态。

示例是案例 [three-group-zoom](../../cases/three-group-zoom)：焦距 100、−30、50 mm 的理想薄透镜，像面在
x = 200 mm，焦距 35-105 mm。

```bash
os-optics-kit cases copy three-group-zoom --dest .
cd three-group-zoom
os-optics-kit zoom studies/zoom.json --out zoom --study
```

## 规格文件

```json
{
  "schema": "os-optics-kit.zoom-spec", "schema_version": 1,
  "base_project": "../project.json",
  "wavelength_nm": 633,
  "groups": [
    {"group_id": "front", "components": ["G1"], "envelope_mm": [0, 3]},
    {"group_id": "variator", "components": ["G2"], "moving": true, "travel_mm": [10, 70], "envelope_mm": [3, 3]},
    {"group_id": "compensator", "components": ["G3"], "moving": true, "travel_mm": [100, 150], "envelope_mm": [3, 3]}
  ],
  "image_plane": {"observation_id": "image"},
  "efl_mm": {"start": 35, "stop": 105, "step": 2.5},
  "anchors": [
    {"efl_mm": 35, "configuration_id": "wide", "positions_mm": {"variator": 26.6323, "compensator": 124.7022}},
    {"efl_mm": 70, "configuration_id": "mid", "positions_mm": {"variator": 47.9331, "compensator": 124.2553}},
    {"efl_mm": 105, "configuration_id": "tele", "positions_mm": {"variator": 59.9205, "compensator": 132.3609}}
  ],
  "tolerances": {"efl_mm": 0.01, "focus_mm": 0.005},
  "clearance": {"min_mm": 1.0}
}
```

| 键 | 含义 |
| --- | --- |
| `groups` | 像面之前位于光轴上的全部光学元件，按光经过的顺序（不在任何镜组中的元件会被拒绝，`UNGROUPED_PATH_ELEMENT`：求解会漏掉它，而写出的 Project 包含它）。镜组沿光轴整体移动；恰好两个镜组运动。`travel_mm` 是运动组允许的范围，`envelope_mm` 是镜座在第一个顶点之前和最后一个顶点之后的轴向长度。 |
| `image_plane` | Project 的一个观测面（固定在设计坐标系中）或一个点，必须位于光轴上。若 Project 在最后一个镜组之后有观测面，给出的点必须与它重合（否则为 `IMAGE_PLANE_MISMATCH`：写出的 Project 的运行和目标都在该观测面上测量）。摘要、`trajectory.json` 和 `report.md` 会写出保持对焦的观测面（`image_plane.observation_id`）和 Project 目标所测量的观测面（`targets_observation_ids`）。两者不同时（例如目标所测面之后 5 mm 处另有一个观测面），`targets_measure_on_this_plane` 为 `false`，`image_plane.note` 和报告会说明 PASS 适用于哪个面；用 `observation_id` 指定目标所测的面即可把对焦保持在该面上。 |
| `efl_mm` | 要求的焦距：`{start, stop, step}`（含 stop）或 `{values}`。 |
| `anchors` | 已知解：某个焦距下运动组的位置，直接给出，或从基础 Project 的某个配置中读取（`{efl_mm, configuration_id}`）。它们作为求解起点并检查求解分支，可以位于要求范围之外。 |
| `tolerances` | 每个样本的验收：\|EFL 误差\| 和 \|焦点误差\|，单位 mm。 |
| `clearance` | 包络之间以及最后一个包络与像面之间允许的最小轴向间隙。 |
| `solver` | 可选：`tolerance_mm`（1e-7）、`max_iterations`（30）、`max_refinements`（8）、`branch_jump_mm`（1.0）、`anchor_tolerance_mm`（0.01）、`derivative_step_mm`（1e-4）。 |

镜组位置是其第一个元件位姿原点沿光轴的设计坐标（光轴沿 +x 时即 x）。长度单位为 mm。

## 位置如何求解

每个样本对两个位置求解两个条件，使用牛顿法。模型是引擎对放置好运动组的基础 Project 的近轴分析
（`os_optics.local.json_api.paraxial_analysis`，与 `os-optics project paraxial` 相同），雅可比矩阵用中心差分求得。
求解器不假设薄透镜：它使用引擎近轴分析对所列元件给出的结果（示例使用理想薄透镜）。

焦距按延拓顺序求解：

1. 每个锚点从其声明的位置求解；
2. 从第一个锚点向下求到范围起点；从每个锚点向上求到下一个锚点；从最后一个锚点向上求到范围终点；
3. 每一步从预测值出发：在锚点处用解曲线的切线，之后用最近两个解的割线；
4. 若某一步不收敛，或解与预测值相差超过 `branch_jump_mm`（落到变焦方程的另一支），则改为两个半步重试，
   最多减半 `max_refinements` 次；
5. 延拓到达下一个锚点的焦距时，解必须与该锚点自身的解相差不超过 `anchor_tolerance_mm`，否则该锚点报告
   `BRANCH_MISMATCH`；
6. 此时这两个锚点之间的连续性丢失：两者之间的样本为 `UNVERIFIED`，从唯一一次检查失败的端点锚点向外（第一个锚点
   以下、最后一个锚点以上）延拓得到的样本也是如此。两个一致的锚点之间的区间保持其状态。`trajectory.json` 中的
   `continuity`（以及标准输出摘要和 `report.md`）列出每处断点：两个锚点、它们的焦距、偏差以及未验证的样本。

补偿组可以中途反向；示例中约在 53.7 mm 处折返。行程和间隙不参与求解，而是在求解后检查，因此超出行程的位置
按求得的值报告。

## 报告内容及其依据

每个求得的样本都成为 `DIR/project.json` 的一个配置；锚点保留其 `configuration_id`，其他配置按焦距命名
（`efl_042p50`）。基础 Project 中引用锚点以外配置的目标会被去掉，并列在 `dropped_targets` 中。报告的数值随后从
引擎对 `DIR/project.json` 逐个配置的近轴分析中读取：

| 数值 | 依据 |
| --- | --- |
| 实际 EFL、EFL 误差 | 系统 EFL，即约化 ABCD 矩阵的 −1/C，减去要求的 EFL |
| 焦点误差 | 后焦点（设计坐标系）沿光轴的位置减去像面沿光轴的位置：无穷远物的近轴像，在像面之后为正 |
| 位置 | 该配置中每个镜组第一个元件沿光轴的坐标 |
| 间隙 | 元件的入射和出射顶点，加上包络；给出最小值及其所在的两者 |

| 状态 | 含义 |
| --- | --- |
| `PASS` | 两项容差都满足，行程和间隙都成立 |
| `INFEASIBLE` | 已求解，但有镜组超出行程（`TRAVEL_EXCEEDED`）或间隙低于最小值（`CLEARANCE_VIOLATED`） |
| `FAIL` | 已求解，但未满足容差（`EFL_OUT_OF_TOLERANCE`、`FOCUS_OUT_OF_TOLERANCE`）或分支检查失败（`BRANCH_MISMATCH`） |
| `UNVERIFIED` | 已求解，容差、行程和间隙都满足，但位于连续性丢失之处（`CONTINUITY_UNVERIFIED`，注明两个锚点）：没有锚点一致性确认其所在解支 |
| `UNSOLVED` | 无解：`NOT_CONVERGED`、`SINGULAR_JACOBIAN`（折点）、`BRANCH_JUMP`、`AFOCAL`、`ENGINE_REFUSED`，或没有锚点收敛时的 `NO_SEED` |

要求范围从最小的要求焦距到最大的要求焦距。范围内的锚点与其他样本一样处理。范围外的锚点（例如要求 35-70 mm
时的 105 mm 锚点）仍作为延拓起点并参与分支检查，也会被求解、写成配置并评估，但单独报告：在 `trajectory.json`
的 `anchors_outside_range` 中（样本编号 `a001` 等），在 `report.md` 的单独一节中，其锚点记录带有
`in_requested_range: false`。它不计入样本计数、覆盖情况、步长统计、最差值、`trajectory.csv`、`--study` 的样本或
总状态。

不丢弃任何样本。`trajectory.json` 包含每个样本及其原因（各自带有相关数值）、锚点的声明位置和求解位置、分支
检查、样本编号和状态、要求范围外的锚点、要求范围的覆盖情况（`covered_fraction`：要求范围中位于该范围内相邻两个
PASS 样本之间的比例，`pass_intervals_mm`，非 PASS 样本的连续段，最大和最小步长）、最大 EFL 误差、焦点误差和
最小间隙、求解器设置和引擎调用次数，以及引擎版本和构建号。`trajectory.csv` 是要求范围的样本表，`report.md` 是
它的展示版本。要求范围内所有样本通过时命令退出码为 0，否则为 1。

## 查看结果

```bash
os-optics-kit view zoom/project.json --configuration efl_087p50      # 单个配置的几何
os-optics project run zoom/project.json --out-dir zoom/run          # 追迹全部配置
os-optics-kit view zoom/run --configuration tele
os-optics results configurations zoom/run                            # 并列比较各配置
```

加 `--study` 时，命令还会通过扫描机制运行每个配置（参数 `efl_mm`；未求解的样本记为被拒绝的样本），因此研究
工具都可以使用：`--workers`、`--resume`、`os-optics-kit plot sweep zoom/study --y target:axis_spot`、
`os-optics-kit compare zoom/study`、`os-optics-kit report zoom/study --project zoom/project.json`，以及用
`present init zoom/run/configurations/wide zoom/study --animate --set assets.playback-study.y=target:axis_spot` 回放各样本并绘出光斑半径。示例中像面上轴上光斑半径按 λf / (πw₀) 从 35 mm 时的
0.00705 mm 增大到 105 mm 时的 0.02115 mm：追迹光束始终聚焦在固定像面上。

## 限制

- 求解基于近轴分析；所求配置的像差在其追迹运行中体现。
- 两个运动组，一条直的光轴（无反射镜），物在无穷远。
- 从锚点出发跟踪一条解支。超过变焦方程折点的样本为 `UNSOLVED`；两个锚点不一致时，其延拓到达的样本为
  `UNVERIFIED`。
- 镜组即整条光路：像面之前不在任何镜组中的元件，若其通光孔径（原生模型的通光或边缘半径）到达光轴或未声明，
  就会被拒绝；孔径不触及光轴的元件不予考虑；近轴分析无法单独处理的光学元件（倾斜、或缺少截面轴的柱面元件）按其
  声明的位姿和孔径判断。像面同样按此规则与 Project 的观测面核对。
- 间隙是轴向的：顶点位置加上声明的包络。
