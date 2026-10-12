# specs/demo — 最小闭环示例任务

一条命令即可体验全自动流水线（explore→confirm→compile→dispatch→verify+heal→push）：

```powershell
# 离线试跑（无 LLM key / 未装 agent 也能走完 explore→compile，dispatch 会给出安装提示）
python cli.py run demo --requirement "在仓库根生成 hello.txt，内容为 hello orch" --yes --mock-llm --dry-run

# 去掉 --dry-run 即继续 dispatch→verify→push；
# 装好任一 coding agent（如 codex）后无需 --mock-llm 也可全自动：
python cli.py run demo --requirement "在仓库根生成 hello.txt，内容为 hello orch" --yes --agent codex
```

- `acceptance.md`：三条可执行命令（两条 required + 一条 optional 演示），verify 阶段逐条执行
- `--dry-run` 只到 compile；之后用 `python cli.py run demo --resume` 从断点继续
- 其余产物（context.md / questions.json / answers.json / prompt.md / state.json / logs/）
  由流水线自动生成，不入库
