# Forge 架构修复与验证

日期：2026-10-28。开发分支为 `dev`，验证后再合并 `main`。

## 原始代码与恢复

开始修复前，包含 `.git`、忽略文件、日志、本地配置和未跟踪文件的完整工作区已压缩为：

`D:\fianll-test\forge-backups\forge-agent-before-repair-20261028\workspace.zip`

压缩包校验通过，共 1108 个文件，SHA-256：

`5037e714639e0510b5487f2e2e9b14994ffc97c52099472ff433a2e8bf9be14b`

备份目录中的 `manifest.json` 记录原始 HEAD 和工作区状态。当前未提交实现被保存为基线提交 `dda8dce`，便于比较修复前后的代码。该提交包括用户此前已有的源码改动、Multi-Agent 文件及文档删除状态。

恢复时将压缩包解压到一个新目录进行比较；需要修复前版本时可从 `dda8dce` 创建另一个分支。避免在含新工作的目录执行 `reset --hard`。

## 实现顺序

1. SWE-bench：harness 固定为兼容现有 Lite 数据格式的 `swebench==4.1.0`，只在 Linux 中运行。复制 `.sh` 到测试容器前使用字节模式转换 CRLF 并检查裸 CR。缺失报告、测试基础设施错误和未完成实例会导致验证失败；空补丁算未解决。
2. 文件工具：`file_view` 的内容与行号对应，每页最多 100 行，读/查看均有字符上限。空文件、非法起始行、EOF、LF/CRLF 均有断言。`file_edit` 要求唯一匹配，可提供 SHA-256 防止旧内容覆盖；原子写入并保留 CRLF。`apply_patch` 先检查完整 unified diff 再应用。超过 500 行或 32 KB 的现有文件禁止整体覆盖，大幅缩短中型文件也会被拒绝。
3. 完成协议：显式注册 `finish`/`give_up`。模型普通 `stop`/`end_turn` 只表示当前回复结束；截断输出可以继续。修改任务必须产生不同于初始工作区的新 diff，且最后一次修改后实际尝试过定向测试。参数验证失败不会被算作已测试。结果包含真实测试结果，测试失败时摘要会标注；成功状态表示完成了交付流程，是否通过测试仍应看 `validation` 和官方评测。
4. Runtime：CLI 默认 Linux Docker；宿主机真实 repo 路径与容器工作目录分别处理。diff 包括暂存、未暂存和新文件，排除本次 EventLog 输出目录，读取不改变 index。容器初始化失败立即报错，命令超时通过容器内 `timeout` 结束进程。修复 Docker 搜索脚本的无效 Python 拼接。原生 Windows 命令执行不用于真实 Agent 工作。
5. 子 Agent：任务描述显式注入，父消息在每步前消费。fork 使用私有 Git index 生成当前工作区快照，再创建独立 detached worktree；绑定独立 Runtime 和工具实例。结果返回 patch，由父 Agent 审查后应用，子 Agent 不自动合并。review 不开放任意 shell。`task_message`/`task_wait` 支持通信和有界等待，并限制同时运行的子任务数量。父任务在子任务仍运行时不能完成。
6. 模型实验：冻结 30 个 SWE-bench Lite 任务，覆盖 12 个仓库。Flash 与 Pro 使用相同代码、工具、prompt、40 步、80,000 累计 Token 预算、8192 输出 Token 上限和 temperature=0。每个任务使用相同官方实例镜像的不可变镜像 ID；任务顺序固定，模型执行顺序交替。关闭 Multi-Agent 以固定单 Agent 架构变量。

## 本地使用

```powershell
.\.venv\Scripts\python.exe -m entry.configure_mimo
docker build -f docker/Dockerfile.runtime -t forge-runtime:py311 .
.\.venv\Scripts\python.exe -m entry.cli run --repo PATH --task "修复问题" --test-cmd "python -m pytest tests/test_target.py -q"
```

新 MiMo Key 使用标准接口 `https://api.xiaomimimo.com/v1`，模型为 `mimo-v2.6-flash` 或 `mimo-v2.6-pro`。Coding Plan 地址与普通 API Key 不可混用。默认配置通过环境变量读取密钥；窗口生成的 `config/mimo.local.yaml` 优先加载且被 Git 忽略。

## 重现 Flash/Pro 实验

```powershell
docker build -f docker/Dockerfile.runtime -t forge-runtime:py311 .
docker build -f docker/Dockerfile.harness -t forge-swebench:4.1.0 .
.\.venv\Scripts\python.exe -m evaluation.run_ab --repos-dir D:\fianll-test\forge\swebench_eval\repos --output evaluation/runs/mimo-flash-pro-20261028
```

若 Debian 默认源连接失败，构建 Runtime 时可添加 `--build-arg DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn`；Python 下载受限时，构建 harness 可添加 `--build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`。这些参数只影响该次镜像构建。

`evaluation/swebench30.json` 已冻结任务 ID、base commit、数据集 SHA-256 和实验参数。使用其他任务集时以新 manifest 和新输出目录保存，不能用已看到的结果重新选任务。

预检会先拉取所有官方实例镜像，然后要求前 5 个任务的 gold patch 全部通过，才开始付费模型调用。`--prepare-only` 只生成数据/镜像规格；`--preflight-only` 执行镜像拉取和 gold 验证。网络失败会阻断后续评分，重复运行可复用已经下载的镜像。

每个模型在独立临时 clone 中运行，保留 result、JSONL 轨迹和 LF 补丁。官方 FAIL_TO_PASS / PASS_TO_PASS 判定解决情况，不使用生成补丁率代替成功率。完整结果生成 `summary.json`，包含解决率、Token、耗时、配对 bootstrap 区间和精确 McNemar 检验。30 个任务只是开发集，结论不等于模型通用能力排名。

断点续跑会核对代码、模型、镜像和预算指纹；代码变化后必须换输出目录。API/环境错误需解决后重跑，不能当作模型失败计分。`--skip-gold-smoke` 仅用于同一组环境已经通过 gold smoke 后的续跑。

## 验证记录

- Windows 宿主机最终全量回归：443 passed，4 skipped；之后的 GitHub Runtime 入口与 CLI 定向回归为 37 passed。
- Linux 容器最终全量回归：438 passed，9 skipped；跳过项是测试容器中没有 Docker CLI 的嵌套 Docker 测试。
- 真实 Docker 搜索、精确编辑、分页、覆盖限制、fork Git 元数据和完成门禁定向集成：10 passed。
- 真实模型小任务：Flash 7 步、20,833 Token，Pro 6 步、17,254 Token；两者均产生补丁、通过目标测试并显式完成。此例只验证运行链路，不能用于判断模型胜负。
- 官方 Astropy gold 验证：`astropy__astropy-14995` resolved，1/1，基础设施错误为 0，实际执行约 90 秒。

完整 30 题对照评测状态与最新回归结果记录在同目录的 `repair-results.md`，原始运行文件位于被忽略的 `evaluation/runs/` 和 `logs/`。
