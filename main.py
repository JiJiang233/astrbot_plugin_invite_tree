"""Track OneBot friend/group relationships and render them as an image tree."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .graph import RelationshipGraph
from .renderer import render_tree

PLUGIN_NAME = "astrbot_plugin_invite_tree"
SOURCE_GROUP_FIELDS = (
    "source_group_id",
    "from_group_id",
    "from_group",
    "group_id",
)
GROUP_NAME_PATTERN = re.compile(
    r"来自[「『\[【]?([^「」『』\[\]【】\n]{2,40})[」』\]】]?的"
)


class InviteTreePlugin(Star):
    """Build a persistent bot -> group -> user -> group relationship tree."""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = config or {}
        self.data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.avatar_dir = self.data_dir / "avatars"
        self.output_dir = self.data_dir / "generated"
        self.graph = RelationshipGraph(self.data_dir / "relationship_tree.json")
        self._ready = False
        self._mutation_lock = asyncio.Lock()
        self._render_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    async def initialize(self) -> None:
        try:
            await asyncio.to_thread(self.graph.load)
            self.avatar_dir.mkdir(parents=True, exist_ok=True)
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self._ready = True
            logger.info("[invite_tree] 关系数据已加载：%s", self.graph.stats())
        except Exception as exc:  # noqa: BLE001 - lifecycle boundary must stay alive
            self._ready = False
            logger.exception(
                "[invite_tree] 关系数据加载失败，已停止写入以保护原文件: %s", exc
            )

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def observe_onebot_event(self, event: AstrMessageEvent) -> None:
        """Observe OneBot v11 request/notice events without consuming them."""
        if not self._ready:
            return
        raw = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw, Mapping):
            return
        payload = dict(raw)
        post_type = str(payload.get("post_type", ""))
        if post_type not in {"request", "notice"}:
            return
        task = asyncio.create_task(self._process_payload(event, payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process_payload(
        self, event: AstrMessageEvent, payload: dict[str, Any]
    ) -> None:
        try:
            async with self._mutation_lock:
                event_key = self._event_key(payload)
                if self.graph.was_processed(event_key):
                    return
                changed = await self._handle_event(event, payload)
                if changed is None:
                    return
                self.graph.mark_processed(event_key)
                await asyncio.to_thread(self.graph.save)
        except Exception as exc:  # noqa: BLE001 - isolate malformed platform events
            logger.exception(
                "[invite_tree] 处理 OneBot 事件失败: %s; raw=%r", exc, payload
            )

    async def terminate(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关系树", alias={"邀请树", "好友树"})
    async def show_tree(self, event: AstrMessageEvent) -> Any:
        """生成并查看好友、群聊邀请关系树。"""
        if not self._ready:
            yield event.plain_result("关系数据未成功加载，请检查插件日志和数据文件。")
            return
        root_id = self._bot_node_id(event.get_self_id())
        if root_id not in self.graph.snapshot()["nodes"]:
            yield event.plain_result("暂时没有记录到关系事件。")
            return
        try:
            async with self._render_lock:
                tree = self.graph.project_tree(root_id)
                avatars = await self._prepare_avatars(tree)
                output = self.output_dir / f"invite_tree_{event.get_self_id()}.png"
                await asyncio.to_thread(
                    render_tree,
                    tree,
                    avatars,
                    output,
                    title=f"邀请关系树 · Bot {event.get_self_id()}",
                )
            stats = self.graph.stats()
            yield event.chain_result(
                [
                    Plain(
                        f"已记录 {stats['users']} 个用户/机器人、"
                        f"{stats['groups']} 个群、{stats['edges']} 条关系。\n"
                    ),
                    Image.fromFileSystem(str(output)),
                ]
            )
        except Exception as exc:  # noqa: BLE001 - command must return a readable error
            logger.exception("[invite_tree] 生成关系树失败: %s", exc)
            yield event.plain_result(f"关系树生成失败：{exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关系树状态")
    async def tree_status(self, event: AstrMessageEvent) -> Any:
        """查看关系树持久化状态和统计信息。"""
        stats = (
            self.graph.stats() if self._ready else {"users": 0, "groups": 0, "edges": 0}
        )
        yield event.plain_result(
            "邀请关系树状态\n"
            f"数据加载：{'正常' if self._ready else '失败'}\n"
            f"用户/机器人：{stats['users']}\n"
            f"群聊：{stats['groups']}\n"
            f"关系：{stats['edges']}\n"
            f"数据文件：{self.graph.path}"
        )

    async def _handle_event(
        self, event: AstrMessageEvent, raw: dict[str, Any]
    ) -> bool | None:
        post_type = str(raw.get("post_type", ""))
        if post_type == "request":
            request_type = str(raw.get("request_type", ""))
            if request_type == "friend":
                return await self._record_friend_request(event, raw)
            if request_type == "group" and str(raw.get("sub_type", "")) == "invite":
                return await self._record_group_invite(event, raw)
            return None

        notice_type = str(raw.get("notice_type", ""))
        if notice_type == "friend_add":
            return await self._record_friend_add(event, raw)
        if notice_type == "group_increase":
            return await self._record_group_increase(event, raw)
        return None

    async def _record_friend_request(
        self, event: AstrMessageEvent, raw: dict[str, Any]
    ) -> bool:
        user_id = self._id(raw.get("user_id"))
        self_id = self._id(raw.get("self_id") or event.get_self_id())
        if not user_id or not self_id:
            return False
        user_info = await self._user_info(event, user_id)
        changed = self._ensure_bot(self_id)
        changed |= self._ensure_user(user_id, user_info)
        groups = await self._find_source_groups(event, raw, user_id)
        if groups:
            for group in groups:
                group_id = self._id(group.get("group_id"))
                if not group_id:
                    continue
                changed |= self._ensure_group(group_id, group)
                changed |= self.graph.add_edge(
                    self._bot_node_id(self_id),
                    self._group_node_id(group_id),
                    "bot_in_group",
                    source="friend_request_source",
                )
                changed |= self.graph.add_edge(
                    self._group_node_id(group_id),
                    self._user_node_id(user_id),
                    "friend_from_group",
                    source="friend_request",
                )
        else:
            changed |= self.graph.add_edge(
                self._bot_node_id(self_id),
                self._user_node_id(user_id),
                "friend_request",
                source="friend_request",
            )
        return changed

    async def _record_friend_add(
        self, event: AstrMessageEvent, raw: dict[str, Any]
    ) -> bool:
        user_id = self._id(raw.get("user_id"))
        self_id = self._id(raw.get("self_id") or event.get_self_id())
        if not user_id or not self_id:
            return False
        changed = self._ensure_bot(self_id)
        changed |= self._ensure_user(user_id, await self._user_info(event, user_id))
        if not self.graph.has_incoming(self._user_node_id(user_id)):
            changed |= self.graph.add_edge(
                self._bot_node_id(self_id),
                self._user_node_id(user_id),
                "friend_added",
                source="friend_add_notice",
            )
        return changed

    async def _record_group_invite(
        self, event: AstrMessageEvent, raw: dict[str, Any]
    ) -> bool:
        inviter_id = self._id(raw.get("user_id"))
        group_id = self._id(raw.get("group_id"))
        self_id = self._id(raw.get("self_id") or event.get_self_id())
        if not inviter_id or not group_id or not self_id:
            return False
        changed = self._ensure_bot(self_id)
        changed |= self._ensure_user(
            inviter_id, await self._user_info(event, inviter_id)
        )
        changed |= self._ensure_group(group_id, await self._group_info(event, group_id))
        inviter_node = self._user_node_id(inviter_id)
        if not self.graph.has_incoming(inviter_node):
            changed |= self.graph.add_edge(
                self._bot_node_id(self_id),
                inviter_node,
                "known_user",
                source="group_invite",
            )
        group_node = self._group_node_id(group_id)
        if self.graph.has_edge(group_node, inviter_node):
            changed |= self.graph.add_edge(
                self._bot_node_id(self_id),
                group_node,
                "bot_in_group",
                source="same_group_invite",
            )
        else:
            changed |= self.graph.add_edge(
                inviter_node,
                group_node,
                "invited_bot_to_group",
                source="group_invite_request",
            )
        return changed

    async def _record_group_increase(
        self, event: AstrMessageEvent, raw: dict[str, Any]
    ) -> bool:
        group_id = self._id(raw.get("group_id"))
        user_id = self._id(raw.get("user_id"))
        self_id = self._id(raw.get("self_id") or event.get_self_id())
        operator_id = self._id(raw.get("operator_id"))
        if not group_id or not user_id or not self_id:
            return False
        if user_id != self_id:
            # 普通成员入群不是“好友来源/拉群”关系，不写入关系树，避免大型群噪声。
            return False
        changed = self._ensure_bot(self_id)
        changed |= self._ensure_group(group_id, await self._group_info(event, group_id))
        group_node = self._group_node_id(group_id)
        if operator_id and operator_id != self_id:
            changed |= self._ensure_user(
                operator_id, await self._user_info(event, operator_id)
            )
            operator_node = self._user_node_id(operator_id)
            if not self.graph.has_incoming(operator_node):
                changed |= self.graph.add_edge(
                    self._bot_node_id(self_id),
                    operator_node,
                    "known_user",
                    source="group_increase",
                )
            if self.graph.has_edge(group_node, operator_node):
                changed |= self.graph.add_edge(
                    self._bot_node_id(self_id),
                    group_node,
                    "bot_in_group",
                    source="same_group_invite",
                )
            else:
                changed |= self.graph.add_edge(
                    operator_node,
                    group_node,
                    "invited_bot_to_group",
                    source="group_increase_notice",
                )
        elif not self.graph.has_incoming(group_node):
            changed |= self.graph.add_edge(
                self._bot_node_id(self_id),
                group_node,
                "bot_in_group",
                source="group_increase_notice",
            )
        return changed

    async def _find_source_groups(
        self, event: AstrMessageEvent, raw: dict[str, Any], user_id: str
    ) -> list[dict[str, Any]]:
        explicit_id = next(
            (
                self._id(raw.get(field))
                for field in SOURCE_GROUP_FIELDS
                if self._id(raw.get(field))
            ),
            "",
        )
        if explicit_id:
            return [await self._group_info(event, explicit_id)]
        if not bool(self.config.get("infer_source_group", True)):
            return []

        groups = await self._action(event, "get_group_list")
        if not isinstance(groups, list):
            return []
        comment = str(raw.get("comment") or "")
        name_match = GROUP_NAME_PATTERN.search(comment)
        if name_match:
            source_name = name_match.group(1).strip()
            named = [
                group
                for group in groups
                if isinstance(group, dict)
                and str(group.get("group_name", "")).strip() == source_name
            ]
            if named:
                return named[:1]

        limit = max(1, int(self.config.get("source_scan_group_limit", 80) or 80))
        concurrency = max(
            1, min(10, int(self.config.get("source_scan_concurrency", 4) or 4))
        )
        semaphore = asyncio.Semaphore(concurrency)

        async def contains(group: dict[str, Any]) -> dict[str, Any] | None:
            group_id = self._id(group.get("group_id"))
            if not group_id:
                return None
            async with semaphore:
                info = await self._action(
                    event,
                    "get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(user_id),
                    no_cache=False,
                )
            return (
                group
                if isinstance(info, dict) and info.get("user_id") is not None
                else None
            )

        candidates = [group for group in groups[:limit] if isinstance(group, dict)]
        results = await asyncio.gather(*(contains(group) for group in candidates))
        matches = [group for group in results if group is not None]
        if len(matches) > 1:
            logger.info(
                "[invite_tree] 用户 %s 同时存在于 %d 个群，全部记录为候选来源",
                user_id,
                len(matches),
            )
        return matches

    async def _user_info(
        self, event: AstrMessageEvent, user_id: str, group_id: str = ""
    ) -> dict[str, Any]:
        if group_id:
            info = await self._action(
                event,
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=False,
            )
            if isinstance(info, dict) and info:
                return info
        info = await self._action(
            event, "get_stranger_info", user_id=int(user_id), no_cache=False
        )
        return info if isinstance(info, dict) else {}

    async def _group_info(
        self, event: AstrMessageEvent, group_id: str
    ) -> dict[str, Any]:
        info = await self._action(
            event, "get_group_info", group_id=int(group_id), no_cache=False
        )
        result = dict(info) if isinstance(info, dict) else {}
        result.setdefault("group_id", group_id)
        return result

    async def _action(self, event: AstrMessageEvent, action: str, **kwargs: Any) -> Any:
        bot = getattr(event, "bot", None)
        if bot is None:
            return None
        self_id = self._id(getattr(event.message_obj, "self_id", ""))
        routed = dict(kwargs)
        if self_id:
            routed["self_id"] = self_id
        try:
            return await bot.call_action(action, **routed)
        except TypeError:
            try:
                return await bot.call_action(action, **kwargs)
            except Exception as exc:  # noqa: BLE001 - protocol clients vary by backend
                logger.debug("[invite_tree] OneBot action %s 失败: %s", action, exc)
        except Exception as exc:  # noqa: BLE001 - protocol clients vary by backend
            logger.debug("[invite_tree] OneBot action %s 失败: %s", action, exc)
        return None

    def _ensure_bot(self, self_id: str) -> bool:
        return self.graph.ensure_node(
            self._bot_node_id(self_id),
            "bot",
            self_id,
            f"Bot {self_id}",
            self._user_avatar_url(self_id),
        )

    def _ensure_user(self, user_id: str, info: dict[str, Any]) -> bool:
        label = str(
            info.get("card") or info.get("nickname") or info.get("nick") or user_id
        )
        return self.graph.ensure_node(
            self._user_node_id(user_id),
            "user",
            user_id,
            label,
            self._user_avatar_url(user_id),
        )

    def _ensure_group(self, group_id: str, info: dict[str, Any]) -> bool:
        label = str(info.get("group_name") or f"群 {group_id}")
        return self.graph.ensure_node(
            self._group_node_id(group_id),
            "group",
            group_id,
            label,
            self._group_avatar_url(group_id),
        )

    async def _prepare_avatars(self, tree: dict[str, Any]) -> dict[str, Path]:
        nodes: list[dict[str, Any]] = []

        def collect(item: dict[str, Any]) -> None:
            nodes.append(item)
            for child in item.get("children", []):
                collect(child)

        collect(tree)
        if not bool(self.config.get("download_avatars", True)):
            return {}
        timeout = aiohttp.ClientTimeout(total=12)
        semaphore = asyncio.Semaphore(6)
        async with aiohttp.ClientSession(timeout=timeout) as session:

            async def fetch(node: dict[str, Any]) -> tuple[str, Path] | None:
                url = str(node.get("avatar_url") or "")
                if not url:
                    return None
                node_id = str(node.get("id"))
                filename = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24] + ".jpg"
                target = self.avatar_dir / filename
                max_age = max(
                    3600, int(self.config.get("avatar_cache_hours", 168) or 168) * 3600
                )
                if target.exists() and time.time() - target.stat().st_mtime < max_age:
                    return node_id, target
                async with semaphore:
                    try:
                        async with session.get(url) as response:
                            if response.status != 200:
                                return None
                            content = await response.read()
                        if not content:
                            return None
                        tmp = target.with_suffix(".tmp")
                        await asyncio.to_thread(tmp.write_bytes, content)
                        await asyncio.to_thread(tmp.replace, target)
                        return node_id, target
                    except Exception as exc:  # noqa: BLE001 - avatar failures are optional
                        logger.debug("[invite_tree] 头像下载失败 %s: %s", url, exc)
                        return None

            fetched = await asyncio.gather(*(fetch(node) for node in nodes))
        return {
            node_id: path for result in fetched if result for node_id, path in [result]
        }

    def _event_key(self, raw: dict[str, Any]) -> str:
        stable = "|".join(
            str(raw.get(key, ""))
            for key in (
                "post_type",
                "request_type",
                "notice_type",
                "sub_type",
                "self_id",
                "user_id",
                "group_id",
                "operator_id",
                "flag",
                "time",
            )
        )
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()

    @staticmethod
    def _id(value: Any) -> str:
        text = str(value or "").strip()
        return text if text.isdigit() else ""

    @staticmethod
    def _bot_node_id(value: str) -> str:
        return f"bot:{value}"

    @staticmethod
    def _user_node_id(value: str) -> str:
        return f"user:{value}"

    @staticmethod
    def _group_node_id(value: str) -> str:
        return f"group:{value}"

    @staticmethod
    def _user_avatar_url(user_id: str) -> str:
        return f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"

    @staticmethod
    def _group_avatar_url(group_id: str) -> str:
        return f"https://p.qlogo.cn/gh/{group_id}/{group_id}/640"
