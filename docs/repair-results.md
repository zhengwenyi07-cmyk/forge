# 修复交付与实验状态

日期：2026-10-28。目标分支：`dev`。

## 交付状态

代码备份、CRLF 验证链路、文件分页、精确编辑和覆盖限制、显式完成协议和测试门禁、Linux Runtime、子 Agent 任务/消息/worktree 隔离已实现。修复过程中的关键边界问题也已加入回归：容器搜索脚本、相对工作目录、日志污染 diff、非法测试参数和子任务任务描述被历史裁剪。

完整备份：`D:\fianll-test\forge-backups\forge-agent-before-repair-20261028\workspace.zip`。基线提交：`dda8dce`。原始文件已验证可解压，具体哈希与恢复方式见 `architecture-repair.md`。

## 已完成的真实验证

- 官方 SWE-bench Lite gold patch：`astropy__astropy-14995` 为 resolved，1/1；基础设施错误为 0。通过 Linux harness 实际运行，约 90 秒；不是根据是否生成补丁判断成功。
- 新 MiMo API Key：标准 API 认证成功。接口返回当前可用模型，配置改为 `mimo-v2.6-pro`；窗口保存的密钥位于 Git 忽略文件中。
- 真实小型编码任务：Flash 为 success，7 步、20,833 Token；Pro 为 success，6 步、17,254 Token。两者均精确修改代码、尝试目标测试、测试通过并显式调用 finish。
- 官方 Astropy 镜像内工具兼容：任务环境为 Python 3.9.20，工具辅助脚本使用镜像基础 Python 3.11.5；file_view 与搜索实际执行成功。
- 打包检查：wheel 包含 `multi_agent`、`evaluation`、`run_verification` 和公开配置；不含 `*.local.yaml`。

以上微型任务只验证模型调用与工具链路，不构成 Flash/Pro 能力对比结论。

## 30 题严格 A/B：未完成，尚无模型对比结论

用户选择对比 `mimo-v2.6-flash` 与 `mimo-v2.6-pro`。`evaluation/swebench30.json` 已冻结同一组 30 个任务，覆盖 12 个仓库，并记录数据集哈希、base commit 和预算。`evaluation/run_ab.py` 已实现成对隔离运行、固定镜像 ID、交替执行顺序、代码指纹、断点续跑、官方评分与配对统计。

已成功构建 `forge-runtime:py311` 和兼容旧 Lite 数据格式的 `forge-swebench:4.1.0`；已生成 30 个官方镜像规格。实例镜像准备遇到持续的网络故障：

- Docker Hub：`net/http: TLS handshake timeout`。
- Matplotlib 镜像：下载出现 `short read ... unexpected EOF`；重试后单次拉取仍超过 600 秒。
- 备用镜像服务：`403 Forbidden`。

部分官方镜像已下载成功，下载缓存可复用。全套镜像和 5 个 gold smoke 尚未全部通过，因此 30 题付费模型任务未启动，不能报告解决率或模型胜负。没有将网络失败计为模型失败，也没有以微型任务替代这 30 个任务。

继续运行：

```powershell
.\.venv\Scripts\python.exe -m evaluation.run_ab --repos-dir D:\fianll-test\forge\swebench_eval\repos --output evaluation/runs/mimo-flash-pro-20261028
```

缺失仓库缓存时会从对应官方实例镜像提取准确的 `/testbed` 源码，核对 base commit 和干净 diff，不需要另行下载完整 Git 历史。预检成功后自动执行两个模型的同组任务，再运行官方验证并生成 `summary.json`。

原始证据在本地：`logs/ab-preflight-retry.txt`、`logs/gold-one.txt`、`logs/live-smoke.txt` 和 `evaluation/runs/mimo-flash-pro-20261028/`。这些运行数据被 Git 忽略，避免混入源码提交。

## 审查结论与边界

已按正确性、可读性、架构、安全和性能五个维度审查。新增回归覆盖了最初缺陷和联调暴露的问题；本轮不合并 `main`。

完成门禁按需求强制“尝试定向测试”，并不要求所有测试必须通过；调用者应查看结构化 `validation`。执行成功但测试失败会被标注，SWE-bench 是否解决仍由官方评分决定。

fork 是并发工作区隔离，不是对恶意代码的安全边界。父消息与停止信号在步骤之间消费，正在执行的模型请求或命令仍受各自超时限制。30 题 A/B 是开发集比较，完成后也不能直接外推为模型通用能力排名。

## 最终回归与交付分支

- Windows 宿主机：443 passed，4 skipped，106.83 秒（`logs/final-windows.txt`）。
- Linux 容器：438 passed，9 skipped，31.64 秒（`logs/final-linux.txt`）。9 个跳过项是容器中没有 Docker CLI 的嵌套 Docker 测试；对应真实 Docker 测试在宿主机全量测试中执行。
- 之后补齐 GitHub Issue 入口使用同一 Linux Runtime，并把旧 smoke 入口转接到新的 Docker 小任务验证；相关入口/CLI 定向回归 37 passed。
- 最终 wheel 构建通过。Git 候选文件扫描未发现新 MiMo Key；本地配置和实验日志均被忽略。
- 交付分支为 `dev`；本地 `main` 仍保持 `977d4cd`，远端 `main` 仍保持 `45d9dfb`。本轮不合并或推送 `main`。

累计 Token 预算在每轮请求前检查，达到 80,000 后停止后续调用；正在执行的单次请求可能使累计消耗超过阈值。两个模型采用完全相同的停止规则。

## 第二轮修复验证（2026-10-28）

复查确认的 8 项缺陷均已修复，并补充针对性回归。实现细节、项目环境配置示例及旧报告迁移规则见 `architecture-repair.md` 的“后续审查修复”。

- Windows 全量：468 passed，4 skipped，143.13 秒（`logs/review-fixes-final.txt`）。
- Linux 全量：461 passed，11 skipped，32.81 秒（`logs/review-fixes-linux.txt`）。11 项为测试容器内没有 Docker CLI 的嵌套 Docker 检查；真实 Docker 路径已在宿主机测试中执行。
- 环境准备实际测试：从 requirements 安装本地依赖 wheel，导入检查通过后断网，任务阶段依赖仍可正常使用；导入检查失败时 Runtime 关闭并阻止任务开始。
- 官方 Astropy gold：新入口、新绑定、新运行编号 `gold-review`，1/1 resolved；输入完全相同时续跑成功。
- 分数完整性复测：将同一任务预测替换成无效补丁并复用 `gold-review`，验证入口退出码 1，拒绝复用旧报告，已绑定输入保持不变（`logs/review-score-integrity.txt`）。
- 新的 Flash 流式小请求：服务端用量 input=98、output=84；Pro：input=98、output=195。两者 `usage_source=provider`，usage-only 尾块已被计入（`logs/review-live-stream.txt`）。这些小请求只验证用量统计，不比较模型能力。
- wheel 构建通过；不包含本地密钥文件。

30 题 A/B 尚未完成。本轮修复了缓存与执行机制，没有把上述单题/小请求作为 30 题评测结果。再次运行完整实验时应选择新的输出目录，以避免混用旧代码生成的缓存与未绑定报告。
