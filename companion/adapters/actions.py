"""分组类型化门面（方案 §3.1，N1 落地；N2 扩消息形态全量）。

业务/服务层只调本模块的门面方法，不裸调 action 名：每个方法把类型化
参数组装为 params 后经 ActionGateway.invoke 出站，门控（ActionSpec
注册表、principal 校验、干跑/限流/额度、L3 硬禁）全部由 Gateway 独占。
可选参数为 None 时不进 params；多余关键字经 ``**extra`` 合并透传。

消息发送门面（MessagingActions 的全部 message 类方法）是例外中的
例外（§3.1 1b）：消息发送须持 DeliveryService 签发的投递句柄
（delivery_token = SendQuota 预留 token），无句柄在门面层直接拒绝、
不调 Gateway；生产发送仍走 DeliveryService → HttpOneBotClient 路径。
send_group 自 N2 起接线（两级额度已落地）：句柄 + Gateway policy
（send_group_msg 默认关）+ DeliveryService 群开关三层门控，运行时
默认硬关、无生产启用路径。

补审第二段 P3：文件域的 message 类门面（FileActions 的
send_online_file / send_online_folder / send_flash_msg）同归
§3.1 1b 纪律——此前三者无句柄可直发任意目标、绕过 DeliveryService
的媒体门与额度记账，与 MessagingActions 双标；现一律须持句柄，
identity 按目标（user / g:group）派生、句柄以 quota_token 透传
网关。登记 kind=message 与网关的额度/内容复核口径因此名实相符，
无须改 kind 规避。
"""

import logging

from companion.adapters.gateway import PRINCIPAL_SYSTEM, ActionResult, _audit_token

logger = logging.getLogger("companion.actions")


def _params(named: dict, extra: dict) -> dict:
    """组装 params：命名参数中为 None 的不进；extra 原样合并覆盖。"""
    merged = {key: value for key, value in named.items() if value is not None}
    merged.update(extra)
    return merged


class _Facade:
    """门面基类：持有 Gateway 与执行主体，invoke 时原样打戳。"""

    def __init__(self, gateway, principal):
        self._gateway = gateway
        self._principal = principal

    async def _invoke(self, action: str, params: dict, **gate) -> ActionResult:
        # gate 透传网关门控参数（N4：authorization / confirmation_token
        # 供 L2 方法显式传入口径；不传时与 N1/N2 行为一致）。
        return await self._gateway.invoke(
            action, params, principal=self._principal, **gate
        )


class MessagingActions(_Facade):
    """A 域消息门面（L0 查询部分 + 受句柄约束的发送入口）。"""

    async def get_msg(self, message_id, **extra):
        return await self._invoke("get_msg", _params({"message_id": message_id}, extra))

    async def get_forward_msg(self, id, **extra):
        return await self._invoke("get_forward_msg", _params({"id": id}, extra))

    async def fetch_emoji_like(self, message_id, **extra):
        return await self._invoke(
            "fetch_emoji_like", _params({"message_id": message_id}, extra)
        )

    async def get_ai_characters(self, group_id=None, **extra):
        return await self._invoke(
            "get_ai_characters", _params({"group_id": group_id}, extra)
        )

    async def translate_en2zh(self, text, **extra):
        return await self._invoke("translate_en2zh", _params({"text": text}, extra))

    # N6 终审：.get_word_slices 门面已下线——bundle 抽取证实该名在
    # 注册表对象中虽为未注释条目，但全 bundle 无任何动作类绑定它
    # （键名仅出现 1 次），4.18.41 无 handler；登记表已改判
    # excluded_internal（docs/napcat-integration-closeout.md）。

    async def send_private(self, user_id, message, *, delivery_token=None, **extra):
        """私聊发送门面：无 DeliveryService 句柄直接拒绝（§3.1 1b）。

        identity 由 user_id 派生（额度 token 与用户绑定校验靠它，
        Review 修复 7），priority 固定 normal 透传。
        """
        if delivery_token is None:
            return ActionResult(outcome="rejected", gate="delivery_handle_required")
        params = _params({"user_id": user_id, "message": message}, extra)
        return await self._gateway.invoke(
            "send_private_msg",
            params,
            principal=self._principal,
            identity=str(user_id),
            priority="normal",
            quota_token=delivery_token,
        )

    async def send_group(self, group_id, message, *, delivery_token=None, **extra):
        """群发送门面（N2 接线）：无句柄直接拒绝；有句柄经 Gateway
        派发（identity=g:{group_id}，token 由 Gateway 对群子桶预留
        做单次消费校验）。运行时默认硬关不在门面层：policy
        send_group_msg 默认 disabled、DeliveryService 群开关默认
        关、无生产调用方——三层默认关叠加，门面只负责契约。"""
        if delivery_token is None:
            return ActionResult(outcome="rejected", gate="delivery_handle_required")
        params = _params({"group_id": group_id, "message": message}, extra)
        return await self._gateway.invoke(
            "send_group_msg",
            params,
            principal=self._principal,
            identity=f"g:{group_id}",
            priority="normal",
            quota_token=delivery_token,
        )

    async def send_msg(self, message, *, user_id=None, group_id=None,
                       delivery_token=None, **extra):
        """统一发送门面：按目标二选一分发到 send_private_msg /
        send_group_msg（登记表口径）；目标缺失或双给在门面层拒。"""
        if delivery_token is None:
            return ActionResult(outcome="rejected", gate="delivery_handle_required")
        if (user_id is None) == (group_id is None):
            return ActionResult(outcome="rejected", gate="invalid_params")
        if user_id is not None:
            params = _params({"user_id": user_id, "message": message}, extra)
            return await self._gateway.invoke(
                "send_msg",
                params,
                principal=self._principal,
                identity=str(user_id),
                priority="normal",
                quota_token=delivery_token,
            )
        params = _params({"group_id": group_id, "message": message}, extra)
        return await self._gateway.invoke(
            "send_msg",
            params,
            principal=self._principal,
            identity=f"g:{group_id}",
            priority="normal",
            quota_token=delivery_token,
        )

    async def send_private_forward(self, user_id, messages, *, delivery_token=None, **extra):
        """合并转发发送（私聊）：messages 为 node 段数组报文。"""
        if delivery_token is None:
            return ActionResult(outcome="rejected", gate="delivery_handle_required")
        params = _params({"user_id": user_id, "messages": messages}, extra)
        return await self._gateway.invoke(
            "send_private_forward_msg",
            params,
            principal=self._principal,
            identity=str(user_id),
            priority="normal",
            quota_token=delivery_token,
        )

    async def send_group_forward(self, group_id, messages, *, delivery_token=None, **extra):
        """合并转发发送（群）：messages 为 node 段数组报文。"""
        if delivery_token is None:
            return ActionResult(outcome="rejected", gate="delivery_handle_required")
        params = _params({"group_id": group_id, "messages": messages}, extra)
        return await self._gateway.invoke(
            "send_group_forward_msg",
            params,
            principal=self._principal,
            identity=f"g:{group_id}",
            priority="normal",
            quota_token=delivery_token,
        )

    async def send_forward(self, messages, *, user_id=None, group_id=None,
                           delivery_token=None, **extra):
        """合并转发通用形：目标二选一，参数与 send_*_forward 同构。"""
        if delivery_token is None:
            return ActionResult(outcome="rejected", gate="delivery_handle_required")
        if (user_id is None) == (group_id is None):
            return ActionResult(outcome="rejected", gate="invalid_params")
        params = _params(
            {"user_id": user_id, "group_id": group_id, "messages": messages}, extra
        )
        identity = str(user_id) if user_id is not None else f"g:{group_id}"
        return await self._gateway.invoke(
            "send_forward_msg",
            params,
            principal=self._principal,
            identity=identity,
            priority="normal",
            quota_token=delivery_token,
        )

    async def forward_friend_single(self, message_id, user_id, *, delivery_token=None, **extra):
        """单条转发到好友（消息类，须句柄）。"""
        if delivery_token is None:
            return ActionResult(outcome="rejected", gate="delivery_handle_required")
        params = _params({"message_id": message_id, "user_id": user_id}, extra)
        return await self._gateway.invoke(
            "forward_friend_single_msg",
            params,
            principal=self._principal,
            identity=str(user_id),
            priority="normal",
            quota_token=delivery_token,
        )

    async def forward_group_single(self, message_id, group_id, *, delivery_token=None, **extra):
        """单条转发到群（消息类，须句柄）。"""
        if delivery_token is None:
            return ActionResult(outcome="rejected", gate="delivery_handle_required")
        params = _params({"message_id": message_id, "group_id": group_id}, extra)
        return await self._gateway.invoke(
            "forward_group_single_msg",
            params,
            principal=self._principal,
            identity=f"g:{group_id}",
            priority="normal",
            quota_token=delivery_token,
        )

    async def send_group_ai_record(
        self, group_id, character, text, *, delivery_token=None, **extra
    ):
        """AI 语音（群）：与 get_ai_characters 配套，消息类须句柄。"""
        if delivery_token is None:
            return ActionResult(outcome="rejected", gate="delivery_handle_required")
        params = _params(
            {"group_id": group_id, "character": character, "text": text}, extra
        )
        return await self._gateway.invoke(
            "send_group_ai_record",
            params,
            principal=self._principal,
            identity=f"g:{group_id}",
            priority="normal",
            quota_token=delivery_token,
        )

    async def send_ark_share(self, *, delivery_token=None, **extra):
        """Ark 卡片分享（私聊向，G 域）：参数透传，消息类须句柄；
        identity 由参数中的 user_id/group_id 派生。"""
        if delivery_token is None:
            return ActionResult(outcome="rejected", gate="delivery_handle_required")
        identity = ""
        if extra.get("user_id") is not None:
            identity = str(extra["user_id"])
        elif extra.get("group_id") is not None:
            identity = f"g:{extra['group_id']}"
        return await self._gateway.invoke(
            "send_ark_share",
            _params({}, extra),
            principal=self._principal,
            identity=identity,
            priority="normal",
            quota_token=delivery_token,
        )

    async def send_group_ark_share(self, group_id, *, delivery_token=None, **extra):
        """Ark 卡片分享（群向，G 域）：消息类须句柄。"""
        if delivery_token is None:
            return ActionResult(outcome="rejected", gate="delivery_handle_required")
        params = _params({"group_id": group_id}, extra)
        return await self._gateway.invoke(
            "send_group_ark_share",
            params,
            principal=self._principal,
            identity=f"g:{group_id}",
            priority="normal",
            quota_token=delivery_token,
        )

    async def set_msg_emoji_like(self, message_id, emoji_id, **extra):
        """消息表情回应（mutation，无句柄要求）。"""
        return await self._invoke(
            "set_msg_emoji_like",
            _params({"message_id": message_id, "emoji_id": emoji_id}, extra),
        )

    async def mark_msg_as_read(self, message_id, **extra):
        return await self._invoke(
            "mark_msg_as_read", _params({"message_id": message_id}, extra)
        )

    async def mark_private_msg_as_read(self, user_id, message_id=None, **extra):
        return await self._invoke(
            "mark_private_msg_as_read",
            _params({"user_id": user_id, "message_id": message_id}, extra),
        )

    async def mark_group_msg_as_read(self, group_id, message_id=None, **extra):
        return await self._invoke(
            "mark_group_msg_as_read",
            _params({"group_id": group_id, "message_id": message_id}, extra),
        )

    async def mark_all_as_read(self, **extra):
        return await self._invoke("_mark_all_as_read", _params({}, extra))

    async def set_input_status(self, user_id, event_type=None, **extra):
        """向对方显示「输入中」等状态（mutation）。"""
        return await self._invoke(
            "set_input_status",
            _params({"user_id": user_id, "event_type": event_type}, extra),
        )

    async def click_inline_keyboard_button(self, **extra):
        """内联键盘按钮点击（mutation；schema 待验，参数透传）。"""
        return await self._invoke(
            "click_inline_keyboard_button", _params({}, extra)
        )

    async def delete_msg(
        self, message_id, *, authorization=None, confirmation_token=None, **extra
    ):
        """撤回消息（L2，N4）：授权链全在网关，门面只组装参数。"""
        return await self._invoke(
            "delete_msg",
            _params({"message_id": message_id}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def handle_quick_operation(
        self, context, operation, *, authorization=None, confirmation_token=None, **extra
    ):
        """事件快捷操作（L2，N4）：批请求/快捷回复，属关系处置。"""
        return await self._invoke(
            ".handle_quick_operation",
            _params({"context": context, "operation": operation}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )


class GroupActions(_Facade):
    """B 域群门面（L0 查询 + N4 的 L2 群管理与 L1 群互动）。"""

    async def get_group_info(self, group_id, **extra):
        return await self._invoke("get_group_info", _params({"group_id": group_id}, extra))

    async def get_group_info_ex(self, group_id, **extra):
        return await self._invoke(
            "get_group_info_ex", _params({"group_id": group_id}, extra)
        )

    async def get_group_list(self, **extra):
        return await self._invoke("get_group_list", _params({}, extra))

    async def get_group_member_info(self, group_id, user_id, **extra):
        return await self._invoke(
            "get_group_member_info",
            _params({"group_id": group_id, "user_id": user_id}, extra),
        )

    async def get_group_member_list(self, group_id, **extra):
        return await self._invoke(
            "get_group_member_list", _params({"group_id": group_id}, extra)
        )

    async def get_group_honor_info(self, group_id, **extra):
        return await self._invoke(
            "get_group_honor_info", _params({"group_id": group_id}, extra)
        )

    async def get_group_shut_list(self, group_id, **extra):
        return await self._invoke(
            "get_group_shut_list", _params({"group_id": group_id}, extra)
        )

    async def get_group_signed_list(self, group_id, **extra):
        return await self._invoke(
            "get_group_signed_list", _params({"group_id": group_id}, extra)
        )

    async def get_group_at_all_remain(self, group_id, **extra):
        return await self._invoke(
            "get_group_at_all_remain", _params({"group_id": group_id}, extra)
        )

    async def get_group_system_msg(self, **extra):
        return await self._invoke("get_group_system_msg", _params({}, extra))

    async def get_group_ignored_notifies(self, **extra):
        return await self._invoke("get_group_ignored_notifies", _params({}, extra))

    async def get_essence_msg_list(self, group_id, **extra):
        return await self._invoke(
            "get_essence_msg_list", _params({"group_id": group_id}, extra)
        )

    async def get_group_notice(self, group_id, **extra):
        return await self._invoke(
            "_get_group_notice", _params({"group_id": group_id}, extra)
        )

    async def get_group_detail_info(self, group_id, **extra):
        return await self._invoke(
            "get_group_detail_info", _params({"group_id": group_id}, extra)
        )

    async def get_group_share_link(self, group_id, **extra):
        return await self._invoke(
            "get_group_share_link", _params({"group_id": group_id}, extra)
        )

    # -- N4：L2 群管理 + L1 群互动（授权/角色/dry_run 门控全在网关，
    #    门面只组装参数并透传 authorization / confirmation_token） --

    async def set_group_kick(
        self, group_id, user_id, *, authorization=None, confirmation_token=None, **extra
    ):
        """踢人（L2）。"""
        return await self._invoke(
            "set_group_kick",
            _params({"group_id": group_id, "user_id": user_id}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def set_group_kick_members(
        self, group_id, user_id, *, authorization=None, confirmation_token=None, **extra
    ):
        """批量踢人（L2；schema 待验，user_id 可为列表，参数透传）。"""
        return await self._invoke(
            "set_group_kick_members",
            _params({"group_id": group_id, "user_id": user_id}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def set_group_ban(
        self, group_id, user_id, duration=None, *,
        authorization=None, confirmation_token=None, **extra,
    ):
        """禁言（L2）；duration 单位秒，0 为解除。"""
        return await self._invoke(
            "set_group_ban",
            _params({"group_id": group_id, "user_id": user_id, "duration": duration}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def set_group_whole_ban(
        self, group_id, enable=None, *,
        authorization=None, confirmation_token=None, **extra,
    ):
        """全员禁言（L2）。"""
        return await self._invoke(
            "set_group_whole_ban",
            _params({"group_id": group_id, "enable": enable}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def set_group_admin(
        self, group_id, user_id, enable=None, *,
        authorization=None, confirmation_token=None, **extra,
    ):
        """设/撤管理员（L2；网关角色门要求机器人为群主）。"""
        return await self._invoke(
            "set_group_admin",
            _params({"group_id": group_id, "user_id": user_id, "enable": enable}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def set_group_card(
        self, group_id, user_id, card=None, *,
        authorization=None, confirmation_token=None, **extra,
    ):
        """改群名片（L2）。"""
        return await self._invoke(
            "set_group_card",
            _params({"group_id": group_id, "user_id": user_id, "card": card}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def set_group_name(
        self, group_id, group_name, *,
        authorization=None, confirmation_token=None, **extra,
    ):
        """改群名（L2）。"""
        return await self._invoke(
            "set_group_name",
            _params({"group_id": group_id, "group_name": group_name}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def set_group_special_title(
        self, group_id, user_id, special_title=None, duration=None, *,
        authorization=None, confirmation_token=None, **extra,
    ):
        """专属头衔（L2）。"""
        return await self._invoke(
            "set_group_special_title",
            _params(
                {
                    "group_id": group_id,
                    "user_id": user_id,
                    "special_title": special_title,
                    "duration": duration,
                },
                extra,
            ),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def set_group_leave(
        self, group_id, is_dismiss=None, *,
        authorization=None, confirmation_token=None, **extra,
    ):
        """退群/解散（L2 不可逆档：网关另须绑定确认令牌）。"""
        return await self._invoke(
            "set_group_leave",
            _params({"group_id": group_id, "is_dismiss": is_dismiss}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def set_group_portrait(
        self, group_id, file, *, authorization=None, confirmation_token=None, **extra
    ):
        """群头像（L2）。"""
        return await self._invoke(
            "set_group_portrait",
            _params({"group_id": group_id, "file": file}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def set_group_add_request(
        self, flag, sub_type, approve=None, reason=None, *,
        authorization=None, confirmation_token=None, **extra,
    ):
        """批入群申请/邀请（L2；与 request 事件处置状态机配套）。"""
        return await self._invoke(
            "set_group_add_request",
            _params(
                {"flag": flag, "sub_type": sub_type, "approve": approve, "reason": reason},
                extra,
            ),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def set_essence_msg(
        self, message_id, *, authorization=None, confirmation_token=None, **extra
    ):
        """设精华（L2）。"""
        return await self._invoke(
            "set_essence_msg",
            _params({"message_id": message_id}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def delete_essence_msg(
        self, message_id, *, authorization=None, confirmation_token=None, **extra
    ):
        """撤精华（L2）。"""
        return await self._invoke(
            "delete_essence_msg",
            _params({"message_id": message_id}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def send_group_notice(
        self, group_id, content, *, authorization=None, confirmation_token=None, **extra
    ):
        """发群公告（L2，动作名 _send_group_notice）。"""
        return await self._invoke(
            "_send_group_notice",
            _params({"group_id": group_id, "content": content}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def del_group_notice(
        self, group_id, notice_id, *,
        authorization=None, confirmation_token=None, **extra,
    ):
        """删群公告（L2，动作名 _del_group_notice）。"""
        return await self._invoke(
            "_del_group_notice",
            _params({"group_id": group_id, "notice_id": notice_id}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def send_group_sign(self, group_id, **extra):
        """群签到（L1 mutation；set_group_sign 为其别名）。"""
        return await self._invoke(
            "send_group_sign", _params({"group_id": group_id}, extra)
        )

    async def set_group_todo(self, group_id, **extra):
        """群待办设置（L1；schema 待验，参数透传）。"""
        return await self._invoke(
            "set_group_todo", _params({"group_id": group_id}, extra)
        )

    async def complete_group_todo(self, group_id, **extra):
        """群待办完成（L1；schema 待验，参数透传）。"""
        return await self._invoke(
            "complete_group_todo", _params({"group_id": group_id}, extra)
        )

    async def cancel_group_todo(self, group_id, **extra):
        """群待办取消（L1；schema 待验，参数透传）。"""
        return await self._invoke(
            "cancel_group_todo", _params({"group_id": group_id}, extra)
        )

    async def set_group_add_option(
        self, group_id, *, authorization=None, confirmation_token=None, **extra
    ):
        """群加群设置（L2；schema 待验，参数透传）。"""
        return await self._invoke(
            "set_group_add_option",
            _params({"group_id": group_id}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def set_group_member_invite_policy(
        self, group_id, *, authorization=None, confirmation_token=None, **extra
    ):
        """群成员邀请策略（L2；schema 待验，参数透传）。"""
        return await self._invoke(
            "set_group_member_invite_policy",
            _params({"group_id": group_id}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def set_group_member_permissions(
        self, group_id, *, authorization=None, confirmation_token=None, **extra
    ):
        """群成员权限（L2；schema 待验，参数透传）。"""
        return await self._invoke(
            "set_group_member_permissions",
            _params({"group_id": group_id}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def set_group_new_member_history_visibility(
        self, group_id, *, authorization=None, confirmation_token=None, **extra
    ):
        """新成员历史可见性（L2；schema 待验，参数透传）。"""
        return await self._invoke(
            "set_group_new_member_history_visibility",
            _params({"group_id": group_id}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def set_group_robot_add_option(
        self, group_id, *, authorization=None, confirmation_token=None, **extra
    ):
        """机器人加群设置（L2；schema 待验，参数透传）。"""
        return await self._invoke(
            "set_group_robot_add_option",
            _params({"group_id": group_id}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def set_group_search(
        self, group_id, *, authorization=None, confirmation_token=None, **extra
    ):
        """群搜索设置（L2；schema 待验，参数透传）。"""
        return await self._invoke(
            "set_group_search",
            _params({"group_id": group_id}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def set_group_remark(
        self, group_id, *, authorization=None, confirmation_token=None, **extra
    ):
        """群备注（L2；schema 待验，参数透传）。"""
        return await self._invoke(
            "set_group_remark",
            _params({"group_id": group_id}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )


class SocialActions(_Facade):
    """C 域好友与关系门面（L0 查询 + N4 的 L2 关系处置与 qzone 发送）。"""

    async def get_friend_list(self, **extra):
        return await self._invoke("get_friend_list", _params({}, extra))

    async def get_unidirectional_friend_list(self, **extra):
        return await self._invoke("get_unidirectional_friend_list", _params({}, extra))

    async def get_stranger_info(self, user_id, **extra):
        return await self._invoke(
            "get_stranger_info", _params({"user_id": user_id}, extra)
        )

    async def get_recent_contact(self, **extra):
        return await self._invoke("get_recent_contact", _params({}, extra))

    async def get_profile_like(self, **extra):
        return await self._invoke("get_profile_like", _params({}, extra))

    async def get_user_status(self, user_id, **extra):
        return await self._invoke(
            "nc_get_user_status", _params({"user_id": user_id}, extra)
        )

    async def get_robot_uin_range(self, **extra):
        return await self._invoke("get_robot_uin_range", _params({}, extra))

    async def fetch_custom_face(self, **extra):
        return await self._invoke("fetch_custom_face", _params({}, extra))

    async def get_collection_list(self, **extra):
        return await self._invoke("get_collection_list", _params({}, extra))

    async def get_friends_with_category(self, **extra):
        return await self._invoke("get_friends_with_category", _params({}, extra))

    async def get_doubt_friends_add_request(self, **extra):
        return await self._invoke("get_doubt_friends_add_request", _params({}, extra))

    async def fetch_custom_face_detail(self, **extra):
        return await self._invoke("fetch_custom_face_detail", _params({}, extra))

    async def send_like(self, user_id, times=None, **extra):
        """点赞（mutation；官方提示注意频率，频控在 Gateway 限流）。"""
        return await self._invoke(
            "send_like", _params({"user_id": user_id, "times": times}, extra)
        )

    async def friend_poke(self, user_id, **extra):
        return await self._invoke("friend_poke", _params({"user_id": user_id}, extra))

    async def group_poke(self, group_id, user_id, **extra):
        return await self._invoke(
            "group_poke", _params({"group_id": group_id, "user_id": user_id}, extra)
        )

    async def poke(self, *, user_id=None, group_id=None, **extra):
        """戳一戳统一门面（登记表三形态合一）：双目标 → group_poke，
        仅 user_id → friend_poke，仅 send_poke 兜底（双缺省时以
        user_id 为必填），其余组合在门面层拒。"""
        if group_id is not None and user_id is not None:
            return await self.group_poke(group_id, user_id, **extra)
        if user_id is not None:
            return await self.friend_poke(user_id, **extra)
        if group_id is not None:
            return await self._invoke(
                "send_poke", _params({"group_id": group_id}, extra)
            )
        return ActionResult(outcome="rejected", gate="invalid_params")

    async def create_collection(self, title=None, **extra):
        """创建收藏（mutation）。"""
        return await self._invoke(
            "create_collection", _params({"title": title}, extra)
        )

    async def add_custom_face(self, **extra):
        """添加自定义表情（mutation；schema 待验，参数透传）。"""
        return await self._invoke("add_custom_face", _params({}, extra))

    async def set_custom_face_desc(self, **extra):
        """设置自定义表情描述（mutation；schema 待验，参数透传）。"""
        return await self._invoke("set_custom_face_desc", _params({}, extra))

    # -- N4：L2 关系处置 + qzone 发送（授权链全在网关） --

    async def set_friend_add_request(
        self, flag, approve=None, remark=None, *,
        authorization=None, confirmation_token=None, **extra,
    ):
        """批好友申请（L2；canonical 路径另有授权链与确认门）。"""
        return await self._invoke(
            "set_friend_add_request",
            _params({"flag": flag, "approve": approve, "remark": remark}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def delete_friend(
        self, user_id, *, authorization=None, confirmation_token=None, **extra
    ):
        """删好友（L2 不可逆档：网关另须绑定确认令牌）。"""
        return await self._invoke(
            "delete_friend",
            _params({"user_id": user_id}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def set_doubt_friends_add_request(
        self, flag, *, authorization=None, confirmation_token=None, **extra
    ):
        """可疑好友申请处置（L2；schema 待验，参数透传）。"""
        return await self._invoke(
            "set_doubt_friends_add_request",
            _params({"flag": flag}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def set_friend_remark(self, user_id, remark=None, **extra):
        """好友备注（L1 mutation；schema 待验）。"""
        return await self._invoke(
            "set_friend_remark",
            _params({"user_id": user_id, "remark": remark}, extra),
        )

    async def send_qzone_msg(self, message=None, **extra):
        """发 QQ 空间说说（L1 mutation；qzone 工具面经 policy_key=qzone 调用）。"""
        return await self._invoke(
            "send_qzone_msg", _params({"message": message}, extra)
        )

    async def delete_qzone_msg(
        self, *, authorization=None, confirmation_token=None, **extra
    ):
        """删 QQ 空间说说（L2；schema 待验，参数透传）。"""
        return await self._invoke(
            "delete_qzone_msg",
            _params({}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def delete_custom_face(
        self, *, authorization=None, confirmation_token=None, **extra
    ):
        """删自定义表情（L2；schema 待验，参数透传）。"""
        return await self._invoke(
            "delete_custom_face",
            _params({}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )


class MediaActions(_Facade):
    """D 域历史与媒体解析门面（L0）。"""

    async def get_group_msg_history(self, group_id, **extra):
        return await self._invoke(
            "get_group_msg_history", _params({"group_id": group_id}, extra)
        )

    async def get_friend_msg_history(self, user_id, **extra):
        return await self._invoke(
            "get_friend_msg_history", _params({"user_id": user_id}, extra)
        )

    async def get_image(self, file, **extra):
        return await self._invoke("get_image", _params({"file": file}, extra))

    async def get_record(self, file, out_format=None, **extra):
        return await self._invoke(
            "get_record", _params({"file": file, "out_format": out_format}, extra)
        )

    async def get_file(self, file_id, **extra):
        return await self._invoke("get_file", _params({"file_id": file_id}, extra))

    async def can_send_image(self, **extra):
        return await self._invoke("can_send_image", _params({}, extra))

    async def can_send_record(self, **extra):
        return await self._invoke("can_send_record", _params({}, extra))

    async def ocr_image(self, image, **extra):
        return await self._invoke("ocr_image", _params({"image": image}, extra))

    async def fetch_ptt_text(self, message_id=None, file=None, **extra):
        return await self._invoke(
            "fetch_ptt_text",
            _params({"message_id": message_id, "file": file}, extra),
        )

    async def get_emoji_likes(self, message_id, **extra):
        return await self._invoke(
            "get_emoji_likes", _params({"message_id": message_id}, extra)
        )

    async def get_ai_record(self, group_id, character, text, **extra):
        """AI 语音合成取数（L1 query；schema 待验，参数透传）。"""
        return await self._invoke(
            "get_ai_record",
            _params({"group_id": group_id, "character": character, "text": text}, extra),
        )


class FileActions(_Facade):
    """E/E2/F 域文件与相册门面（L0 查询 + N5 的 E/E2 写动作）。

    N5 写动作的命名参数严格按 bundle payload schema 字段表
    （docs/napcat-schema-evidence-n5.md /
    tests/fixtures/napcat/bundle_action_schemas.json）：可选字段
    None 不进 params；带 schema default 的字段（upload_file、
    file_retention 等）同样以 None 表示「用平台默认」、不进 params。
    """

    async def get_private_file_url(self, user_id, file_id, **extra):
        return await self._invoke(
            "get_private_file_url",
            _params({"user_id": user_id, "file_id": file_id}, extra),
        )

    async def get_group_file_system_info(self, group_id, **extra):
        return await self._invoke(
            "get_group_file_system_info", _params({"group_id": group_id}, extra)
        )

    async def get_group_root_files(self, group_id, **extra):
        return await self._invoke(
            "get_group_root_files", _params({"group_id": group_id}, extra)
        )

    async def get_group_files_by_folder(self, group_id, folder_id, **extra):
        return await self._invoke(
            "get_group_files_by_folder",
            _params({"group_id": group_id, "folder_id": folder_id}, extra),
        )

    async def get_group_file_url(self, group_id, file_id, **extra):
        return await self._invoke(
            "get_group_file_url",
            _params({"group_id": group_id, "file_id": file_id}, extra),
        )

    # N6 Review 修复（P2-1）：本组五名在 N1/N5 落地时 schema 待验、
    # 签名按臆测取了 file_set_id/file_id，与 bundle payloadSchema
    # 实证字段不符（实调必 400，N0 探针已证空参回「缺少必填」）；
    # 按证据定案改签名，与 get_online_file_msg 的 N5 修正同例。
    # 证据：docs/napcat-schema-evidence-n5.md、-n6.md（get_share_link）。
    async def get_flash_file_list(self, fileset_id, **extra):
        return await self._invoke(
            "get_flash_file_list", _params({"fileset_id": fileset_id}, extra)
        )

    async def get_flash_file_url(
        self, fileset_id, file_name=None, file_index=None, **extra
    ):
        return await self._invoke(
            "get_flash_file_url",
            _params(
                {
                    "fileset_id": fileset_id,
                    "file_name": file_name,
                    "file_index": file_index,
                },
                extra,
            ),
        )

    async def get_fileset_info(self, fileset_id, **extra):
        return await self._invoke(
            "get_fileset_info", _params({"fileset_id": fileset_id}, extra)
        )

    async def get_fileset_id(self, share_code, **extra):
        return await self._invoke(
            "get_fileset_id", _params({"share_code": share_code}, extra)
        )

    async def get_share_link(self, fileset_id, **extra):
        return await self._invoke(
            "get_share_link", _params({"fileset_id": fileset_id}, extra)
        )

    async def get_online_file_msg(self, user_id, **extra):
        # N5 裁决修正：N1 落地时 schema 待验、签名按臆测取 message_id；
        # bundle payloadSchema 实证唯一必填为 user_id（handler 亦只读
        # user_id），按证据定案改签名（docs/napcat-schema-evidence-n5.md）。
        return await self._invoke(
            "get_online_file_msg", _params({"user_id": user_id}, extra)
        )

    async def get_qun_album_list(self, group_id, **extra):
        return await self._invoke(
            "get_qun_album_list", _params({"group_id": group_id}, extra)
        )

    async def get_group_album_media_list(self, group_id, album_id=None, **extra):
        return await self._invoke(
            "get_group_album_media_list",
            _params({"group_id": group_id, "album_id": album_id}, extra),
        )

    # -- N5：E 域写动作（字段表以 bundle schema 证据为准） --

    async def upload_private_file(self, user_id, file, name, upload_file=None, **extra):
        """私聊文件上传（L1）。upload_file 带 schema default，不传即默认。"""
        return await self._invoke(
            "upload_private_file",
            _params(
                {"user_id": user_id, "file": file, "name": name, "upload_file": upload_file},
                extra,
            ),
        )

    async def download_file(self, url=None, base64=None, name=None, headers=None, **extra):
        """下载文件落盘（L1；四字段在 schema 中全可选，url/base64
        二选一等约束 schema 未表达，门面不臆造必填）。"""
        return await self._invoke(
            "download_file",
            _params({"url": url, "base64": base64, "name": name, "headers": headers}, extra),
        )

    async def upload_group_file(
        self, group_id, file, name, folder=None, folder_id=None, upload_file=None, **extra
    ):
        """群文件上传（L1）。"""
        return await self._invoke(
            "upload_group_file",
            _params(
                {
                    "group_id": group_id,
                    "file": file,
                    "name": name,
                    "folder": folder,
                    "folder_id": folder_id,
                    "upload_file": upload_file,
                },
                extra,
            ),
        )

    async def delete_group_file(
        self, group_id, file_id, bus_id=None, busid=None, *,
        authorization=None, confirmation_token=None, **extra,
    ):
        """删群文件（L2 不可逆档：网关另须绑定确认令牌）。"""
        return await self._invoke(
            "delete_group_file",
            _params(
                {"group_id": group_id, "file_id": file_id, "bus_id": bus_id, "busid": busid},
                extra,
            ),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def create_group_file_folder(self, group_id, folder_name=None, name=None, **extra):
        """建群文件文件夹（L1；folder_name 与 name 为可选同义字段，
        schema 不保证至少传一个，门面不臆造）。"""
        return await self._invoke(
            "create_group_file_folder",
            _params({"group_id": group_id, "folder_name": folder_name, "name": name}, extra),
        )

    async def delete_group_folder(
        self, group_id, folder_id=None, folder=None, *,
        authorization=None, confirmation_token=None, **extra,
    ):
        """删群文件夹（L2 不可逆档；folder_id/folder 为可选同义字段）。"""
        return await self._invoke(
            "delete_group_folder",
            _params({"group_id": group_id, "folder_id": folder_id, "folder": folder}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def trans_group_file(self, group_id, file_id, **extra):
        """群文件转发（L1；独立 canonical，仅 group_id+file_id 两字段）。"""
        return await self._invoke(
            "trans_group_file",
            _params({"group_id": group_id, "file_id": file_id}, extra),
        )

    async def move_group_file(
        self, group_id, file_id, current_parent_directory, target_parent_directory, **extra
    ):
        """群文件移动（L1；独立 canonical，非 trans 别名——N5 定案）。"""
        return await self._invoke(
            "move_group_file",
            _params(
                {
                    "group_id": group_id,
                    "file_id": file_id,
                    "current_parent_directory": current_parent_directory,
                    "target_parent_directory": target_parent_directory,
                },
                extra,
            ),
        )

    async def rename_group_file(
        self, group_id, file_id, current_parent_directory, new_name, **extra
    ):
        """群文件重命名（L1；独立 canonical，非 trans 别名——N5 定案）。"""
        return await self._invoke(
            "rename_group_file",
            _params(
                {
                    "group_id": group_id,
                    "file_id": file_id,
                    "current_parent_directory": current_parent_directory,
                    "new_name": new_name,
                },
                extra,
            ),
        )

    # -- N5：E2 域写动作（闪传 / 在线文件 / 流式） --

    async def create_flash_task(
        self, files, name=None, thumb_path=None, validitySeconds=None, **extra
    ):
        """创建闪传任务（L1；validitySeconds 无 schema default，
        description 所述 1209600 只是文案，不代填）。"""
        return await self._invoke(
            "create_flash_task",
            _params(
                {
                    "files": files,
                    "name": name,
                    "thumb_path": thumb_path,
                    "validitySeconds": validitySeconds,
                },
                extra,
            ),
        )

    async def send_flash_msg(
        self, fileset_id, user_id=None, group_id=None, *, delivery_token=None, **extra
    ):
        """以 flashtransfer 发送闪传（L1）。登记 kind=message：须持
        投递句柄（§3.1 1b，补审第二段 P3），无句柄门面层直接拒绝；
        目标恰一（user_id/group_id 二选一，与 MessagingActions
        .send_msg 同纪律，复审 P3）——双给/双缺门面层拒
        invalid_params：网关额度域按「群目标须 user_id 缺席」派生
        identity，门面若自选群优先派生，双给时合法的群句柄会被
        网关按用户域验票误拒，恰一约束使两层派生恒一致。"""
        if delivery_token is None:
            return ActionResult(outcome="rejected", gate="delivery_handle_required")
        if (user_id is None) == (group_id is None):
            return ActionResult(outcome="rejected", gate="invalid_params")
        if group_id is not None:
            identity = f"g:{group_id}"
        else:
            identity = str(user_id)
        params = _params(
            {"fileset_id": fileset_id, "user_id": user_id, "group_id": group_id}, extra
        )
        return await self._gateway.invoke(
            "send_flash_msg",
            params,
            principal=self._principal,
            identity=identity,
            priority="normal",
            quota_token=delivery_token,
        )

    async def download_fileset(self, fileset_id, **extra):
        """下载闪传文件集落盘（L1，审计）。"""
        return await self._invoke(
            "download_fileset", _params({"fileset_id": fileset_id}, extra)
        )

    async def send_online_file(
        self, user_id, file_path, file_name=None, *, delivery_token=None, **extra
    ):
        """发送在线文件（L1，登记 kind=message）。须持投递句柄
        （§3.1 1b，补审第二段 P3），无句柄门面层直接拒绝；
        identity 由 user_id 派生（与 MessagingActions 同口径）。"""
        if delivery_token is None:
            return ActionResult(outcome="rejected", gate="delivery_handle_required")
        params = _params(
            {"user_id": user_id, "file_path": file_path, "file_name": file_name}, extra
        )
        return await self._gateway.invoke(
            "send_online_file",
            params,
            principal=self._principal,
            identity=str(user_id),
            priority="normal",
            quota_token=delivery_token,
        )

    async def send_online_folder(
        self, user_id, folder_path, folder_name=None, *, delivery_token=None, **extra
    ):
        """发送在线文件夹（L1，登记 kind=message）。须持投递句柄
        （§3.1 1b，补审第二段 P3），口径与 send_online_file 一致。"""
        if delivery_token is None:
            return ActionResult(outcome="rejected", gate="delivery_handle_required")
        params = _params(
            {"user_id": user_id, "folder_path": folder_path, "folder_name": folder_name},
            extra,
        )
        return await self._gateway.invoke(
            "send_online_folder",
            params,
            principal=self._principal,
            identity=str(user_id),
            priority="normal",
            quota_token=delivery_token,
        )

    async def receive_online_file(self, user_id, msg_id, element_id, **extra):
        """接收在线文件（L1；示例中的 save_path 不在 schema 内，不设参数）。"""
        return await self._invoke(
            "receive_online_file",
            _params({"user_id": user_id, "msg_id": msg_id, "element_id": element_id}, extra),
        )

    async def refuse_online_file(self, user_id, msg_id, element_id, **extra):
        """拒绝在线文件（L1；element_id 为 schema 必填，示例漏列不采）。"""
        return await self._invoke(
            "refuse_online_file",
            _params({"user_id": user_id, "msg_id": msg_id, "element_id": element_id}, extra),
        )

    async def cancel_online_file(self, user_id, msg_id, **extra):
        """取消在线文件（L1）。"""
        return await self._invoke(
            "cancel_online_file",
            _params({"user_id": user_id, "msg_id": msg_id}, extra),
        )

    async def upload_file_stream(
        self, stream_id, chunk_data=None, chunk_index=None, total_chunks=None,
        file_size=None, expected_sha256=None, is_complete=None, filename=None,
        reset=None, verify_only=None, file_retention=None, **extra,
    ):
        """流式上传一块（L1；file_retention 带 schema default，不传即默认）。
        分块编排（进度/取消）由 FileTaskManager.stream_upload 驱动。"""
        return await self._invoke(
            "upload_file_stream",
            _params(
                {
                    "stream_id": stream_id,
                    "chunk_data": chunk_data,
                    "chunk_index": chunk_index,
                    "total_chunks": total_chunks,
                    "file_size": file_size,
                    "expected_sha256": expected_sha256,
                    "is_complete": is_complete,
                    "filename": filename,
                    "reset": reset,
                    "verify_only": verify_only,
                    "file_retention": file_retention,
                },
                extra,
            ),
        )

    async def download_file_stream(self, file=None, file_id=None, chunk_size=None, **extra):
        """流式下载（L1；三字段全可选，file/file_id 二选一 schema 未表达）。"""
        return await self._invoke(
            "download_file_stream",
            _params({"file": file, "file_id": file_id, "chunk_size": chunk_size}, extra),
        )

    async def download_file_image_stream(
        self, file=None, file_id=None, chunk_size=None, **extra
    ):
        """流式下载图片（L1；字段同 download_file_stream）。"""
        return await self._invoke(
            "download_file_image_stream",
            _params({"file": file, "file_id": file_id, "chunk_size": chunk_size}, extra),
        )

    async def download_file_record_stream(
        self, file=None, file_id=None, chunk_size=None, out_format=None, **extra
    ):
        """流式下载语音（L1；另有可选 out_format）。"""
        return await self._invoke(
            "download_file_record_stream",
            _params(
                {
                    "file": file,
                    "file_id": file_id,
                    "chunk_size": chunk_size,
                    "out_format": out_format,
                },
                extra,
            ),
        )


class AlbumActions(_Facade):
    """F 域群相册写门面（N5；读动作 get_qun_album_list /
    get_group_album_media_list 在 FileActions，不动）。

    字段表以 bundle schema 证据为准（docs/napcat-schema-evidence-n5.md）。
    相册动作是网关熔断的默认管控面（ActionGateway.circuit_actions
    由登记表 domain="F" 派生）。
    """

    async def upload_image_to_qun_album(self, group_id, album_id, album_name, file, **extra):
        """上传单图到群相册（L1）。"""
        return await self._invoke(
            "upload_image_to_qun_album",
            _params(
                {
                    "group_id": group_id,
                    "album_id": album_id,
                    "album_name": album_name,
                    "file": file,
                },
                extra,
            ),
        )

    async def upload_images_to_qun_album(self, group_id, album_id, album_name, files, **extra):
        """上传多图到群相册（L1，一帖多图）。"""
        return await self._invoke(
            "upload_images_to_qun_album",
            _params(
                {
                    "group_id": group_id,
                    "album_id": album_id,
                    "album_name": album_name,
                    "files": files,
                },
                extra,
            ),
        )

    async def upload_video_to_qun_album(self, group_id, album_id, album_name, file, **extra):
        """上传视频到群相册（L1）。"""
        return await self._invoke(
            "upload_video_to_qun_album",
            _params(
                {
                    "group_id": group_id,
                    "album_id": album_id,
                    "album_name": album_name,
                    "file": file,
                },
                extra,
            ),
        )

    async def set_group_album_media_like(self, group_id, album_id, batch_id, lloc=None, **extra):
        """相册媒体点赞（L1）。"""
        return await self._invoke(
            "set_group_album_media_like",
            _params(
                {"group_id": group_id, "album_id": album_id, "batch_id": batch_id, "lloc": lloc},
                extra,
            ),
        )

    async def cancel_group_album_media_like(
        self, group_id, album_id, batch_id, lloc=None, **extra
    ):
        """取消相册媒体点赞（L1；与 set 共用同一 schema）。"""
        return await self._invoke(
            "cancel_group_album_media_like",
            _params(
                {"group_id": group_id, "album_id": album_id, "batch_id": batch_id, "lloc": lloc},
                extra,
            ),
        )

    async def del_group_album_media(
        self, group_id, album_id, lloc, *,
        authorization=None, confirmation_token=None, **extra,
    ):
        """删相册媒体（L2 不可逆档：网关另须绑定确认令牌；
        证据字段含 group_id，角色复核按群管理员档执行）。"""
        return await self._invoke(
            "del_group_album_media",
            _params({"group_id": group_id, "album_id": album_id, "lloc": lloc}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def do_group_album_comment(
        self, group_id, album_id, content, lloc=None, batch_id=None, **extra
    ):
        """相册评论（L1）。bundle 类级 check() 约束：lloc 与 batch_id
        至少提供一个（lloc 为空表示评论整个批次，此时必须有
        batch_id）——schema 本身未表达此约束，门面层按 check() 拒绝
        双缺，不调网关。"""
        if not lloc and not batch_id:
            # 门面本地拒绝不经网关（与既有句柄门面拒绝同模式）：
            # 补一条与网关同格式的审计行，避免审计覆盖缺口
            # （Review P3-7，部分接受——约束本身仍在门面表达，
            # 因网关参数规则无法表达二选一）。
            logger.info(
                "action principal=%s authorizer=%s action=%s policy_key=%s "
                "outcome=%s gate=%s elapsed_ms=%d",
                _audit_token(self._principal),
                "none",
                "do_group_album_comment",
                "do_group_album_comment",
                "rejected",
                "invalid_params",
                0,
            )
            return ActionResult(outcome="rejected", gate="invalid_params")
        return await self._invoke(
            "do_group_album_comment",
            _params(
                {
                    "group_id": group_id,
                    "album_id": album_id,
                    "lloc": lloc,
                    "batch_id": batch_id,
                    "content": content,
                },
                extra,
            ),
        )


class ProfileActions(_Facade):
    """G 域资料与自身状态门面（L0/L1 部分 + N4 的 L2 账号资料）。"""

    async def get_online_clients(self, **extra):
        return await self._invoke("get_online_clients", _params({}, extra))

    async def get_mini_app_ark(self, **extra):
        """生成小程序 Ark 卡片数据（只生成不发送，故不要求投递句柄；
        真正发出仍走消息发送门面的句柄纪律）。"""
        return await self._invoke("get_mini_app_ark", _params({}, extra))

    async def set_online_status(self, status=None, **extra):
        """设置自身在线状态（L1 mutation）。"""
        return await self._invoke(
            "set_online_status", _params({"status": status}, extra)
        )

    async def set_diy_online_status(self, **extra):
        """设置自定义在线状态（mutation；schema 待验，参数透传）。"""
        return await self._invoke("set_diy_online_status", _params({}, extra))

    async def get_model_show(self, **extra):
        """取机型展示（mutation 形态登记；schema 待验，参数透传）。"""
        return await self._invoke("_get_model_show", _params({}, extra))

    async def set_model_show(self, **extra):
        """设置机型展示（mutation；schema 待验，参数透传）。"""
        return await self._invoke("_set_model_show", _params({}, extra))

    # -- N4：L2 账号资料（目标为 account，授权上下文须 account=True） --

    async def set_qq_profile(
        self, *, authorization=None, confirmation_token=None, **extra
    ):
        """改自身资料（L2；字段经 extra 透传，如 nickname/sex）。"""
        return await self._invoke(
            "set_qq_profile",
            _params({}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def set_qq_avatar(
        self, file, *, authorization=None, confirmation_token=None, **extra
    ):
        """改自身头像（L2）。"""
        return await self._invoke(
            "set_qq_avatar",
            _params({"file": file}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )

    async def set_self_longnick(
        self, longnick, *, authorization=None, confirmation_token=None, **extra
    ):
        """改自身签名（L2）。"""
        return await self._invoke(
            "set_self_longnick",
            _params({"longnick": longnick}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )


class SystemActions(_Facade):
    """H 域系统门面（L0 部分 + N4 的 L2 clean_cache）。"""

    async def get_login_info(self, **extra):
        return await self._invoke("get_login_info", _params({}, extra))

    async def get_status(self, **extra):
        return await self._invoke("get_status", _params({}, extra))

    async def get_version_info(self, **extra):
        return await self._invoke("get_version_info", _params({}, extra))

    async def get_packet_status(self, **extra):
        return await self._invoke("nc_get_packet_status", _params({}, extra))

    async def check_url_safely(self, url, **extra):
        return await self._invoke("check_url_safely", _params({"url": url}, extra))

    async def clean_cache(
        self, *, authorization=None, confirmation_token=None, **extra
    ):
        """清缓存（L2 运维动作，N4；授权上下文须 account=True）。"""
        return await self._invoke(
            "clean_cache",
            _params({}, extra),
            authorization=authorization,
            confirmation_token=confirmation_token,
        )


class SensitiveActions(_Facade):
    """H 域敏感门面（L3）：方法存在，Gateway 默认硬禁；门面不预判。

    N6 起 Gateway 支持受控解锁（构造期 l3_unlocked 名单，运维配置
    + 重启生效）：解锁后也只有持 ops_cli 主体 + 授权上下文的调用
    能真正派发，且经本门面的调用方须以 gate 参数显式传
    authorization（见 _Facade._invoke 的 gate 透传）。运行时服务
    内无任何调用点（tests/test_l3_unlock_n6.py 有收敛断言）。
    """

    # 授权上下文以 gate 参数显式传入（N4 L2 门面同款口径）：解锁后
    # 网关 c3 段要求 AuthorizationContext，缺省 None 即在网关被拒，
    # 不会被 **extra 吞进平台 params。
    async def get_cookies(self, domain, *, authorization=None, **extra):
        # domain 为网关必填（_PARAM_RULES 与 bundle schema 证据）：
        # 门面签名同步设为必填（补审第二段 P4）——此前默认 None 的
        # 调用必然在网关 invalid_params，类型化签名不得广告必败形态。
        return await self._invoke(
            "get_cookies",
            _params({"domain": domain}, extra),
            authorization=authorization,
        )

    async def get_csrf_token(self, *, authorization=None, **extra):
        return await self._invoke(
            "get_csrf_token", _params({}, extra), authorization=authorization
        )

    async def get_credentials(self, domain, *, authorization=None, **extra):
        # domain 必填口径同 get_cookies（补审第二段 P4）。
        return await self._invoke(
            "get_credentials",
            _params({"domain": domain}, extra),
            authorization=authorization,
        )

    async def get_clientkey(self, *, authorization=None, **extra):
        return await self._invoke(
            "get_clientkey", _params({}, extra), authorization=authorization
        )

    async def get_rkey(self, *, authorization=None, **extra):
        return await self._invoke(
            "get_rkey", _params({}, extra), authorization=authorization
        )

    async def get_rkey_server(self, *, authorization=None, **extra):
        return await self._invoke(
            "get_rkey_server", _params({}, extra), authorization=authorization
        )

    async def nc_get_rkey(self, *, authorization=None, **extra):
        return await self._invoke(
            "nc_get_rkey", _params({}, extra), authorization=authorization
        )

    async def send_packet(
        self, cmd, data, rsp=None, *, authorization=None, **extra
    ):
        # N6 类型化（bundle schema 证据：cmd/data 必填、rsp 可选带
        # 默认，docs/napcat-schema-evidence-n6.md）；旧 dict 入参形态
        # 已随终审下线。rsp=None 时不进 params（用平台默认值）。
        # cmd/data 签名必填与网关 _PARAM_RULES 对齐（补审第二段 P4）。
        return await self._invoke(
            "send_packet",
            _params({"cmd": cmd, "data": data, "rsp": rsp}, extra),
            authorization=authorization,
        )


class ActionsBundle:
    """九组门面的聚合体：同一 Gateway、同一执行主体（N5 新增 album）。"""

    def __init__(self, gateway, principal=PRINCIPAL_SYSTEM):
        self.gateway = gateway
        self.principal = principal
        self.messaging = MessagingActions(gateway, principal)
        self.group = GroupActions(gateway, principal)
        self.social = SocialActions(gateway, principal)
        self.media = MediaActions(gateway, principal)
        self.files = FileActions(gateway, principal)
        self.album = AlbumActions(gateway, principal)
        self.profile = ProfileActions(gateway, principal)
        self.system = SystemActions(gateway, principal)
        self.sensitive = SensitiveActions(gateway, principal)


def make_actions(gateway, principal=PRINCIPAL_SYSTEM) -> ActionsBundle:
    """构造 ActionsBundle 的自由函数入口。"""
    return ActionsBundle(gateway, principal)
