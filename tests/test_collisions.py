"""Same-file qualified_name collisions must not silently drop nodes.

Same-named symbols in one file (methods of different object literals, repeated
generated helpers) produce the same qualified_name. Before the fix, the insert's
ON CONFLICT(qualified_name) DO UPDATE collapsed them last-writer-wins and the
earlier definitions vanished. The batch store now disambiguates them.
"""

import tempfile
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo


def _func(name, line, path="/repo/db.ts", parent=None):
    return NodeInfo(
        kind="Function", name=name, file_path=path,
        line_start=line, line_end=line + 2, language="typescript",
        parent_name=parent,
    )


class TestQualifiedNameCollisions:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_same_name_methods_both_persist(self):
        # Two object literals each defining getById -> empty parent_name -> collide.
        self.store.store_file_nodes_edges(
            "/repo/db.ts", [_func("getById", 10), _func("getById", 20)], []
        )
        assert self.store.get_stats().total_nodes == 2
        # First occurrence keeps the bare key; the second carries a line suffix.
        first = self.store.get_node("/repo/db.ts::getById")
        assert first is not None and first.line_start == 10
        second = self.store.get_node("/repo/db.ts::getById:L20")
        assert second is not None and second.line_start == 20

    def test_three_same_name_module_functions_persist(self):
        nodes = [_func("rootRouteImport", n) for n in (5, 10, 15)]
        self.store.store_file_nodes_edges("/repo/routeTree.gen.ts", nodes, [])
        assert self.store.get_stats().total_nodes == 3

    def test_edge_to_bare_key_resolves_to_first_def(self):
        # An edge targeting the bare key still names a real node (the first def),
        # so collisions never dangle existing edges.
        self.store.store_file_nodes_edges(
            "/repo/db.ts", [_func("getById", 10), _func("getById", 20)], []
        )
        edge = EdgeInfo(
            kind="CALLS", source="/repo/api.ts::handler",
            target="/repo/db.ts::getById", file_path="/repo/api.ts", line=3,
        )
        self.store.store_file_nodes_edges(
            "/repo/api.ts", [_func("handler", 1, path="/repo/api.ts")], [edge]
        )
        target = self.store.get_node("/repo/db.ts::getById")
        assert target is not None and target.line_start == 10

    def test_find_disambiguated_nodes_lists_suffixed_keys(self):
        self.store.store_file_nodes_edges(
            "/repo/db.ts", [_func("getById", 10), _func("getById", 20)], []
        )
        assert self.store.find_disambiguated_nodes() == ["/repo/db.ts::getById:L20"]

    def test_unique_names_keep_bare_keys_unchanged(self):
        self.store.store_file_nodes_edges(
            "/repo/db.ts", [_func("a", 10), _func("b", 20)], []
        )
        assert self.store.find_disambiguated_nodes() == []
        assert self.store.get_node("/repo/db.ts::a") is not None
        assert self.store.get_node("/repo/db.ts::b") is not None
