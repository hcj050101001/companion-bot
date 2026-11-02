# 工具权限

| 工具 | 权限/用途 | 默认开关 | 人工确认 | 限流与 dry-run |
|---|---|---:|---:|---|
| 私聊发送 `send_private_msg` | OneBot 给已准入的私聊用户发送回复 | 由安全处理链路调用 | 否；危机回复按固定流程发送 | 支持 `dry_run`；调用方传 `rate_limit`，默认 6 |
| 群聊发送 `send_group_msg` | 群消息接口预留 | 关闭 | 否 | 接口接收 `dry_run` 和 `rate_limit`，当前返回 disabled |
| reaction `send_reaction` | 表情/反应接口预留 | 关闭 | 否 | 接口接收 `dry_run` 和 `rate_limit`，当前返回 disabled |
| QQ 空间 `qzone_post` | 发布空间内容 | `config/app.yaml` 的 `tools.qzone.enabled: false` | 否 | 接口接收 `dry_run` 和 `rate_limit`；未启用返回 disabled |
| 好友管理 `manage_friend` | 好友添加/删除等管理操作 | `config/app.yaml` 的 `tools.friend_management.enabled: false` | 是；未确认返回 `pending`，不会执行 | 接口接收 `dry_run` 和 `rate_limit`；当前不执行变更 |

配置项缺失按关闭处理。`ToolGateway` 对调用统一应用启用检查和限流，默认采用 dry-run；生产动作必须由调用方显式设置 `dry_run=False`。好友管理在任何执行动作前都必须经过人工确认。
