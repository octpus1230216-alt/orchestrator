# 可重复的 stub e2e 回归（非 unittest 自动发现，手动跑：python tests/e2e_stub.py）：
# stub agent + 本地 bare 仓库，验证：
#   R2: feat 分支/PR 内容无 .orch/，change-summary 无 .orch/，hello.txt 正常在内
#   R1: state.json 的 doc_id / fingerprint_mode 非空且与 fingerprint.doc_id 一致
#   整体: 一句需求 → explore..push 全自动 rc=0（仅 mock LLM，无外部依赖）
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PY = Path(sys.executable)

STUB = '''
import pathlib, sys
pf = None
argv = sys.argv[1:]
if "--prompt-file" in argv:
    pf = argv[argv.index("--prompt-file") + 1]
p = pathlib.Path(pf) if pf else None
print("stub: prompt_file exists =", bool(p and p.exists()), "head =", (p.read_text(encoding="utf-8")[:40] if p and p.exists() else ""))
pathlib.Path("hello.txt").write_text("hi\\n", encoding="utf-8")
print("stub done")
'''


def run(args, cwd, input=""):
    return subprocess.run(args, cwd=str(cwd), input=input, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="p0e2e_"))
    repo = tmp / "repo"
    repo.mkdir()
    bare = tmp / "remote.git"
    ok = True
    fails = []

    # 目标仓库：main + 被跟踪文件
    assert run(["git", "init", "-b", "main", str(repo)], tmp).returncode == 0
    (repo / "tracked_file.txt").write_text("base\n", encoding="utf-8")
    run(["git", "config", "user.email", "t@t"], repo)
    run(["git", "config", "user.name", "t"], repo)
    run(["git", "add", "-A"], repo)
    run(["git", "commit", "-m", "init"], repo)
    run(["git", "init", "--bare", str(bare)], tmp)
    run(["git", "-C", str(repo), "remote", "add", "origin", str(bare)], tmp)

    stub = tmp / "stub.py"
    stub.write_text(STUB, encoding="utf-8")

    cfg = {
        "version": 1,
        "agent": "codex",
        "max_retry": 1,
        "timeouts": {"agent": 60, "verify_cmd": 30},
        "segment_mode": "auto", "segment_threshold": 6000,
        "llm": {"strong": {"model": "mock"}, "fast": {"model": "mock"}},
        "skills": {"dir": str(ROOT / "skills"), "auto_select": True},
        "adapters": {"codex": {
            "command_template": f"{PY.as_posix()} {stub.as_posix()} --prompt-file {{prompt_file}}",
            "install_hint": "stub",
            "capabilities": {},
        }},
        "git": {"remote": "origin", "main_branch": "main", "auto_push": True,
                "open_pr": True, "base_branch": "main"},
        "sandbox": {"enabled": True, "worktree_root": ".orch-worktrees",
                    "branch_prefix": "feat", "base_branch": "main"},
        "policy": {"default": "auto",
                   "require_approval": ["delete_file", "modify_public_signature"],
                   "forbidden": ["force_push", "push_main_directly"],
                   "record_levels": ["L2"]},
        "runtime": {"max_llm_calls": 40, "max_tokens": 200000, "max_usd": 5},
        "fingerprint": {"mode": "file_bytes", "algorithm": "sha256"},
        "verify": {"keep_head_lines": 50, "keep_tail_lines": 50},
    }
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")

    # 预建 task 目录（task_id 用 make_task_id 预测，先放 acceptance.md）
    sys.path.insert(0, str(ROOT))
    from core import fingerprint as fp_mod  # noqa: E402
    requirement = "新建一个 hello.txt 文件"
    task_id = fp_mod.make_task_id(requirement)
    task_dir = ROOT / "specs" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "acceptance.md").write_text(
        "# 验收\n```sh\ntype hello.txt   # timeout=30 检查文件存在\n```\n", encoding="utf-8")

    proc = run([str(PY), str(ROOT / "cli.py"), "--config", str(cfg_path),
                "run", task_id,
                "--requirement", requirement, "--agent", "codex",
                "--mock-llm", "--yes"],
               cwd=repo, input="")
    print("---- cli 输出（尾 30 行）----")
    print("\n".join((proc.stdout or "").splitlines()[-30:]))
    if proc.stderr:
        print("---- stderr（尾 10 行）----")
        print("\n".join(proc.stderr.splitlines()[-10:]))

    def check(name, cond, detail=""):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail and not cond else ""))
        ok = ok and cond

    check("E2E-1 全自动 rc=0（explore→push 无打断）", proc.returncode == 0,
          f"rc={proc.returncode}")

    ls = run(["git", "-C", str(bare), "ls-tree", "-r", "--name-only",
              f"feat/{task_id}"], tmp)
    tree = ls.stdout
    check("E2E-2 远端 feat 分支含 agent 产出", "hello.txt" in tree, tree)
    check("E2E-3(R2) 远端 feat 分支无 .orch/", ".orch" not in tree, tree)

    cs = task_dir / "logs" / "change-summary.md"
    cs_text = cs.read_text(encoding="utf-8") if cs.exists() else ""
    check("E2E-4(R2) change-summary 无 .orch/", ".orch" not in cs_text, cs_text)

    st_json = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    doc = fp_mod.doc_id(task_dir, cfg)
    check("E2E-5(R1) state.doc_id 非空且与 fingerprint.doc_id 一致",
          st_json.get("doc_id", "").startswith("sha256:") and st_json["doc_id"] == doc,
          f"state={st_json.get('doc_id')} calc={doc}")
    check("E2E-6(R1) state.fingerprint_mode=file_bytes",
          st_json.get("fingerprint_mode") == "file_bytes", st_json.get("fingerprint_mode"))
    check("E2E-7 push 阶段 done", "push" in st_json.get("stages_done", []),
          str(st_json.get("stages_done")))

    # 清理本次 e2e 产物
    import shutil
    shutil.rmtree(task_dir, ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)

    # 审计 R13 收尾（根因：sandbox.create 的 mkdir(parents=True) 会建 worktree 根，
    # 而 destroy 只清 task 子目录——根不在清理契约内）。验收：
    # ① 编排器根级 .orch-worktrees：空根顺手删除（上次运行的遗留自愈）；
    #    非空 = 有任务残留 → FAIL 暴露，而非静默手删。
    # ② specs/ 退出后仅剩白名单目录（任务目录已自清，无新增残留）。
    wt_root = ROOT / ".orch-worktrees"
    leftover = [p.name for p in wt_root.iterdir()] if wt_root.exists() else []
    if leftover:
        check("E2E-8(R13) 根级 .orch-worktrees 无任务残留", False, str(leftover))
    else:
        if wt_root.exists():
            wt_root.rmdir()   # 空根属已知根因产物，自愈删除
        check("E2E-8(R13) 根级 .orch-worktrees 不存在或已清空", True)
    extra_specs = [d.name for d in (ROOT / "specs").iterdir()
                   if d.is_dir() and d.name not in ("_template", "demo", "audit")]
    check("E2E-9(R13) specs/ 仅剩白名单 _template/demo/audit", not extra_specs,
          str(extra_specs))

    print("\n==== P0 e2e 回归：" + ("全部通过 ✅" if ok else "存在失败 ❌") + " ====")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
