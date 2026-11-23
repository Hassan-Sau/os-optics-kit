# 本地实时查看器

[os-optics-kit](../../README.zh-CN.md) · [English](../en/viewer.md) · 简体中文

`os-optics-kit view` 会在你的默认浏览器中打开一个轻量的本地只读页面：当你或编程 Agent 在本地修改原生 Project 文件或持续产出新的运行结果时，页面会自动跟随更新，让你实时看到光路和指标的变化。

查看器仅绑定本机回环地址（`127.0.0.1`），只需安装基础包 `os-optics-kit`（依赖 `os-optics>=0.13,<0.14`），**无需**安装 `[render]` 或 `[plot]` 扩展，**无需**下载 Playwright/Chromium，也**无需**任何云端账号。

## 快速开始

```bash
python -m pip install "os-optics[protected]" os-optics-kit

# 查看并实时跟随一个已保存的运行目录（3D 光路、功率账本、执行状态与指标评估）
os-optics-kit view run

# 查看原生 Project 文件（未关联运行结果时，仅展示标称几何预览）
os-optics-kit view project.json

# 同时关联 Project 与运行目录（自动跟踪新鲜度，绝不张冠李戴）
os-optics-kit view project.json --run run

# 在不修改原文件的前提下，于临时目录执行一次显式本地计算并查看
os-optics-kit view project.json --evaluate
```

常用参数：

- `--port PORT` / `--host 127.0.0.1` — 指定回环端口（默认由系统自动分配空闲端口；拒绝非回环地址）。
- `--project PROJECT.json` — 打开运行目录时，额外指定用于对比新鲜度的 Project 文件。
- `--configuration ID` — 当运行目录包含多构型时，指定查看某一构型。
- `--poll-interval SECONDS` — 文件变化轮询间隔（默认 `0.4` 秒）。
- `--no-open` — 只启动服务并在标准输出打印就绪 JSON，不自动弹浏览器。
- `--once` — 输出单次快照 JSON 后直接退出。

按 `Ctrl-C` 即可干净关闭查看器。

## 页面展示内容

- **交互式 3D 光路与追迹光线** — 直接复用引擎自带的离线 3D 场景视图（零外部 CDN），底部附光线分支色标、功率与终止状态。
- **功率账本** — 不仅靠光线颜色示意，而是完整呈现总输入功率、残差、守恒状态、多通道堆叠比例条、各通道精确功率与单位，以及按元件展开的功率明细。
- **执行状态、完整性与新鲜度** — 清晰区分计算是否完成（`COMPLETED` / 触及预算的 `PARTIAL`）、模型适用范围、manifest 哈希校验以及结果新鲜度（`CURRENT` / `HISTORICAL`）。
- **指标达成与观测面物理量** — 逐条展示 `MET` / `UNMET` / `INDETERMINATE` / `NOT_APPLICABLE` 判定、实测值与声明范围，并在部分完成时给出有界区间和未追迹功率提示。
- **界面语言切换（EN / 中文）** — 点击右上角即可随时切换中英文界面，无需重启服务。

## 实时跟随与只读安全保证

- **严格只读**：绝不改写你的 Project 文件或运行目录，服务端拒绝所有写入类 HTTP 请求（`POST`/`PUT`/`DELETE`/`PATCH` 返回 `405`）。
- **单次原子快照绑定**：3D 几何、光线、功率账本、执行状态与指标判定始终绑定在同一份已校验的快照版本上，绝不把运行 A 的数值拼到运行 B 的几何上。
- **几何预览绝不冒充计算结果**：未关联运行结果的 Project 只展示几何布局并明确标注「未计算」；`--evaluate` 仅在启动时于临时目录计算一次，后续文件编辑不会反复触发重算。
- **过期结果不误标**：当 Project 修改后与关联运行不再匹配时，新鲜度立即转为「历史过期」，旧功率与光线会被隐藏，只展示新工程几何，直到新运行写入完成。
- **半写入容错与自动恢复**：当外部 Agent 正在写文件（JSON 截断或目录尚未写齐）时，页面会保持上一份有效快照并显示「等待写入」提示，写入完成后自动恢复更新。

## 与其他命令的分工

- `os-optics-kit view`：面向日常设计迭代的**本地实时交互查看**（普通浏览器直开，零额外依赖）。
- `os-optics view RUN --no-browser --snapshot DIR --views iso`：无需浏览器的静态系统视图导出。
- `os-optics-kit render` / `os-optics-kit present`：演示级高清出图、图库和幻灯片材料包。
