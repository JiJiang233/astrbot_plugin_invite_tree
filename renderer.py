"""Pillow renderer for avatar relationship trees."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

USER_COLOR = "#4F8EF7"
GROUP_COLOR = "#F06BA8"
BOT_COLOR = "#22A699"
BACKGROUND = "#F7F9FC"
INK = "#263238"
LINE = "#B8C2CC"


@dataclass
class LayoutNode:
    data: dict[str, Any]
    depth: int
    x: int = 0
    y: int = 0


def _font(size: int) -> ImageFont.ImageFont:
    candidates = [
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
    ]
    for path in candidates:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                pass
    return ImageFont.load_default()


def _flatten(
    root: dict[str, Any],
) -> tuple[list[LayoutNode], list[tuple[LayoutNode, LayoutNode]]]:
    nodes: list[LayoutNode] = []
    edges: list[tuple[LayoutNode, LayoutNode]] = []

    def walk(item: dict[str, Any], depth: int, parent: LayoutNode | None) -> None:
        current = LayoutNode(item, depth)
        nodes.append(current)
        if parent is not None:
            edges.append((parent, current))
        for child in item.get("children", []):
            walk(child, depth + 1, current)

    walk(root, 0, None)
    return nodes, edges


def render_tree(
    tree: dict[str, Any],
    avatars: dict[str, Path],
    output: Path,
    *,
    title: str = "邀请关系树",
) -> Path:
    nodes, edges = _flatten(tree)
    row_gap = 116
    col_gap = 260
    margin_x = 90
    top = 140
    max_depth = max((node.depth for node in nodes), default=0)
    width = max(900, margin_x * 2 + max_depth * col_gap + 260)
    height = max(420, top + len(nodes) * row_gap + 90)
    canvas = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    title_font = _font(34)
    text_font = _font(21)
    id_font = _font(16)
    draw.text((margin_x, 45), title, fill=INK, font=title_font)
    draw.text(
        (margin_x, 92),
        "蓝色：用户   粉色：群聊   绿色：机器人",
        fill="#687785",
        font=id_font,
    )

    for index, node in enumerate(nodes):
        node.x = margin_x + node.depth * col_gap
        node.y = top + index * row_gap

    for parent, child in edges:
        start = (parent.x + 86, parent.y + 43)
        end = (child.x - 13, child.y + 43)
        mid_x = (start[0] + end[0]) // 2
        draw.line([start, (mid_x, start[1]), (mid_x, end[1]), end], fill=LINE, width=4)
        draw.polygon(
            [(end[0], end[1]), (end[0] - 10, end[1] - 6), (end[0] - 10, end[1] + 6)],
            fill=LINE,
        )

    for node in nodes:
        data = node.data
        node_type = str(data.get("type", "user"))
        color = (
            BOT_COLOR
            if node_type == "bot"
            else GROUP_COLOR
            if node_type == "group"
            else USER_COLOR
        )
        center = (node.x + 43, node.y + 43)
        draw.ellipse(
            (center[0] - 43, center[1] - 43, center[0] + 43, center[1] + 43),
            fill="white",
            outline=color,
            width=7,
        )
        avatar_path = avatars.get(str(data.get("id", "")))
        if avatar_path and avatar_path.exists():
            try:
                avatar = Image.open(avatar_path).convert("RGB")
                avatar = ImageOps.fit(avatar, (70, 70), method=Image.Resampling.LANCZOS)
                mask = Image.new("L", (70, 70), 0)
                ImageDraw.Draw(mask).ellipse((0, 0, 69, 69), fill=255)
                canvas.paste(avatar, (center[0] - 35, center[1] - 35), mask)
            except OSError:
                _draw_placeholder(draw, center, data, color, text_font)
        else:
            _draw_placeholder(draw, center, data, color, text_font)

        label = str(data.get("label") or data.get("external_id") or data.get("id"))
        if len(label) > 15:
            label = label[:14] + "…"
        external_id = str(data.get("external_id", ""))
        draw.text((node.x + 100, node.y + 14), label, fill=INK, font=text_font)
        draw.text(
            (node.x + 100, node.y + 50), external_id, fill="#718096", font=id_font
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, format="PNG", optimize=True)
    return output


def _draw_placeholder(
    draw: ImageDraw.ImageDraw,
    center: tuple[int, int],
    data: dict[str, Any],
    color: str,
    font: ImageFont.ImageFont,
) -> None:
    text = (
        "群"
        if data.get("type") == "group"
        else "机"
        if data.get("type") == "bot"
        else "人"
    )
    box = draw.textbbox((0, 0), text, font=font)
    width = math.ceil(box[2] - box[0])
    height = math.ceil(box[3] - box[1])
    draw.text(
        (center[0] - width // 2, center[1] - height // 2 - 2),
        text,
        fill=color,
        font=font,
    )
