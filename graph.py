"""Persistent relationship graph with cycle-safe tree projection."""

from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
RELATION_PRIORITY = {
    "friend_from_group": 100,
    "invited_bot_to_group": 100,
    "group_member_joined": 80,
    "bot_in_group": 50,
    "friend_request": 20,
    "friend_added": 20,
    "known_user": 10,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RelationshipGraph:
    """JSON-backed graph. A node may have several observed parent relations."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.data: dict[str, Any] = self._empty_data()

    @staticmethod
    def _empty_data() -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "nodes": {},
            "edges": [],
            "processed_events": {},
        }

    def load(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                self.data = self._empty_data()
                return
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("关系数据根节点必须是对象")
            if int(raw.get("schema_version", 0)) > SCHEMA_VERSION:
                raise ValueError("关系数据版本高于当前插件支持版本")
            raw.setdefault("nodes", {})
            raw.setdefault("edges", [])
            raw.setdefault("processed_events", {})
            self.data = raw

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)

    def ensure_node(
        self,
        node_id: str,
        node_type: str,
        external_id: str,
        label: str = "",
        avatar_url: str = "",
        **extra: Any,
    ) -> bool:
        with self._lock:
            now = utc_now()
            nodes = self.data["nodes"]
            node = nodes.get(node_id)
            changed = node is None
            if node is None:
                node = {
                    "id": node_id,
                    "type": node_type,
                    "external_id": str(external_id),
                    "label": label or str(external_id),
                    "avatar_url": avatar_url,
                    "created_at": now,
                    "updated_at": now,
                }
                nodes[node_id] = node
            updates = {
                "type": node_type,
                "external_id": str(external_id),
            }
            if label:
                updates["label"] = label
            if avatar_url:
                updates["avatar_url"] = avatar_url
            updates.update({k: v for k, v in extra.items() if v not in (None, "")})
            for key, value in updates.items():
                if node.get(key) != value:
                    node[key] = value
                    changed = True
            if changed:
                node["updated_at"] = now
            return changed

    def add_edge(
        self,
        parent: str,
        child: str,
        relation: str,
        *,
        source: str = "",
        observed_at: str | None = None,
    ) -> bool:
        if parent == child:
            return False
        with self._lock:
            for edge in self.data["edges"]:
                if (
                    edge.get("parent") == parent
                    and edge.get("child") == child
                    and edge.get("relation") == relation
                ):
                    edge["last_seen_at"] = observed_at or utc_now()
                    if source:
                        edge["source"] = source
                    return False
            now = observed_at or utc_now()
            self.data["edges"].append(
                {
                    "parent": parent,
                    "child": child,
                    "relation": relation,
                    "source": source,
                    "created_at": now,
                    "last_seen_at": now,
                }
            )
            return True

    def has_edge(self, parent: str, child: str) -> bool:
        return any(
            edge.get("parent") == parent and edge.get("child") == child
            for edge in self.data["edges"]
        )

    def has_incoming(self, node_id: str) -> bool:
        return any(edge.get("child") == node_id for edge in self.data["edges"])

    def parent_groups(self, node_id: str) -> list[str]:
        return [
            str(edge["parent"])
            for edge in self.data["edges"]
            if edge.get("child") == node_id
            and str(edge.get("parent", "")).startswith("group:")
        ]

    def mark_processed(self, event_key: str) -> None:
        with self._lock:
            processed = self.data["processed_events"]
            processed[event_key] = utc_now()
            if len(processed) > 5000:
                keep = sorted(processed.items(), key=lambda item: item[1])[-4000:]
                self.data["processed_events"] = dict(keep)

    def was_processed(self, event_key: str) -> bool:
        return event_key in self.data["processed_events"]

    def stats(self) -> dict[str, int]:
        nodes = self.data["nodes"]
        return {
            "users": sum(
                node.get("type") in {"user", "bot"} for node in nodes.values()
            ),
            "groups": sum(node.get("type") == "group" for node in nodes.values()),
            "edges": len(self.data["edges"]),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self.data)

    def project_tree(self, root_id: str) -> dict[str, Any]:
        """Return a deterministic, cycle-safe spanning tree of the graph."""
        snap = self.snapshot()
        nodes = snap["nodes"]
        if root_id not in nodes:
            raise KeyError(root_id)
        adjacency: dict[str, list[dict[str, Any]]] = {}
        for edge in snap["edges"]:
            if edge.get("parent") in nodes and edge.get("child") in nodes:
                adjacency.setdefault(str(edge["parent"]), []).append(edge)
        for edges in adjacency.values():
            edges.sort(
                key=lambda edge: (
                    -RELATION_PRIORITY.get(str(edge.get("relation", "")), 0),
                    str(nodes[edge["child"]].get("type", "")),
                    str(nodes[edge["child"]].get("label", "")),
                    str(edge["child"]),
                )
            )

        visited: set[str] = set()

        def build(node_id: str, ancestry: set[str]) -> dict[str, Any]:
            item = dict(nodes[node_id])
            item["children"] = []
            visited.add(node_id)
            for edge in adjacency.get(node_id, []):
                child_id = str(edge["child"])
                if child_id in ancestry or child_id in visited:
                    continue
                child = build(child_id, ancestry | {child_id})
                child["relation"] = edge.get("relation", "")
                item["children"].append(child)
            return item

        return build(root_id, {root_id})
