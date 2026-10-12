# audit-report — 多 agent 工作流编排器审计与修复记录

> 审计基线：2026-10-10 全仓通读；修复按确认后的优先级清单执行。
> 状态图例：✅ 已修（附回归）｜📋 待处理｜🧷 P3 记录备查（不改）｜⏳ 待验证
> **P0/P1/P2 全部完成（R1~R14 全 ✅），P3 按约定只记录不改。**

## 一、审计发现总表（R1~R18 × 处理状态）

| 编号 | 发现 | 级别 | 状态 |
|---|---|---|---|
| R9 | run_segments 停下上报一律标 `M\t`，前序段删除漏判 delete_file L3 | P0 | ✅ 已修 |
| R2 | `.orch/` 混入 commit/PR/files_changed/change-summary | P0 | ✅ 已修 |
| R4 | confirm 无 TTY 未 --yes 时 EOFError 裸 traceback | P0 | ✅ 已修 |
| R1 | state 契约 `doc_id`/`fingerprint_mode` 从未由流水线写入 | P0 | ✅ 已修 |
| R3 | 成本预算形同虚设（tokens/usd 不累计，max_usd/max_tokens 门永不触发） | P1 | ✅ 已修 |
| R5 | 分段阈值双源（compile 1200 vs dispatch 6000） | P1 | ✅ 已修 |
| R6 | sandbox.destroy 零调用、cleanup.on_merge 不生效 | P1 | ✅ 已修 |
| R12 | runtime.state_dir/resume/timeout_seconds 被 cli 硬编码取代 | P1 | ✅ 已修 |
| R7 | verify_parallel 死字段 | P2 | ✅ 已修（裁定方案：保留字段+不静默提示，理由下附） |
| R8 | adapter type(cli/manual/http) 未落地、http 无骨架 | P2 | ✅ 已修（base 按 type 真分派，http 明确 NotImplementedError） |
| R10 | orch approve 文案说"处理审批"，实现只展示 | P2 | ✅ 已修（help 与实现一致） |
| R11 | compile docstring 声称产 acceptance.md，实际不产 | P2 | ✅ 已修（docstring 对齐+行为证明） |
| R13 | specs/ 与 .orch-worktrees 历史残留目录 | P2 | ✅ 已修（清理+根因接在 e2e 断言，E2E-8/9） |
| R14 | 无 gh 且远端非 GitHub 时 PR 环节无可操作指引 | P2 | ✅ 已修（回退 note 扩为可操作步骤，不实现 API 直连） |
| R15 | _escalate_by_path 对任意 config 目录段升级（偏严） | P3 | 🧷 记录备查，不改 |
| R16 | FORBID_ALIASES 自然语言匹配偏宽（偏严） | P3 | 🧷 **保持宁严勿松，不放松** |
| R17 | cost.json 快照点不全（heal 每轮不落） | P3 | 🧷 记录备查，不改 |
| R18 | supports_write_requests 声明式审批链路未经真实 agent 实战 | P3 | ⏳ 本机无支持该能力的 agent，**标注"未经实战，待验证"**，不算已通过 |

## 二、P0 修复记录（本轮已完成）

### R9 — 上报真实 name-status（防漏审删除）
- 改动：`core/dispatch.py` run_segments blocked 分支，`files_changed` 由
  `[f"M\t{p}" for p in prev_files]` 改为 `[r for d in done_info for r in d["raw"]]`
  （done_info 的 raw 就是 git name-status 原行，A/M/D/R 保留）。
- 回归：`tests/test_p0_fixes.py::TestR9RealStatusReport` —
  前序段 `D\told.py` + 次段 force_push 停下：断言上报含 `D\told.py` 且不含 `M\told.py`，
  且经 `collect_writes`→`policy.decide` 识别出 delete_file → verdict REQUIRE（不漏审）。✅

### R2 — `.orch/` 不进 commit / PR / files_changed
- 改动三处：
  1. `adapters/base.py::_diff_entries`：差集结果过滤 `.orch/` 前缀（POSIX 归一比对）；
  2. `core/dispatch.py::collect_writes`：新增 `_is_internal()`，files_changed 与
     write_requests 两侧都过滤；
  3. `git_push.py::ensure_committed`：commit 前调新增 `_exclude_orch_dir()`
     把 `.orch/` 写入仓库 `info/exclude`，`git add -A` 不再带入。
- 修复中实证到的两个 git 事实（写下来防止后人再踩）：
  - 裸 `rev-parse --git-dir` 对主仓库返回**相对进程 cwd** 的 `.git`，会写错位置；
  - linked worktree 的 `info/exclude` 实际从 **common dir**（主仓库 `.git/info/exclude`）
    读取，写到 `worktrees/<name>/info/` 下**不生效**——最终用 `--git-common-dir`。
- 回归：
  - 单测 `TestR2OrchExcluded` 3 项（diff 过滤 / collect_writes 过滤 / 主仓库 commit 排除）✅
  - e2e `tests/e2e_stub.py`：远端 feat 分支含 hello.txt、**.orch 零出现**、
    change-summary 无 .orch ✅

### R4 — confirm 无 TTY 友好失败
- 改动：`core/confirm.py` 新增 `NonInteractiveError`，run 捕获 `EOFError` →
  抛该异常并提示"非交互环境请使用 --yes"；`cli.py` handle_run 捕获 → 落 state → **返回退出码 2**，
  不 hang、无 traceback。
- 回归：`TestR4NonInteractiveConfirm` 3 项：
  - EOF → NonInteractiveError 且文案含 `--yes` ✅
  - assume_yes 从不读输入 ✅
  - 进程内调 `cli.main(["run", task])` + stdin 立即 EOF → rc=2、stderr 含 `--yes` ✅
  - 另用真实子进程验证过：`python cli.py run <task>` stdin 关闭 → rc=2 即时退出。

### R1 — 指纹契约接线
- 改动：`cli.py::_checkpoint` 增加可选 `config` 参数；传入时
  `st.fingerprint_mode = config.fingerprint.mode`、`st.doc_id = fingerprint.doc_id(task_dir, config)`，
  四处调用点（explore/confirm/compile/verify）全部接上。切 mode 只改 config，调用方代码不改。
- 回归：
  - 单测 `TestR1FingerprintWired` 3 项（落盘非空且与 doc_id 一致 / mode 切换传播 /
    zip 重导出 file_bytes 判新、content 稳定）✅
  - e2e `tests/e2e_stub.py` E2E-5/6：真跑一轮后 state.json 两字段非空且口径一致 ✅
- **设计口径说明（与任务书措辞的差异，按既有 B7 设计执行）**：
  任务书 R1 回归写"切换 fingerprint.mode 后值随之变"；而 B7 设计明确
  "未重新导出的普通文本文件两模式同值（切换不是迁移）"。`doc_id(task_dir)` 只聚合
  md 规格文件，故纯 md 任务目录切 mode **值不变才是正确行为**；"值随之变"发生在
  zip（docx 等）单文件层，已用 `compute_doc_id` 在回归中演示。mode 字段本身随 config 变，
  证明开关真实接线。若要求 md 场景也变值，则与 B7 设计冲突，需产品决策。

## 三、P1 修复记录（本轮已完成）

### R3 — 计费回调（用户裁定：回调方案，禁全局账本，为 R7 并发铺路）
- 改动：`core/llm.py` `LLMClient(config, on_cost=…)`；`_call_openai/_call_anthropic`
  返回 `(text, usage)` 并解析真实 usage（openai prompt/completion_tokens 与
  anthropic input/output_tokens 同字段名兼容）；`_bill` 统一记账：
  usage 缺失 → 按字符估算（约 4 字符/token）+ stderr `[billing] usage 缺失` 警告，
  不静默按 0；mock 路径走**同一记账函数**，`estimated=True`；
  单价 `llm.<tier>.price_per_1k_in/out` 缺省 0，但配了 `runtime.max_usd` 而无单价时
  启动明确警告“单价未配置，usd 恒 0”。
  `core/explore.py::run` / `core/compile.py::run` 加 `on_cost` 参数透传；
  `cli.py` 删除原手工 `add_cost(llm_calls=1)`，改 `_on_cost` 回调入账。
- 回归（实测触发，非“代码看起来对”）：
  - 极低 `max_tokens=1` / `max_usd=0.0001`+单价 → 进程内 `cli.main` 实测 **rc=3**，
    输出含“预算超限”+进度+花费，state.json `budget.tokens>0`、`budget.usd>0` ✅
  - openai usage 精确入账（tokens=150、estimated=False）/ 缺 usage 估算+警告 /
    mock 同口径 / explore・compile 签名含 on_cost（无全局状态）✅
- 附带发现并修复的真实缺陷：adapters 未注册 agent 时 `load_adapter` 裸 KeyError
  traceback → `cli._run_dispatch` 捕获 KeyError 归入 **rc=4**（与 agent 不可用同级）。

### R5 — 分段阈值单源（用户裁定：compile 删死分支，不新增判定行为）
- 改动：全链唯一判定函数 `dispatch.resolve_segment_mode` 读唯一键
  `config.segment_threshold`，唯一默认常量 `DEFAULT_SEGMENT_THRESHOLD=6000`；
  `core/compile.py` 删除死分支与第二键 `compile.segment_threshold_chars`；
  `config.yaml` 删 compile 段并注释说明；cli 打印行改常量。
- 回归：阈值驱动/同 config 判定一致/命令行覆盖优先 +
  **grep 级防变种断言**（四文件中 segment_threshold_chars 仅允许出现在删除说明行）✅

### R6 — 清理接线（用户裁定：fetch+merge-base 判定、orch clean 兜底、不新增轮询/钩子）
- 改动：`core/sandbox.py::destroy` 重写（work_dir 参数优先、幂等、返回 bool、
  `_repo_from_worktree` 从 .git 文件反查主仓库**不依赖 cwd**、remove 失败降级不崩）；
  `cli.py` 新增 `_branch_merged`（fetch + `merge-base --is-ancestor feat origin/base`
  三态 True/False/None）与 `_maybe_cleanup`（never/always/on_merge）；
  **三接线点**：`_do_push` 成功尾 / `handle_status` push-done 触发 /
  `orch clean --task` 新子命令（无 state→2、已清→0 幂等、失败→1）。
- 回归（真实临时 git 仓库+origin+worktree，进程内）：merge 后 worktree 与临时分支
  被清理 / 未合入保留+提示（误删保护）/ fetch 不可用降级 / always、never /
  坏 worktree（普通目录、被 lock）降级不崩 / cwd 切仓库外仍可清理 /
  orch clean rc 与幂等 / 三接线点静态断言 —— 10 项 ✅
- 残留清理（先列后删，清单当时已贴汇报）：specs/ 删 7 个运行产物目录
  （demo-overbudget/demo-resume/demo-verify/smoke-cli7/task-f8fa1aa0/
  task-policy-reg/task-seg-demo，共 58 文件）+ .orch-worktrees/ 删 4 个降级目录；
  白名单 `_template`/`demo`/`audit` 未动；删后逐项确认“剩余文件=0”才移空目录。

### R12 — runtime 三字段统一从 config 读（cli 只覆盖不取代）
- 改动：
  1. `cli.py` 新增 `_specs_dir(config)`（`runtime.state_dir`，默认相对 ROOT 的
     "specs"，绝对路径直用；`SPECS_DIR` 降为无配置兜底常量）；
     `init_task_dir(task_id, config)` 与 run/push/status/clean/approve 全部改经
     `_specs_dir`；模板仍固定取仓内 `specs/_template`（不随 state_dir 搬迁）。
  2. resume：`effective = args.resume or runtime.resume（默认 true，行为不变）`；
     `resume:false` 且已有 state.json 又无显式 `--resume` → 拒绝静默续跑 **rc=2**。
  3. timeout：`sandbox._git` 硬编码 60 → `_timeout(config)` 读
     `runtime.timeout_seconds`，并捕获 `TimeoutExpired` 转 rc=124 失败返回；
     `git_push._run` 原先**完全无 timeout**（hang 风险）→ 接线 + 捕获
     `TimeoutExpired` 转 rc=124；`ensure_committed` 增可选 config 参并顺带检查
     commit 返回码（超时/失败不再假报 True；措辞冲突按“捕获转失败返回”设计执行）。
  4. `cli._branch_merged`（R6 引入）本就读 runtime.timeout_seconds，同口径。
- 回归（TestR12 8 项）：_specs_dir 绝对/相对/兜底规则；实测 state_dir 指 tmp →
  任务目录落 tmp 不落 specs/；resume:false+已有 state → rc=2 含“拒绝静默续跑”，
  显式 --resume 放行 rc=0；monkeypatch 捕获 kwargs 断言 timeout 等于所设值
  （git_push 77/33/默认 60，sandbox 45）；**故意超时实测**：真实慢命令
  sleep 5 + timeout=1 → 返回 rc=124、实测耗时 <4s 不 hang；sandbox 同场景 mock
  TimeoutExpired → rc=124 不崩；grep 防变种：cli 内 `SPECS_DIR /` 直拼仅剩
  _template 一处、`_specs_dir(config)` ≥7 处 ✅
- 同类变种扫描声明（逐处，无静默忽略）：
  - compile 死阈值 → 已并入 R5 删除；sandbox.worktree_root cwd 问题 → 已并入 R6 修掉；
  - `dispatch.py` `timeouts.agent`（默认 1800）、`verify.py` `timeouts.verify_cmd`
    （默认 600）：命令级超时独立语义，本就 config 驱动 → 本就正确；
  - `llm.py` `request_timeout_seconds`（默认 120）：config 驱动 → 本就正确；
  - `adapters/base.py::_git_name_status` 两处 `timeout=30`：证据采集快照的探针式
    短超时，已捕获 TimeoutExpired 且失败返回空集（fail-safe），不属 runtime 命令
    超时语义 → 判定“本就正确”；若需也接 config 请裁下（未自行扩范围）；
  - state_dir/resume 第三处硬编码：不存在（grep 断言兼证）；
  - 已知残留：`runtime.timeout_seconds` 缺省值 60 在 cli/sandbox/git_push 三处
    各自作为 fallback 字面量重复（读取源单一、仅默认值重复），未强行抽公共
    常量以免跨模块新增依赖，记录备查。

## 四、P2 修复记录（本轮已完成，最后一批）

### R7 — verify_parallel：选定**保留字段 + 不静默**（未实现并行）
- 选型理由：acceptance 三段 setup→check→teardown 共享同一 worktree 状态，
  "无依赖命令"的判定需改 acceptance.md 格式（加并行声明标记）= 动设计边界；
  本机验收秒级、并行收益小而 Windows 并发子进程风险大（P0 已领教
  kill+communicate 死锁）。违约点只在"静默忽略"，故只把它变成不静默。
- 改动：`cli.handle_run` 进 verify 阶段时 `verify_parallel` 真值 → stderr 提示
  "预留字段、本批暂不生效：验收仍逐条串行（原因：setup/teardown 顺序依赖与
  worktree 共享状态）"；false/缺省不打（不误报已并行）。`config.yaml` 注释同步。
  verify.py/heal.py 零改动、无并发代码。
- 回归（TestR7 3 项）：进程内续跑到 verify 阶段实测 true → stderr 含
  "预留/串行/暂不生效"；false → 无提示无误导；grep 断言 verify.py/heal.py 内
  `verify_parallel` 零读取（杜绝假实现）✅

### R8 — adapter type：选定 **base 按 type 真分派**
- 选型理由："读到 http 才报错"仍是把分派藏进报错路径；base 显式三态语义才是
  字段活着；分派仅一个 if 链，不新增 http 实现。
- 改动：`AgentAdapter.__init__` 读 `cfg.type`（缺省 "cli"，六家现有配置零改动保持
  现行为）；未知值**加载即抛**不静默当 cli；`run()` 头部分派：manual → 不 spawn，
  抛既有 `AgentUnavailableError`（裁定：不新增异常/退出码），消息区分
  "无法自动执行，非 CLI 缺失"并给人工步骤（prompt 文件路径 + --resume 续跑）；
  http → `NotImplementedError`（裁定：收为 rc=4，不动 README 退出码契约）。
  `cli._run_dispatch` 与 verify/heal 段各补 `except NotImplementedError` → rc=4
  防裸 traceback。`config.yaml`：trae 事实归位 `type: manual`，adapters 段头注释
  说明三态；codex 等**不加** type（缺省兼容由回归证明）。
- 回归（TestR8 7 项）：缺省 cli + codex 无 type 键 + trae=manual 配置断言；
  manual → mock 断言 `subprocess.run` **零调用** + 消息含人工步骤；http → 单测
  NotImplementedError + **全链双路径实测 rc=4**（dispatch 路径与 verify/heal 路径）
  含"暂不支持 http"；未知值构造即抛；grep 断言 base 确有分派✅

### R10 — approve help 与实现一致
- 改动：`help="查看权限闸门审批记录与被拦详情（重审走 run --resume --interactive）"`；
  未实现"事后处理"（真正审批在 dispatch 交互模式，改实现会碰 approval 链路=扩范围）。
  `handle_approve` 零改动。
- 回归：`build_parser()` 取 approve help 断言含"查看"、不含"处理待"、含重审路径 ✅

### R11 — compile docstring 对齐实现
- 改动：模块 docstring 改为：读 context.md/answers.json/skills（acceptance.md 为
  _template 播种、人工维护的任务**输入**），**唯一产出 prompt.md**（含 SEGMENT 分段）。
- 回归（行为证明，非只改文案）：tmp 预置三输入+acceptance.md 跑 `compile.run(mock)`
  → 文件集合增量**恰好 {prompt.md}**且 acceptance.md 字节不变；docstring 静态断言
  旧虚假文案已移除 ✅

### R13 — 残留根因接在 e2e 路径（产品代码零改动，裁定3）
- 根因（非"清理未接线"）：`sandbox.create` 的 `mkdir(parents=True)` 会建 worktree
  **根目录**，而 `destroy` 清理契约只覆盖 task 子目录——空根是契约空白产物，手删
  治不了每次复现。`destroy` 不越权删共用根（回归静态断言）。
- 改动（仅 `tests/e2e_stub.py` 收尾）：E2E-8 根级 `.orch-worktrees` 空→自愈 rmdir，
  **非空→FAIL 暴露真残留**（断言而非手删）；E2E-9 specs/ 退出后仅剩白名单三目录。
- 回归实测：e2e **连跑两轮**均 rc=0、E2E-1..9 全 PASS；退出后
  `specs/` 仅 `_template/demo/audit`、`.orch-worktrees` 不存在（第二轮不复现
  = 不自增）✅

### R14 — 无 gh/非 GitHub 远端给可操作指引（裁定：不实现 API 直连）
- 改动：`git_push.open_pr` 回退分支 note 扩为编号步骤：①分支已推 feat/<task>→base；
  ②GitHub 形态：compare 链接（pr_url 保留不丢）+ GitLab
  `/-/merge_requests/new?...` 构造规则一行提示；③本地/自建裸仓库无 Web 界面：
  `git merge --no-ff <branch>` 人工裁定（明示编排器永不代劳 merge）+ 托管站手动
  新建路径。`cli._do_push` 原样打印 note 不动。
- 回归（TestR14 2 项）：真临时仓库 + mock 无 gh + 本地 origin → note 含分支名/
  目标/"merge --no-ff"/"PR/MR"且 pr_url 不造假链接；GitHub 形态 mock → compare
  完整链接不丢 ✅

### P2 附带裁定记录（三个待裁点的用户裁定，均明确执行）
1. `adapters/base._git_name_status` 两处 `timeout=30`：**明确不扩范围**——证据快照
   探针非 runtime 主链路，已捕获超时 fail-safe 返回空集，接 config 反引语义混乱。
2. `runtime.timeout_seconds` 缺省 60 三处 fallback 重复：**已知重复、判定无害**——
   读取源已单一，抽常量需新增跨模块依赖，耦合成本 > 收益，保持现状。
3. R14 只要"可操作指引"，GitHub API 直连不实现。

## 五、P3 已知取舍（不改，记录）

- R15 路径升级偏严：`src/config/*.yaml` 也触发 REQUIRE——多问一次，不放过。
- R16 FORBID 自然语言 alias 偏宽：**宁宽勿松**，误触只是多打扰一次拒绝；
  任何"降低误触"的收紧放松提议都改变安全边界，维持现状。
- R17 cost.json 快照点：heal 每轮不落快照——预算门在 checkpoint 时判定，语义未破坏。
- R18 声明式 write_requests 审批链路：单测已覆盖（R9 用例即走声明链路），
  但**没有任何真实 agent 实战触发过**，标注"未经实战，待验证"。

## 六、验证命令备忘

```powershell
# 全量单测（99 项：P0 回归 10 + P1 回归 28 + P2 回归 17 + 既有 44）
python -m unittest discover -s tests
# stub e2e（R2/R1 端到端实证 + R13 残留断言 E2E-8/9，共 9 项；可连跑验证不自增）
python tests/e2e_stub.py
# R3 预算门手工复现（config 设 runtime.max_tokens: 1 → rc=3）
python cli.py run <task> --requirement "..." --yes --mock-llm
# R4 手工复现（无 --yes、stdin 关闭 → rc 2 + 提示）
cmd /c "python cli.py run <已建好questions的task> < NUL"
# R6 兜底清理（on_merge 语义手动入口，幂等）
python cli.py clean --task <task-id>
```
