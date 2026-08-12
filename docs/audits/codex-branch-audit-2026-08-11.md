# Codex 两分支审计收尾 — 2026-08-11

## 范围与威胁模型

本报告复核并修复以下两个 Codex 任务的工作：

| Codex 任务 | 原始分支/审计基线 | 修复所在分支 | 修复代码 HEAD |
| --- | --- | --- | --- |
| `019fec2b-a260-7123-8a9d-a6cc0a3f0ab9` | `codex/phase12-windows` @ `25ee335d5dd080e45547d7127519802d7d17aff1` | `codex/phase12-windows` | `a0243dd628fccd0f5e9e6aa95dc69c0a48f53e33` |
| `019fec60-45bc-72f3-ac34-23d01667be12` | `codex/phase13-macos` @ `cac2b6dbc589f7f3574ee519b110cfa02994418f` | `codex/phase13-audit-fixes` | `17ab98ece533cdf70e4b6c0b8f4dda893d13a667` |

当前唯一产品需求是可信本机、单用户使用的本地应用。当前不承诺、不设计也不实现公共网站、多用户服务、租户隔离、云部署或不存在的 Server Runtime。未来交付形态为 **TBD**。

本轮仍防御路径穿越、链接逃逸、特殊文件、损坏或恶意 DEM/清单/ZIP/3MF、ZIP bomb、资源耗尽、未声明 Provider 输出、误删、半成品发布、进程遗留/PID 误杀，以及制造产物、哈希、provenance 和确定性失真。

明确不支持同权限外部进程并发修改 TopoForge 正在使用的活动工作区。Provider 返回即表示其写入完成；本轮没有实现对同权限攻击者的 inode/ABA 对抗协议。

## 审计结论

两个原始分支都包含有价值的平台与本地工作流工作，但原始 HEAD 均不足以支撑新的公开平台声明。已确认的源代码、恢复、发布和制造证据问题已分别形成可回退的本地提交；没有合并两个分支，没有推送，没有移动标签，也没有发布候选。

基础架构只保留中立边界：

- 核心算法不依赖 CLI、Web UI、数据库或具体存储系统；
- CLI、Web UI 和 API 调用核心能力，不复制地理、网格、制造或验证算法；
- 本地任务管理和文件存储通过明确接口连接核心；
- 不为未知的未来产品形态增加预测性服务端抽象。

## Phase 12 修复提交

| 提交 | 主要关闭内容 |
| --- | --- |
| `0bfed33576ec091941c16cfe00357635061f47af` | 本地 Web 生命周期、任务/工作区删除与恢复、进程身份和 containment、持久启动意图、路径/链接/原子发布边界。 |
| `7191d3c9149981d03d41468b0fd948f744572a9a` | 完整工作流离线复核、ZIP 预算、原子写入、清理/备份/恢复事务、DEM 到 STL/3MF/GLB 的语义绑定、AOI/方向与 provenance。 |
| `b63eb7ec50d632e7e7ec15ae5c57f3e325438ea7` | Windows portable 的 wheel RECORD 闭包、独立发布证据、路径无关的 Bambu 内容身份、真实回滚与 clean-target 验证边界。 |
| `a0243dd628fccd0f5e9e6aa95dc69c0a48f53e33` | ACQUIRE Provider 输出的简单私有快照、精确目录闭包、完整阶段整体发布及清理失败语义。 |

### ACQUIRE 最终协议

1. 程序在私有随机根中排他创建 Provider 落点。
2. Provider 返回后，对该平面目录进行 no-follow 枚举。
3. 只允许 DEM、Provider acquisition 清单，以及清单声明且实际存在的质量掩膜。
4. 额外普通文件、子目录、符号链接、硬链接歧义和特殊对象全部拒绝。
5. 只把允许的普通文件复制到程序排他创建的快照目录；清单中的路径重绑定到快照。
6. 后续哈希、清单解析、Rasterio 检查、`acquire.json` 生成和发布只读取私有快照。
7. 完整 ACQUIRE 阶段在私有目录内完成，然后一次性移动到最终阶段目录。

发布状态现在可区分：

- 移动前失败：尚未发布，保留私有阶段供检查；
- 移动成功并复核通过：已成功发布；
- 移动成功但私有根清理失败：发布仍成功，记录 warning 并保留残余目录；
- 底层原子移动明确报告 committed 但耐久最终态无法证明：报告发布结果不确定，要求严格重开后再决定。

## Phase 13 修复提交

`codex/phase13-audit-fixes` 在原始 Phase 13 HEAD 上包含以下九个提交：

- `53bc8e1`：macOS 路径、私有应用目录和 Bambu 证据边界；
- `012e98a`：托管 CI/发布证据预算和来源绑定；
- `96eae18`：强制版本化 Bambu probe；
- `fc7505e`：Bambu project/ZIP/清单复核；
- `e7918ef`：拒绝含糊的 support-matrix 证据；
- `e1806ea`：证据工作流 action 固定到精确提交；
- `460813c`、`072f671`、`17ab98e`：固定 Bambu 证据读取、关闭已确认的读取/发布不一致，并协调最终发布证据。

这些提交只证明修复代码和本地/托管回归，不证明 macOS 产品支持，也未合并到 Phase 12 分支。

## 验证

### 当前 Phase 12 工作树

- 聚焦 ACQUIRE/完成工作流复核：`22 passed in 13.33s`。
- `uv lock --check`：`Resolved 76 packages in 1ms`。
- `uv run ruff format --check .`：`196 files already formatted`；随后唯一机械 lint 修正格式化 1 个文件，单文件复核为 `1 file already formatted`。
- `uv run pyright`：`0 errors, 0 warnings, 0 informations`。
- `uv run ruff check .` 初次报告一条 `SIM117`；机械合并 `with` 后只重跑失败项，结果为 `All checks passed!`。
- `git diff --check`：退出 0。
- 全量 Pytest 仅运行一轮；当前套件独立收集原文为 `873 tests collected in 2.00s`。该轮进程已经结束，但并行执行句柄没有保留终端末尾和退出码，因此不能如实补写通过/跳过计数；遵照“不重复全量测试”的停止条件，没有为补录结果再次运行全套。既有 Linux 原生 Windows junction 跳过仍保留在套件中。
- 既有 Web 复核：Vitest 29/29，production build 通过，Playwright 2 passed / 2 project-inapplicable skipped。

### Phase 13 审计工作树

- 全量 Pytest：`382 passed`。
- 目标 macOS/Bambu/release 套件：`87 passed`。
- Ruff、format、Pyright、lock 和 diff 检查均通过。

## Residual risks / 开放外部门槛

- 不支持同权限外部进程并发修改活动工作区；Provider 必须在返回前完成写入。
- 发布后的私有空根若清理失败会保留并记录 warning，需要操作者后续检查；不会因此把已发布阶段误报为未提交。
- 未来产品形态为 TBD；当前没有公共网站、多用户、云或桌面发行承诺。
- Windows 10 22H2 x64、Windows 11 x64、跨平台制造比较和官方 Windows Bambu Studio 真机证据仍开放。
- macOS 的最终 OS/架构矩阵、clean-system 运行、应用签名、公证、quarantine/Gatekeeper 首次启动和官方 macOS Bambu Studio 证据仍开放。
- `codex/phase13-audit-fixes` 尚未与 Phase 12 分支合并；本任务明确禁止合并。
- 定量多机器/材料校准仍是独立外部证据工作，不由本次源码审计关闭。

## Git 操作边界

本次只创建本地提交。未推送、未合并两个分支、未变基、未移动标签、未创建发布。
