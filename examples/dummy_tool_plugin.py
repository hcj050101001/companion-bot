"""最小工具插件示例（方案 §5.5 / §8 P4）。

演示新增一个只读工具的全部成本：一个插件类 + 一段注册代码。
不需要改 brain、pipeline 或 ingress 的任何一行——宿主只认协议：

    registry = ReadOnlyRegistry(enabled=True, policies={
        "dice_roll": {"enabled": True},
    })
    plugins = PluginRegistry(registry)
    plugins.register_plugin(DiceRollPlugin())
    await plugins.load_all()

门控（双开关、闭合 schema 校验、超时、截断、全局信号量）由网关
统一执行；插件拿不到 identity/sender/gateway，不能发消息或读写
记忆。结果政策声明为 synthesize：结果回喂模型合成。
"""

import random

from pydantic import Field

from companion.tools.plugins import BaseToolPlugin, PluginConfig, PluginMeta
from companion.tools.read_only import ReadOnlyTool

SCHEMA = {
    "type": "object",
    "properties": {
        "sides": {"type": "integer", "minimum": 2, "maximum": 120},
    },
    "required": ["sides"],
    "additionalProperties": False,
}


class DiceRollConfig(PluginConfig):
    """插件独立配置命名空间示例（与全局配置互不污染）。"""

    loaded_message: str = Field(default="dice plugin loaded")


class DiceRollPlugin(BaseToolPlugin):
    meta = PluginMeta(
        name="dice_roll",
        version="0.1.0",
        description="Roll a dice with the given number of sides (example plugin).",
        max_output_chars=200,
    )
    config_schema = DiceRollConfig

    def __init__(self, config: DiceRollConfig | None = None, *, rng=None):
        self.config = config or DiceRollConfig()
        self._rng = rng or random.Random()
        self.loaded = False
        self.cache: dict = {}

    def register(self, ctx) -> None:
        async def action(sides):
            return f"rolled {self._rng.randint(1, sides)} on a {sides}-sided dice"

        ctx.register_tool(
            ReadOnlyTool(
                "dice_roll",
                "掷一个指定面数的骰子（示例插件，无外部访问）。",
                SCHEMA,
                action,
            ),
            policy="synthesize",
        )

    async def on_load(self) -> None:
        self.loaded = True

    async def on_unload(self) -> None:
        # dispose 语义：清掉自己的一切缓存与状态。
        self.loaded = False
        self.cache.clear()
