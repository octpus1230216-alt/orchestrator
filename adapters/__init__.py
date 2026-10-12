"""orchestrator.adapters — 可插拔 coding agent 适配器集合。

每家 agent 一个薄层文件（类名 + build 工厂），命令模板与 capabilities 配在
config.yaml 的 adapters 段；基类 AgentAdapter 负责 {prompt_file}/{prompt_inline}
变量替换、subprocess 执行与 git diff 写操作推断。
新增一家：加本目录一个文件 + config.yaml adapters 下一条。
"""
