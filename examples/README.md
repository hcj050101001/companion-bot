# 示例索引

- `dummy_tool_plugin.py`：最小只读工具插件示例——演示新增一个工具只需一个插件类加一段注册代码，门控由宿主网关统一执行。运行命令（仅验证可导入；实际加载方式见文件头 docstring 的注册片段）：`PYTHONPATH=. python examples/dummy_tool_plugin.py`
- `fake_adapter_demo.py`：最小内存版 PlatformAdapter 端到端演示——把一条模拟的 OneBot 风格私聊事件解析为统一 InboundEvent，再经适配器发送回复并打印成功回执，全程离线、零网络。运行命令：`PYTHONPATH=. python examples/fake_adapter_demo.py`
