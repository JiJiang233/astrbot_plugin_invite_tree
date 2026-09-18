import os
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_invite_tree.graph import RelationshipGraph
from astrbot_plugin_invite_tree.renderer import FONT_PATH, _bezier_points, render_tree

TEST_CACHE = Path(os.environ.get("ASTRBOT_PLUGIN_TEST_CACHE", Path.cwd() / "cache"))
TEST_CACHE.mkdir(parents=True, exist_ok=True)


def build_graph(path: Path) -> RelationshipGraph:
    graph = RelationshipGraph(path)
    graph.load()
    graph.ensure_node("bot:1", "bot", "1", "Bot 1")
    graph.ensure_node("group:10", "group", "10", "起点群")
    graph.ensure_node("user:20", "user", "20", "用户甲")
    graph.ensure_node("group:30", "group", "30", "新群")
    graph.add_edge("bot:1", "group:10", "bot_in_group")
    graph.add_edge("group:10", "user:20", "friend_from_group")
    graph.add_edge("user:20", "group:30", "invited_bot_to_group")
    return graph


class GraphTests(unittest.TestCase):
    def test_round_trip_and_deduplication(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEST_CACHE) as directory:
            path = Path(directory) / "tree.json"
            graph = build_graph(path)
            self.assertFalse(
                graph.add_edge("user:20", "group:30", "invited_bot_to_group")
            )
            graph.save()

            loaded = RelationshipGraph(path)
            loaded.load()
            self.assertEqual(loaded.stats(), {"users": 2, "groups": 2, "edges": 3})
            tree = loaded.project_tree("bot:1")
            self.assertEqual(tree["children"][0]["id"], "group:10")
            self.assertEqual(tree["children"][0]["children"][0]["id"], "user:20")

    def test_projection_is_cycle_safe(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEST_CACHE) as directory:
            graph = build_graph(Path(directory) / "tree.json")
            graph.add_edge("group:30", "user:20", "ambiguous_source")
            tree = graph.project_tree("bot:1")

            def count(item: dict) -> int:
                return 1 + sum(count(child) for child in item.get("children", []))

            self.assertEqual(count(tree), 4)

    def test_source_group_beats_root_fallback(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEST_CACHE) as directory:
            graph = build_graph(Path(directory) / "tree.json")
            graph.add_edge("bot:1", "user:20", "friend_request")
            tree = graph.project_tree("bot:1")
            group = next(
                child for child in tree["children"] if child["id"] == "group:10"
            )
            self.assertEqual(group["children"][0]["id"], "user:20")

    def test_renderer_creates_png(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEST_CACHE) as directory:
            temp_dir = Path(directory)
            graph = build_graph(temp_dir / "tree.json")
            output = temp_dir / "tree.png"
            render_tree(graph.project_tree("bot:1"), {}, output)
            self.assertTrue(output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

    def test_bundled_font_and_bezier_connector(self) -> None:
        self.assertTrue(FONT_PATH.is_file())
        points = _bezier_points((0.0, 0.0), (120.0, 80.0))
        self.assertEqual(points[0], (0.0, 0.0))
        self.assertEqual(points[-1], (120.0, 80.0))
        self.assertGreater(len(points), 20)


if __name__ == "__main__":
    unittest.main()
