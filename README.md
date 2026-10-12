# orch — 多 agent 可插拔本地工作流编排器

把「对话挖掘需求 → 生成提示词 → 交给 coding agent 执行 → 验收 → 失败自愈 → 推送 GitHub 开 PR」
串成一条全自动本地流水线。**只有触发 L3 权限闸门（改公共接口/依赖/CI/删文件等）才会打断你。**

```
orch run task-001 --requirement "给登录接口加限流"
        │
        ▼
  explore ──▶ confirm ──▶ compile ──▶ dispatch ──▶ verify ──▶ heal ──▶ push(PR)
  需求挖掘     逐题确认     生成提示词    隔离 worktree   逐条执行     失败回灌     推隔离分支
  context.md   answers.json  prompt.md    喂给 agent     acceptance   ≤max_retry   + gh pr create
  questions.json            acceptance   (codex/qoder/…  命令         轮次         永不直推 main
                                         /segment 分段)                             永不 force push
```

---

## 1. 安装

```powershell
# Python 3.10+，仅依赖 pyyaml（rich 可选，装了 confirm 交互更好看）
python -m pip install pyyaml

# 任选一个 coding agent 装到 PATH（编排器通过它们的 CLI 派发）
npm install -g @openai/codex                # codex
npm install -g @anthropic-ai/claude-code    # claude
python -m pip install aider-chat            # aider

# 可选：GitHub PR 自动化
# gh CLI（https://cli.github.com/）并 `gh auth login`；未安装时 push 会回退为 compare 链接
```

验证：

```powershell
cd orchestrator
python cli.py agents        # 列出已注册 adapter 及其 CLI 可用性
```

## 2. 配置（config.yaml）

| 段 | 作用 | 关键键 |
|---|---|---|
| `llm` | 编排器自用模型（挖掘/编译提示词） | `strong/fast.model`；密钥只写 `env:OPENAI_API_KEY`，不落盘 |
| `agent` | 默认 coding agent | 可被 `--agent` 覆盖 |
| `adapters` | 每家 agent 的命令模板与能力声明 | `command_template`（支持 `{prompt_file}` / `{prompt_inline}`）、`capabilities`、`install_hint` |
| `policy` | 权限闸门三档 | `default: auto`、`require_approval`(L3 八项)、`forbidden`(force_push/push_main_directly)、`record_levels:[L2]` |
| `segment_mode` / `segment_threshold` | 分段喂入 | `auto`：prompt 字符数 < 阈值 → whole，否则逐段 |
| `skills` | 多 skill 能力库 | `dir: skills/`、`auto_select: true`、`explicit: []` |
| `fingerprint` | 文档指纹策略 | `mode: file_bytes \| content`（见 §7） |
| `sandbox` | 隔离执行环境 | 每次运行独立 `git worktree` + 分支 `feat/<task-id>` |
| `git` | 推送与 PR | `remote`、`main_branch`、`auto_push`、`open_pr`、`pr.draft_by_default` |
| `runtime` / `max_retry` | 预算与自愈上限 | `max_usd/max_llm_calls/max_tokens`、`max_retry`（heal 轮次） |
| `timeouts` | 超时 | `agent`（派发）、`verify_cmd`（验收命令缺省） |

密钥约定：**任何 api_key/token 只允许 `env:` 引用环境变量**，所有日志落盘前都过脱敏（§6）。

## 3. 用法

```powershell
# 全自动：一句需求 → PR（仅 L3 打断）
python cli.py run task-001 --requirement "给登录接口加限流" --agent codex --yes

# 只到 compile（生成 specs 与提示词，不派发）
python cli.py run task-001 --requirement "..." --dry-run

# 断点续跑：跳过 state.json 里已完成阶段；逐段模式从最后未成功段继续
python cli.py run task-001 --resume

# 强制逐段喂入（长 prompt 防 token 爆炸；每段独立 heal 与 checkpoint）
python cli.py run task-001 --requirement "..." --segment per_segment

# 权限模式（互斥；默认 auto）
python cli.py run task-001 --requirement "..." --interactive   # L3 逐条询问
python cli.py run task-001 --requirement "..." --yolo          # L3 放行+警告（forbidden 仍拒绝）

# 查看 / 推送 / 审批
python cli.py status --task task-001
python cli.py push   --task task-001 --open-pr [--base main]
python cli.py approve --task task-001
```

TASK_ID 可省略：新任务给 `--requirement` 时自动生成 `<slug>-<hash8>`。

### 退出码

| 码 | 含义 | 下一步 |
|---|---|---|
| 0 | 成功（或 dry-run 完成） | — |
| 2 | 缺参数 / 缺产物（prompt.md、state.json 不在） | 补齐输入或从头跑 |
| 3 | 预算超限（max_usd/llm_calls/tokens） | 调高 `--max-budget` 后 `--resume` |
| 4 | agent CLI 不可用 | 按 `install_hint` 安装或 `--agent` 换一家 |
| 5 | forbidden / 人工 reject | 看 `specs/<id>/blocked.md` 与 approvals.log |
| 6 | CI 无 TTY 遇 L3 → fail（不 hang） | 有终端后 `--resume --interactive` 补审 |
| 7 | heal 用尽 / 段失败交接 | 看 `specs/<id>/report.md`，修复后 `--resume` |
| 8 | whole 模式 agent exit≠0 | 改用 `--segment per_segment` 获得逐段自愈 |
| 9 | push 失败 | 检查 remote/网络后 `orch push --task ...` |

## 4. 新增一家 coding agent（3 步）

以新增 `windsurf` 为例，**不需要改 core/ 任何代码**：

1. **加一个薄 adapter**：`adapters/windsurf.py`

   ```python
   from typing import Any
   from .base import AgentAdapter

   class WindsurfAdapter(AgentAdapter):
       name = "windsurf"

   def build(config: dict[str, Any]) -> WindsurfAdapter:
       return WindsurfAdapter(config)
   ```

2. **config.yaml 的 `adapters:` 下加一条**：

   ```yaml
   windsurf:
     command_template: "windsurf run --file {prompt_file}"   # 或 {prompt_inline}
     install_hint: "安装 Windsurf CLI 后重试"
     capabilities: { supports_prompt_file: true, supports_streaming: false,
                     returns_diff: true, supports_write_requests: false, supports_resume: false }
   ```

3. **跑起来**：`python cli.py run t1 --requirement "..." --agent windsurf`

写操作证据两条路径自动择一：`supports_write_requests` → 解析 agent 声明；否则执行前后
`git diff --name-status` 推断——都由 policy 三档判定，adapter 作者无需实现。

## 5. 权限模型（闸门）

三档判定，优先级 **forbidden > L3 require > 路径升级 > 默认 auto**：

| 档 | 覆盖 | 行为 |
|---|---|---|
| L1 | 格式、内部实现修改 | 全自动放行，不打扰 |
| L2 | 新增内部模块/测试、内部重构、改名 | 放行，但记 `logs/change-summary.md` |
| L3 | 改公共签名 / 需求范围 / schema / 依赖 / CI·config·lock·Dockerfile / 删文件 / 安全鉴权支付 / merge PR | **必须问**（auto/interactive 询问；无 TTY 则 fail） |
| forbidden | force push、直推 main | **任何模式拒绝** + 回灌替代建议 + blocked.md |

模式矩阵（`--interactive / --auto / --yolo`，默认取 `config.policy.default`）：

| 操作 \ 模式 | interactive | auto（默认） | yolo |
|---|---|---|---|
| L1 / L2 | 放行 | 放行 | 放行 |
| L3 | 询问 | 询问；**CI 无 TTY → fail，不 hang** | 放行 + 警告（仍审计） |
| forbidden | 拒绝 | 拒绝 | 拒绝 |

交互输入：`[a]pprove` `[r]eject` `[e]dit`（修订原因后重问） `[A]pprove all in this category`。
所有决策写 `specs/<task-id>/approvals.log`（时间|决策|模式|操作|目标|原因）。
**agent 不得自主越权，只能申请**；L3 优先级高于默认 auto（危险优先）。

## 6. 可观测性

`specs/<task-id>/` 下一次运行的全部痕迹（文件即接口）：

```
state.json               阶段游标（stages_done/segment_index/heal_attempts/budget/doc_id）
context.md questions.json answers.json prompt.md acceptance.md
logs/dispatch-<ts>[-segN-tryI].log   每次派发 stdout/stderr 分开存
logs/heal-<n>.log                    每轮自愈 stdout/stderr 分开存
logs/events.jsonl                    结构化事件（JSON lines，逐阶段追加）
logs/cost.json                       成本快照（llm_calls/tokens/usd）
logs/change-summary.md               L2 自动放行留档
approvals.log blocked.md report.md   审计 / 拒绝交接 / 失败交接
result/verify_report.json            验收结果（passed/required_failed/每条明细）
```

**脱敏**：`api_key/token/secret/authorization/bearer` 的取值在任何日志落盘前一律替换为 `***`，密钥不进 log。

**失败交接**（heal 用尽）：保留隔离 worktree 并打印路径 → 生成 `report.md`
（做了什么 / 卡在哪 / 已试轮次 / 建议下一步）；可选 `orch push --open-pr` 开 draft PR 附 blocked.md @ 评审人。

## 7. 文档指纹（fingerprint）

背景：python-docx / openpyxl / python-pptx 保存会把 zip 时间戳写进包内，
**同一内容两次导出的字节必然不同** → `doc_id=sha256(file_bytes)` 变化 → 被判为新文档。
因此「同一份文件重跑幂等」成立；「重新导出的文件」会被当新文档（已知副作用，详见
`core/fingerprint.py` 模块注释）。

- 生产语义保持 `mode: file_bytes`（需求明确 sha256(file_bytes)）；
- 需求变为「跨导出保持 doc_id 稳定」时：`config.fingerprint.mode` 改 `content`，
  启用 normalize（忽略 zip 时间戳/docProps 等），**调用方 `compute_doc_id(path)` / `doc_id(task_dir)` 不改**；
- content 模式对「未重新导出」的普通文本文件回退原始字节哈希，与 file_bytes 值一致
  （切换不是迁移）；zip 容器历史上已落 file_bytes 值的，切换后首轮视为一次性迁移。

## 8. 约束与红线（代码强制）

- **永不 force push**：`git push` 只带 `-u`，无 force 参数路径；policy 层命中即 FORBID
- **永不直推 main**：`git_push._guard_base_branch` 二次校验 + 只推 `feat/<task-id>`，上线只走 PR
- **L3 优先级高于默认 auto**（危险优先），agent 只能申请
- **CI 遇 REQUIRE 必须 fail，不得 hang**（无 TTY 直接 ci_fail + 待批清单）
- **密钥不进 log**（state.redact 全覆盖）
- 所有执行都在隔离 `git worktree`，主工作区不被 agent 污染

## 9. 分段喂入与多 skill

- `segment_mode: auto` + `--segment` 覆盖：长 prompt.md 按 `## SEGMENT n` 逐段自动喂入；
  段间上下文只注入 context.md 全文 + 前序段文件清单 + 一行摘要（**不重塞整份前序 prompt**）；
  每段完成写 checkpoint，失败只对该段 heal，`--resume` 从最后未成功段继续。
- `skills/` 目录（backend/frontend/data/common.md，四分区：约束/偏好/输出规范/反例）：
  `explicit` 非空只用列出的，否则按任务关键词 auto_select；选用结果记 `state.skills_used`，
  explore 注入「约束/偏好」、compile 注入「输出规范/反例」；单一 `skills.md` 兼容。

## 10. 测试

```powershell
cd orchestrator
python -m unittest discover -s tests -v
```

覆盖：adapter 变量替换、prompt 分段、verify 解析、policy 判定（auto/require/forbid）、
approval 无 TTY 行为、指纹双模式切换（tests/ 共 44 项，标准库零依赖）。
