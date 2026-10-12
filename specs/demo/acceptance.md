# 验收标准 (acceptance.md) — specs/demo

> 每条都是**可执行命令**，在隔离工作区（worktree）内逐条执行；退出码 0 视为通过。
> 行尾注释支持选项：`# timeout=秒数 optional|required 任意备注`。
> 任一 **required** 命令失败 → 进入 heal 自愈回灌；`optional` 失败只记录不阻断。

## 检查项（可执行命令）
```sh
python -c "print('demo ok')"      # timeout=30 必需1：Python 环境在隔离区内可用
python -m compileall -q .         # timeout=120 必需2：隔离区 Python 源码语法自检
python -c "raise SystemExit(1)"   # timeout=10 optional 演示：optional 失败不阻断验收
```
