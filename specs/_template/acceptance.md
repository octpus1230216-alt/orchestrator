# 验收标准 (acceptance.md)

> 由 core/compile.py 生成，verify 阶段逐条执行；**每条必须是可执行命令**。
> 命令退出码 0 视为通过，非 0 转 heal 自愈。

## 检查项（可执行命令）
```sh
python -m compileall -q src      # 编译/导入通过
python -m pytest -q              # 单元测试通过
```

（待按具体需求细化为可执行命令）
