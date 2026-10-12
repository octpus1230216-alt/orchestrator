#!/usr/bin/env python3
"""orchestrator.cli
多 agent 可插拔本地工作流编排器的命令行入口。

流水线阶段（`orch run` 一条龙全自动，仅 L3 权限闸门会打断）：
    对话挖掘需求(explore) → 确认(confirm) → 生成提示词(compile)
    → 交给 coding agent 执行(dispatch，可分段) → 验收(verify)
    → 失败自愈(heal，用尽则 report.md 交接) → 推送 GitHub 开 PR(push)

`--resume` 按 state.json.stages_done 跳过已完成阶段/段，从断点继续；
`--dry-run` 只跑到 compile；退出码见各 handle_* 文档字符串。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

import git_push
import subprocess
from adapters.base import AgentUnavailableError
from core import compile as compile_mod
from core import confirm as confirm_mod
from core import dispatch as dispatch_mod
from core import explore as explore_mod
from core import fingerprint
from core import heal as heal_mod
from core import sandbox as sandbox_mod
from core import skills as skills_mod
from core import state as state_mod
from core.state import BudgetLimits, TaskState, load as load_state

# 编排器根目录（orchestrator/），供各子命令定位 config.yaml 与 specs/
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
# 审计 R12：任务状态目录统一经 _specs_dir(config) 取（runtime.state_dir）；
# 此常量仅作无 config/未配 state_dir 时的兜底。
SPECS_DIR = ROOT / "specs"


def _specs_dir(config: dict | None) -> Path:
    """任务状态目录（审计 R12）：runtime.state_dir，默认 "specs" 相对 ROOT；
    绝对路径直接使用；未配置回退 SPECS_DIR 常量（行为与历史一致）。"""
    d = ((config or {}).get("runtime", {}) or {}).get("state_dir")
    if not d:
        return SPECS_DIR
    p = Path(d)
    return p if p.is_absolute() else ROOT / p


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器（含全部子命令与参数占位）。"""
    parser = argparse.ArgumentParser(
        prog="orch",
        description=(
            "orch — 多 agent 可插拔本地工作流编排器\n"
            "对话挖掘需求 → 生成提示词 → 交给 coding agent 执行 → 验收 → 推送 GitHub"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  orch run task-001 --requirement \"给登录接口加限流\" --agent codex\n"
            "  orch run task-001 --resume                     # 从最后成功阶段/段继续\n"
            "  orch run task-001 --requirement \"...\" --dry-run   # 只到 compile\n"
            "  orch run task-001 --requirement \"...\" --yolo      # L3 也放行（仍警告）\n"
            "  orch run task-001 --requirement \"...\" --segment per_segment\n"
            "  orch approve --task task-001\n"
            "  orch push --task task-001 --open-pr\n"
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_PATH,
        help="配置文件路径（默认: orchestrator/config.yaml）",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="输出调试日志",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        metavar="{run,approve,push,status,agents}",
    )

    # ---- orch run ----------------------------------------------------
    run_parser = subparsers.add_parser(
        "run",
        help="启动一次完整工作流（挖掘→提示词→执行→验收→自愈→PR）",
    )
    run_parser.add_argument(
        "task_id",
        nargs="?",
        default=None,
        metavar="TASK_ID",
        help="任务标识（specs/<task-id>）；新任务缺省时由需求自动生成",
    )
    run_parser.add_argument(
        "--requirement",
        type=str,
        default="",
        help="原始需求描述（新任务必填；留空则进入交互式对话挖掘）",
    )
    run_parser.add_argument(
        "--agent",
        type=str,
        default=None,
        help="指定 coding agent（codex/qoder/trae/cursor/claude_code/aider），"
        "默认取 config.yaml 的 agent",
    )
    run_parser.add_argument(
        "--resume",
        action="store_true",
        help="从 state.json 最后成功阶段/段继续（配合位置参数 TASK_ID）",
    )
    mode_group = run_parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--interactive", action="store_true",
        help="权限闸门模式：L3 逐条询问（无 TTY 直接 fail 不 hang）",
    )
    mode_group.add_argument(
        "--auto", action="store_true",
        help="权限闸门模式（默认）：只拦 L3；询问，CI 无 TTY 则 fail",
    )
    mode_group.add_argument(
        "--yolo", action="store_true",
        help="权限闸门模式：L3 放行+警告（forbidden 仍拒绝）",
    )
    run_parser.add_argument(
        "--max-budget",
        type=float,
        default=None,
        help="本次运行的预算上限（美元），超过即中止",
    )
    run_parser.add_argument(
        "--segment",
        type=str,
        choices=["whole", "per_segment", "auto"],
        default=None,
        help="分段喂入模式，覆盖 config.segment_mode；"
        "auto：prompt.md 字符数 < segment_threshold → whole，否则逐段自动喂入",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只生成 specs 与提示词（到 compile 为止），不派发 agent",
    )
    run_parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm 阶段全部采用默认答案（非交互）",
    )
    run_parser.add_argument(
        "--mock-llm",
        action="store_true",
        help="使用离线 mock LLM（无 key/无网时跑通 explore→compile）",
    )

    # ---- orch approve ------------------------------------------------
    approve_parser = subparsers.add_parser(
        "approve",
        help="查看权限闸门审批记录与被拦详情（重审走 run --resume --interactive）",
    )
    approve_parser.add_argument(
        "--task",
        type=str,
        required=True,
        metavar="TASK_ID",
        help="要审批的 task-id",
    )

    # ---- orch push ---------------------------------------------------
    push_parser = subparsers.add_parser(
        "push",
        help="验收通过后开 PR（永不直推 main）",
    )
    push_parser.add_argument(
        "--task",
        type=str,
        required=True,
        metavar="TASK_ID",
        help="要推送的 task-id",
    )
    push_parser.add_argument(
        "--open-pr",
        action="store_true",
        help="推送到隔离分支后创建 PR",
    )
    push_parser.add_argument(
        "--base",
        type=str,
        default="main",
        help="PR 的目标分支（默认: main）",
    )

    # ---- orch status -------------------------------------------------
    status_parser = subparsers.add_parser(
        "status",
        help="查看某个 task 的流水线状态",
    )
    status_parser.add_argument(
        "--task",
        type=str,
        required=True,
        metavar="TASK_ID",
        help="要查看的 task-id",
    )

    # ---- orch clean --------------------------------------------------
    clean_parser = subparsers.add_parser(
        "clean",
        help="清理 task 的隔离工作区（on_merge 语义兜底手动入口，幂等）",
    )
    clean_parser.add_argument(
        "--task",
        type=str,
        required=True,
        metavar="TASK_ID",
        help="要清理的 task-id",
    )

    # ---- orch agents -------------------------------------------------
    subparsers.add_parser(
        "agents",
        help="列出已注册的 coding agent 适配器",
    )

    return parser


def load_config(path: Path) -> dict:
    """读取并解析 config.yaml。"""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def init_task_dir(task_id: str, config: dict | None = None) -> Path:
    """从 specs/_template/ 播种新任务目录（携带全局 skills.md / acceptance.md 等接口）。

    审计 R12：目录基路径经 _specs_dir(config) 取（runtime.state_dir）；
    模板始终在仓库内 SPECS_DIR/_template（不随 state_dir 搬迁）。
    """
    import shutil

    base = _specs_dir(config)
    task_dir = base / task_id
    template = SPECS_DIR / "_template"
    if not task_dir.exists() and template.exists():
        shutil.copytree(template, task_dir)
    task_dir.mkdir(parents=True, exist_ok=True)
    return task_dir


def _checkpoint(task_dir: Path, st: TaskState, stage: str, limits: BudgetLimits,
                config: dict | None = None) -> bool:
    """标记阶段完成 + 落盘 + 写成本快照；返回是否仍预算内（False=已超限）。

    传入 config 时顺手接线指纹契约字段（审计 R1）：
    st.fingerprint_mode 取 config.fingerprint.mode，st.doc_id 由 doc_id(task_dir, config) 求值。
    切换 mode 只改 config，调用方代码不改（B7 设计）。
    """
    if config is not None:
        st.fingerprint_mode = fingerprint._mode_algo(config)[0]
        try:
            st.doc_id = fingerprint.doc_id(task_dir, config)
        except OSError as exc:  # 规格文件短暂不可读不阻断流水线
            print(f"[orch] [warn] doc_id 计算失败：{exc}", file=sys.stderr)
    state_mod.mark_done(st, stage)
    state_mod.save(task_dir, st)
    state_mod.record_cost_log(task_dir, st, note=stage)
    breach = state_mod.over_budget(st, limits)
    if breach:
        print(f"[orch] [暂停] {stage} 后预算超限（{breach}）：{state_mod.budget_progress(st, limits)}")
    return breach is None


def _run_dispatch(st: TaskState, task_dir: Path, config: dict, agent: str,
                  policy_mode: str, segment_override: str | None = None) -> int:
    """派发阶段：按 segment_mode 选整份/逐段；逐段时从 state.segment_index 续跑。

    退出码：0 成功；4 agent 不可用；5 forbidden/rejected；6 CI 无 TTY；7 段 heal 用尽。
    """
    try:
        adapter = dispatch_mod.load_adapter(agent, config)
        prompt_file = task_dir / "prompt.md"
        if not prompt_file.exists():
            print(f"[orch] [dispatch 无法执行] 缺少 {prompt_file}；"
                  f"请先完成 compile 阶段（新跑一次或检查 specs 目录完整性）。", file=sys.stderr)
            return 2
        worktree = sandbox_mod.create(st.task_id, config)
        st.worktree = str(worktree)
        prompt_md = prompt_file.read_text(encoding="utf-8")
        seg_mode = dispatch_mod.resolve_segment_mode(
            len(prompt_md), config, override=segment_override)
        resume_note = f"，从第 {st.segment_index + 1} 段起" if st.segment_index else ""
        print(f"[orch] 分段喂入={seg_mode}（prompt {len(prompt_md)} 字符，"
              f"threshold={config.get('segment_threshold', dispatch_mod.DEFAULT_SEGMENT_THRESHOLD)}）"
              f"{resume_note}")
        if seg_mode == dispatch_mod.SEG_PER:
            summary = dispatch_mod.run_segments(
                task_dir, adapter, config, worktree, mode=policy_mode, state=st)
        else:
            summary = dispatch_mod.run(
                task_dir, adapter, config, worktree, mode=policy_mode)
    except AgentUnavailableError as exc:
        print(f"[orch] [dispatch 未执行] {exc}", file=sys.stderr)
        state_mod.save(task_dir, st)
        return 4
    except KeyError as exc:  # config.adapters 未注册该 agent → 与不可用同级处理，不裸 traceback
        print(f"[orch] [dispatch 未执行] agent 配置缺失：{exc}", file=sys.stderr)
        state_mod.save(task_dir, st)
        return 4
    except NotImplementedError as exc:
        # 审计 R8：type: http 型 adapter 明确报错（不静默当 cli 跑），与不可用同级 rc=4
        print(f"[orch] [dispatch 未执行] {exc}", file=sys.stderr)
        state_mod.save(task_dir, st)
        return 4

    print(
        f"[orch] dispatch 完成：exit_code={summary['exit_code']} "
        f"改动={len(summary['files_changed'])} 个文件 判定={summary['status']}"
    )
    if summary["blocked"]:
        print(f"[orch] {summary['message']}", file=sys.stderr)
        if summary["status"] in ("forbidden", "rejected"):
            print(f"[orch] 详见 {task_dir/'blocked.md'} 与 approvals.log（orch approve --task {st.task_id}）")
            state_mod.save(task_dir, st)
            return 5
        if summary["status"] == "ci_fail":
            print("[orch] CI 无 TTY，L3 无法审批 → fail（不 hang）。", file=sys.stderr)
            state_mod.save(task_dir, st)
            return 6
        if summary["status"] == "segment_failed":
            print(f"[orch] SEGMENT {summary.get('failed_segment')} heal 用尽 → 失败交接；"
                  f"已完成段产物已保留，修复后 --resume 从失败段继续。", file=sys.stderr)
            state_mod.save(task_dir, st)
            return 7
    if not summary["ok"]:
        print("[orch] agent 执行失败（exit!=0）。whole 模式无逐段 heal；"
              "可用 --segment per_segment 获得逐段自愈与段级断点。", file=sys.stderr)
        state_mod.save(task_dir, st)
        return 8
    state_mod.mark_done(st, "dispatch")
    state_mod.save(task_dir, st)
    return 0


def _policy_mode(args: argparse.Namespace, config: dict) -> str:
    """权限闸门模式：--yolo/--interactive/--auto 互斥 flag 优先，否则 config.policy.default。"""
    if getattr(args, "yolo", False):
        return "yolo"
    if getattr(args, "interactive", False):
        return "interactive"
    if getattr(args, "auto", False):
        return "auto"
    return config.get("policy", {}).get("default", "auto")


def handle_run(args: argparse.Namespace, config: dict) -> int:
    """处理 `orch run`：全自动流水线 explore→confirm→compile→dispatch→verify+heal→push。

    已完成的阶段（state.stages_done）自动跳过不重跑 —— 这就是 --resume 语义；
    逐段模式下段游标 state.segment_index 同样从最后未成功段继续。
    退出码：0 成功；2 缺参/缺产物；3 预算超限；4 agent 不可用；
    5 forbidden/rejected；6 CI 无 TTY；7 heal 用尽（见 report.md）；8 whole 模式 exit!=0；9 push 失败。
    """
    limits = BudgetLimits.from_config(config)
    if args.max_budget is not None:
        limits.max_usd = args.max_budget
    policy_mode = _policy_mode(args, config)
    if args.mock_llm:
        llm_cfg = config.setdefault("llm", {})
        for tier in ("strong", "fast"):
            llm_cfg.setdefault(tier, {})["model"] = "mock"

    # 审计 R3：配了 max_usd 却没配单价 → 明确警告（不静默；usd 将恒 0）
    if limits.max_usd:
        _llm = config.get("llm", {}) or {}
        if not any((_llm.get(t) or {}).get(k)
                   for t in ("strong", "fast")
                   for k in ("price_per_1k_in", "price_per_1k_out")):
            print("[orch] [warn] 已设 runtime.max_usd 但 llm 单价（price_per_1k_in/out）未配置："
                  "usd 恒 0，max_usd 门不会触发（tokens/calls 门仍有效）", file=sys.stderr)

    # 审计 R3：计费回调（无全局账本）：每次 LLM 调用真实累计 tokens/usd → 预算门可触发
    def _on_cost(rec: dict) -> None:
        state_mod.add_cost(st, llm_calls=rec["llm_calls"],
                           tokens=rec["tokens"], usd=rec["usd"])

    # ---- 定位任务：已有 state.json 则续跑（阶段跳过），否则新建 ----------
    # 审计 R12：resume 统一读 runtime.resume（默认 true，行为不变）；
    # 配 false 时拒绝静默续跑，必须显式 --resume。
    cfg_resume = bool((config.get("runtime", {}) or {}).get("resume", True))
    task_dir = _specs_dir(config) / args.task_id if args.task_id else None
    if task_dir is not None and (task_dir / "state.json").exists():
        if not args.resume and not cfg_resume:
            print(f"[orch] 已存在 {task_dir / 'state.json'} 且 runtime.resume=false："
                  f"拒绝静默续跑。继续请显式 --resume；另起炉灶请换新 task-id "
                  f"或删除旧状态目录", file=sys.stderr)
            return 2
        st = load_state(task_dir)
        if args.requirement:
            print("[orch] 任务已存在，--requirement 忽略（需求已挖掘进 context.md）")
        print(f"[orch] 续跑 task={st.task_id}（已完成阶段 {st.stages_done} 跳过）  "
              f"{state_mod.budget_progress(st, limits)}")
    else:
        if args.resume:
            print(f"[orch] 无法续跑：未找到 {task_dir}/state.json", file=sys.stderr)
            return 2
        if not args.requirement and not args.task_id:
            print("[orch] 需提供 TASK_ID 或 --requirement（两者均缺时无法启动）", file=sys.stderr)
            return 2
        task_id = args.task_id or fingerprint.make_task_id(args.requirement)
        task_dir = init_task_dir(task_id, config)
        agent = args.agent or config.get("agent", "codex")
        print(f"[orch] task-id={task_id}  agent={agent}  mode={policy_mode}  specs={task_dir}")
        st = TaskState(task_id=task_id, agent=agent)
        st.worktree = str(Path(config.get("sandbox", {}).get("worktree_root", ".orch-worktrees"))
                          / task_id)
        # 多 skill 选用：explicit 优先，否则按任务标签自动选用（记入 state.skills_used）
        st.skills_used = skills_mod.select(args.requirement, config, ROOT)
        state_mod.save(task_dir, st)
        if st.skills_used:
            print(f"[orch] skills_used={st.skills_used}（explore←约束/偏好，compile←输出规范/反例）")

    agent = args.agent or st.agent or config.get("agent", "codex")
    st.agent = agent
    requirement = args.requirement or ""
    breach = state_mod.over_budget(st, limits)
    if breach:
        print(f"[orch] [暂停] 预算超限（{breach}）：累计 {st.budget}；"
              f"调高 --max-budget 后 `orch run {st.task_id} --resume` 继续。",
              file=sys.stderr)
        return 3

    def skipped(stage: str) -> bool:
        if stage in st.stages_done:
            print(f"[orch] ({stage}) 已完成，跳过")
            return True
        return False

    # ---- (1) explore ----------------------------------------------
    if not skipped("explore"):
        print("[orch] (1/6) explore 需求挖掘 …")
        explore_mod.run(task_dir, requirement, config,
                        skills_text=skills_mod.for_explore(st.skills_used, config, ROOT),
                        on_cost=_on_cost)
        state_mod.log_event(task_dir, "explore", "done")
        if not _checkpoint(task_dir, st, "explore", limits, config):
            return 3

    # ---- (2) confirm ----------------------------------------------
    if not skipped("confirm"):
        print(f"[orch] (2/6) confirm …{'（--yes 全默认）' if args.yes else ''}")
        try:
            confirm_mod.run(task_dir, config, assume_yes=args.yes)
        except confirm_mod.NonInteractiveError as exc:
            # 审计 R4：无 TTY 且未 --yes → 清晰提示 + 退出码 2，不 hang 不 traceback
            print(f"[orch] [confirm 中断] {exc}", file=sys.stderr)
            state_mod.save(task_dir, st)
            return 2
        state_mod.log_event(task_dir, "confirm", "done", assume_yes=args.yes)
        if not _checkpoint(task_dir, st, "confirm", limits, config):
            return 3

    # ---- (3) compile ----------------------------------------------
    if not skipped("compile"):
        print("[orch] (3/6) compile 生成提示词 …")
        segments = compile_mod.run(task_dir, config,
                                   skills_text=skills_mod.for_compile(st.skills_used, config, ROOT),
                                   on_cost=_on_cost)
        state_mod.set_segments(st, [s["index"] for s in segments])
        state_mod.log_event(task_dir, "compile", "done", segments=len(segments))
        if not _checkpoint(task_dir, st, "compile", limits, config):
            return 3

    prompt_path = task_dir / "prompt.md"
    print(f"[orch] [OK] 提示词就绪 {prompt_path}")

    if args.dry_run:
        print("[orch] --dry-run：只到 compile，不派发 agent。"
              f"继续：`orch run {st.task_id} --resume`")
        return 0

    # ---- (4) dispatch（逐段模式从 state.segment_index 续跑） ----------
    if not skipped("dispatch"):
        print(f"[orch] (4/6) dispatch → agent={agent} mode={policy_mode} …")
        code = _run_dispatch(st, task_dir, config, agent, policy_mode, args.segment)
        if code:
            return code

    # ---- (5) verify + heal（全局回灌循环，首验也由 heal.run 执行） ------
    if not skipped("verify"):
        # 审计 R7：verify_parallel 为预留字段——配了真值不静默忽略，明确提示仍串行；
        # false/缺省 → 不打（不误报已并行）。不重写 verify.py，不加并发代码。
        if config.get("verify_parallel"):
            print("[orch] verify_parallel=true 为预留字段，本批暂不生效：验收仍逐条串行"
                  "（原因：setup/teardown 顺序依赖与 worktree 共享状态）", file=sys.stderr)
        worktree = Path(st.worktree) if st.worktree else sandbox_mod.create(st.task_id, config)
        st.worktree = str(worktree)
        try:
            adapter = dispatch_mod.load_adapter(agent, config)
        except AgentUnavailableError as exc:
            print(f"[orch] [verify/heal 无法执行] {exc}", file=sys.stderr)
            state_mod.save(task_dir, st)
            return 4
        except NotImplementedError as exc:
            # 审计 R8：http 型明确报错（防裸 traceback）
            print(f"[orch] [verify/heal 无法执行] {exc}", file=sys.stderr)
            state_mod.save(task_dir, st)
            return 4
        print(f"[orch] (5/6) verify 验收 + heal 自愈（上限 {heal_mod.max_retries(config)} 轮）…")
        try:
            outcome = heal_mod.run(task_dir, adapter, config, worktree,
                                   policy_mode=policy_mode, state=st)
        except NotImplementedError as exc:
            # 审计 R8：http 型在 heal 内首次执行 adapter.run 时报错 → 同样收为 rc=4
            print(f"[orch] [verify/heal 无法执行] {exc}", file=sys.stderr)
            state_mod.save(task_dir, st)
            return 4
        rpt = outcome.get("report", {})
        print(f"[orch] verify 结果：passed={rpt.get('passed')} "
              f"total={rpt.get('total')} heal 轮次={outcome.get('attempts')}")
        status = outcome["status"]
        if status == "verified":
            state_mod.log_event(task_dir, "verify", "passed", attempts=outcome.get("attempts"))
            if not _checkpoint(task_dir, st, "verify", limits, config):
                return 3
        else:
            print(f"[orch] [停] verify/heal 结果={status}：{outcome.get('message', rpt.get('required_failed'))}",
                  file=sys.stderr)
            if status == "heal_exhausted":
                print(f"[orch] 已保留隔离工作区：{outcome.get('worktree')}；"
                      f"交接文档 {task_dir/'report.md'}；修复后 `orch run {st.task_id} --resume` 从 verify 继续。",
                      file=sys.stderr)
                state_mod.save(task_dir, st)
                return 7
            state_mod.save(task_dir, st)
            return 5 if status in ("forbidden", "rejected", "blocked") else 6

    # ---- (6) push + PR（config.git.auto_push 时自动；否则提示手动） -----
    git_cfg = config.get("git", {})
    if not skipped("push"):
        if git_cfg.get("auto_push", False):
            print("[orch] (6/6) push + PR …")
            code = _do_push(st, task_dir, config,
                            base=git_cfg.get("base_branch", "main"),
                            open_pr_flag=git_cfg.get("open_pr", True))
            if code:
                return code
        else:
            print(f"[orch] (6/6) push 未启用（config.git.auto_push=false）；"
                  f"验收已通过，可执行：orch push --task {st.task_id} --open-pr")

    print(f"[orch] [完成] 全部阶段已过：{st.stages_done}")
    if st.extras.get("pr_url"):
        print(f"[orch] PR: {st.extras['pr_url']}")
    return 0


def _do_push(st: TaskState, task_dir: Path, config: dict, *,
             base: str = "main", open_pr_flag: bool = True) -> int:
    """推送阶段：隔离分支补提交 → push（永不 force）→ 开 PR（gh 缺失则回退 compare 链接）。

    红线：只推 feat/<task-id> 隔离分支，永不直推 main；命中即拒绝。
    退出码：0 成功；5 直推主干被拒；9 push 失败。
    """
    worktree = Path(st.worktree) if st.worktree else Path(".")
    if not worktree.exists():
        print(f"[orch] [push 无法执行] 隔离工作区不存在：{worktree}（先跑完 dispatch）",
              file=sys.stderr)
        return 9
    try:
        git_push.ensure_committed(worktree, f"[orch] {st.task_id}: agent 产出（编排器补提交）",
                                  config)
        branch = sandbox_mod.branch_name(st.task_id, config)
        res = git_push.push_branch(worktree, branch, config)
    except git_push.DirectPushForbidden as exc:
        print(f"[orch] [push 拒绝] {exc}", file=sys.stderr)
        return 5
    if not res.pushed:
        print(f"[orch] [push 失败] {res.note}", file=sys.stderr)
        state_mod.save(task_dir, st)
        return 9
    print(f"[orch] 已推送隔离分支 {branch}（非 force，未触碰 main）")

    pr_url, note = "", res.note
    if open_pr_flag:
        body = f"由 orchestrator 自动创建。task={st.task_id}，验收已通过。"
        blocked = task_dir / "blocked.md"
        if blocked.exists():
            body += "\n\n附：上一轮失败交接 blocked.md（已用 heal 轮次与卡点）\n\n" \
                    + blocked.read_text(encoding="utf-8")[:2000]
        pr = git_push.open_pr(worktree, branch, base, config,
                              title=f"[orch] {st.task_id}", body=body)
        pr_url, note = pr.pr_url, pr.note
        if pr_url:
            print(f"[orch] PR 已创建：{pr_url}")
        else:
            print(f"[orch] 未自动建 PR（{note}）；分支已推送，可手动打开："
                  f"{base} ← {branch}")
    st.extras["pr_url"] = pr_url
    st.extras["push_note"] = note
    state_mod.mark_done(st, "push")
    state_mod.save(task_dir, st)
    state_mod.record_cost_log(task_dir, st, note="push")
    state_mod.log_event(task_dir, "push", "done", branch=branch, pr_url=pr_url)
    # 审计 R6：push 后接 cleanup 策略（always 立即清理；on_merge 判已合并则清理）
    _maybe_cleanup(st, config)
    return 0


def _branch_merged(worktree: Path, config: dict) -> bool | None:
    """feat 分支是否已并入 origin/base（审计 R6：on_merge 的实际判定）。

    fetch + merge-base --is-ancestor HEAD origin/<base>；
    无法判定（无网/非仓库/命令失败）返回 None，不抛异常。
    """
    git_cfg = config.get("git", {})
    remote = git_cfg.get("remote", "origin")
    base = git_cfg.get("base_branch", "main")
    timeout = int(config.get("runtime", {}).get("timeout_seconds", 60))

    def _g(args: list[str]):
        try:
            return subprocess.run(["git", *args], cwd=str(worktree),
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None

    if _g(["fetch", remote, base]) is None:
        return None
    mb = _g(["merge-base", "--is-ancestor", "HEAD", f"{remote}/{base}"])
    if mb is None or mb.returncode not in (0, 1):
        return None
    return mb.returncode == 0


def _maybe_cleanup(st: TaskState, config: dict) -> None:
    """按 sandbox.cleanup 策略处理隔离工作区（审计 R6，不 throw）。

    never → 不动；always → push 后立即 destroy；
    on_merge → 仅在能确认分支已合入 base 时清理，否则提示 `orch clean` 兜底。
    """
    cleanup = (config.get("sandbox", {}) or {}).get("cleanup", "on_merge")
    if cleanup == "never" or not st.worktree:
        return
    wt = Path(st.worktree)
    if not wt.exists():
        return  # 已清理过，幂等静默
    if cleanup == "always":
        sandbox_mod.destroy(st.task_id, config, work_dir=wt)
        return
    if cleanup != "on_merge":
        return
    merged = _branch_merged(wt, config)
    base = config.get("git", {}).get("base_branch", "main")
    if merged is True:
        print(f"[orch] 分支已合入 {base}，清理隔离工作区 …")
        sandbox_mod.destroy(st.task_id, config, work_dir=wt)
    elif merged is False:
        print(f"[orch] 分支尚未合入 {base}：保留隔离工作区；"
              f"合并后 `orch clean --task {st.task_id}`（或再跑 orch status 自动清理）")
    else:
        print("[orch] 无法确认合并状态（fetch 不可用）：需要清理时用 `orch clean --task …`",
              file=sys.stderr)


def handle_push(args: argparse.Namespace, config: dict) -> int:
    """处理 `orch push`：验收通过的 task 推隔离分支并（可选）开 PR。"""
    task_dir = _specs_dir(config) / args.task
    state_file = task_dir / "state.json"
    if not state_file.exists():
        print(f"[orch] 未找到 {state_file}", file=sys.stderr)
        return 2
    st = load_state(task_dir)
    if "verify" not in st.stages_done:
        print(f"[orch] 拒绝 push：验收（verify）尚未通过。先 `orch run {st.task_id} --resume`。",
              file=sys.stderr)
        return 2
    return _do_push(st, task_dir, config, base=args.base, open_pr_flag=args.open_pr)


def handle_status(args: argparse.Namespace, config: dict) -> int:
    """处理 `orch status`：打印某 task 的流水线进度与成本。"""
    task_dir = _specs_dir(config) / args.task
    state_file = task_dir / "state.json"
    if not state_file.exists():
        print(f"[orch] 未找到 {state_file}", file=sys.stderr)
        return 2
    st = load_state(task_dir)
    marks = []
    for s in state_mod.STAGES:
        marks.append(f"[{ 'x' if s in st.stages_done else ' ' }] {s}")
    print(f"task   : {st.task_id}  agent={st.agent}")
    print(f"stages : {' '.join(marks)}")
    print(f"stage  : {st.stage}  segment={st.segment_index}/{len(st.segments)}  "
          f"heal_attempts={st.heal_attempts}")
    print(f"budget : llm_calls={st.budget.llm_calls} tokens={st.budget.tokens} usd={st.budget.usd}")
    print(f"specs  : {task_dir}")
    print(f"worktree: {st.worktree or '（尚未创建）'}  skills_used={st.skills_used or '[]'}")
    if st.extras.get("failed_segment") is not None:
        print(f"⚠ 失败段：SEGMENT {st.extras['failed_segment']}（修复后 --resume 从该段继续）")
    for name in ("report.md", "blocked.md"):
        if (task_dir / name).exists():
            print(f"⚠ 存在 {name}：先看交接文档再决定下一步")
    if st.extras.get("pr_url"):
        print(f"pr     : {st.extras['pr_url']}")
    # 审计 R6：push 完成后查看状态时顺带触发 on_merge 检测（合并后自动清理）
    if "push" in st.stages_done:
        _maybe_cleanup(st, config)
    nxt = st.next_stage()
    print(f"next   : {nxt if nxt else '全部完成'}"
          + ("" if nxt else "" if not st.extras.get('pr_url') else "（PR 已开）"))
    return 0


def handle_approve(args: argparse.Namespace, config: dict) -> int:
    """处理 `orch approve`：展示待审批上下文（approvals.log + blocked.md）。"""
    task_dir = _specs_dir(config) / args.task
    if not task_dir.exists():
        print(f"[orch] 未找到 {task_dir}", file=sys.stderr)
        return 2
    log = task_dir / "logs" / "approvals.log"
    if log.exists():
        print(f"[orch] 审批日志 {log}（末 20 行）：")
        lines = log.read_text(encoding="utf-8").splitlines()
        for line in lines[-20:]:
            print("  " + line)
    else:
        print(f"[orch] 尚无审批记录（{log} 不存在）")
    blocked = task_dir / "blocked.md"
    if blocked.exists():
        print(f"\n[orch] 被拦详情见 {blocked}")
    print(f"\n[orch] 重新审批并继续：`orch run {args.task} --resume --interactive`")
    return 0


def handle_clean(args: argparse.Namespace, config: dict) -> int:
    """`orch clean`：手动清理 task 的隔离工作区（审计 R6 兜底入口，幂等）。"""
    task_dir = _specs_dir(config) / args.task
    state_file = task_dir / "state.json"
    if not state_file.exists():
        print(f"[orch] 未找到 {state_file}", file=sys.stderr)
        return 2
    st = load_state(task_dir)
    if not st.worktree or not Path(st.worktree).exists():
        print(f"[orch] 隔离工作区不存在（已清理过）：{st.worktree or '（尚未创建）'}")
        return 0
    ok = sandbox_mod.destroy(st.task_id, config, work_dir=Path(st.worktree))
    return 0 if ok else 1


def handle_agents(config: dict) -> int:
    """列出已注册的 coding agent 适配器及其 CLI 可用性。"""
    import shlex
    import shutil

    default = config.get("agent", "codex")
    print(f"[orch] 默认 agent: {default}（config.agent，--agent 可覆盖）")
    for name in config.get("adapters", {}):
        ad = dispatch_mod.load_adapter(name, config)
        tpl = ad.command_template
        if not tpl:
            print(f"  {name:<12} [未配置 command_template] {ad.install_hint}")
            continue
        exe = shlex.split(tpl)[0]
        mark = "可用" if shutil.which(exe) else "未安装"
        caps = ",".join(k for k, v in ad.capabilities.items() if v) or "-"
        print(f"  {name:<12} [{mark}] {tpl}")
        print(f"               capabilities: {caps}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口：run 为全自动流水线；approve/push/status/agents 均已接入。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    config = load_config(args.config)

    if args.command == "run":
        return handle_run(args, config)

    if args.command == "agents":
        return handle_agents(config)

    if args.command == "push":
        return handle_push(args, config)

    if args.command == "status":
        return handle_status(args, config)

    if args.command == "clean":
        return handle_clean(args, config)

    if args.command == "approve":
        return handle_approve(args, config)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
