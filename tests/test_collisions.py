"""Same-file qualified_name collisions must keep a stable, scope-derived identity.

Two same-named symbols in one file used to collapse into a single node on insert
(``ON CONFLICT(qualified_name) DO UPDATE``), silently dropping the earlier ones,
and every caller resolved to the first definition. Identity is now derived from
the receiver/scope, with an occurrence ordinal only for genuinely identical
scopes, and calls resolve to the correct duplicate or are flagged ambiguous.
"""

from unittest.mock import patch

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.parser import CodeParser, NodeInfo
from code_review_graph.postprocessing import run_post_processing


def _build_file(store, path, text):
    parser = CodeParser()
    path.write_text(text)
    nodes, edges = parser.parse_file(path)
    store.store_file_nodes_edges(str(path), nodes, edges)
    return nodes, edges


def _calls(store):
    return store._conn.execute(
        "SELECT source_qualified, target_qualified, confidence_tier, extra "
        "FROM edges WHERE kind = 'CALLS'"
    ).fetchall()


def _tracked(names):
    return patch(
        "code_review_graph.incremental.get_all_tracked_files",
        return_value=list(names),
    )


class TestReceiverScopeIdentity:
    """Requirement 1: stable receiver/scope identity, not line numbers."""

    def test_object_literal_methods_qualify_by_receiver(self, tmp_path):
        store = GraphStore(tmp_path / "g.db")
        _build_file(
            store, tmp_path / "db.ts",
            "export const personas = { getById(id) { return id } };\n"
            "export const users = { getById(id) { return id } };\n",
        )
        keys = {
            r["qualified_name"]
            for r in store._conn.execute(
                "SELECT qualified_name FROM nodes WHERE name = 'getById'"
            )
        }
        assert keys == {
            f"{tmp_path / 'db.ts'}::personas.getById",
            f"{tmp_path / 'db.ts'}::users.getById",
        }
        # No line-number suffix anywhere: identity is scope-derived.
        assert not any(":L" in key for key in keys)
        store.close()

    def test_identity_survives_line_shift(self, tmp_path):
        store = GraphStore(tmp_path / "g.db")
        path = tmp_path / "db.ts"
        text = (
            "export const personas = { getById(id) { return id } };\n"
            "export const users = { getById(id) { return id } };\n"
        )
        _build_file(store, path, text)
        before = {r["qualified_name"] for r in store._conn.execute(
            "SELECT qualified_name FROM nodes WHERE name = 'getById'")}
        # Insert blank lines above: every line_start moves.
        _build_file(store, path, "\n\n\n" + text)
        after = {r["qualified_name"] for r in store._conn.execute(
            "SELECT qualified_name FROM nodes WHERE name = 'getById'")}
        assert before == after
        store.close()


class TestStructuralDiscriminator:
    """Requirement 1 (identical scope): duplicates persist via an ordinal."""

    def test_identical_module_functions_all_persist(self, tmp_path):
        store = GraphStore(tmp_path / "g.db")
        _build_file(
            store, tmp_path / "routeTree.gen.ts",
            "function rootRouteImport() { return 1 }\n"
            "function rootRouteImport() { return 2 }\n"
            "function rootRouteImport() { return 3 }\n",
        )
        keys = sorted(
            r["qualified_name"].rsplit("::", 1)[-1]
            for r in store._conn.execute(
                "SELECT qualified_name FROM nodes WHERE name = 'rootRouteImport'"
            )
        )
        assert keys == ["rootRouteImport", "rootRouteImport#1", "rootRouteImport#2"]
        store.close()


class TestUpsertConsistency:
    """Requirement 5: identity is a pure function of NodeInfo."""

    def _func(self, name, path, disambiguator=None):
        return NodeInfo(
            kind="Function", name=name, file_path=path,
            line_start=1, line_end=2, language="typescript",
            disambiguator=disambiguator,
        )

    def test_identical_nodeinfo_collapses(self, tmp_path):
        store = GraphStore(tmp_path / "g.db")
        path = str(tmp_path / "a.ts")
        store.upsert_node(self._func("helper", path))
        store.upsert_node(self._func("helper", path))
        assert store.get_stats().total_nodes == 1
        store.close()

    def test_distinct_disambiguator_persists_both(self, tmp_path):
        store = GraphStore(tmp_path / "g.db")
        path = str(tmp_path / "a.ts")
        store.upsert_node(self._func("helper", path))
        store.upsert_node(self._func("helper", path, disambiguator="1"))
        assert store.get_stats().total_nodes == 2
        store.close()


class TestCallerResolution:
    """Requirements 2 and 3: resolve to the right duplicate, else ambiguous."""

    def test_each_receiver_resolves_to_its_own_duplicate(self, tmp_path):
        store = GraphStore(tmp_path / "g.db")
        db = tmp_path / "db.ts"
        api = tmp_path / "api.ts"
        _build_file(
            store, db,
            "export const personas = { getById(id) { return id } };\n"
            "export const users = { getById(id) { return id } };\n",
        )
        _build_file(
            store, api,
            'import { personas, users } from "./db";\n'
            "export function handler() { return personas.getById(1); }\n"
            "export function other() { return users.getById(2); }\n",
        )
        run_post_processing(store)

        targets = {
            r["source_qualified"].rsplit("::", 1)[-1]: r["target_qualified"]
            for r in _calls(store)
        }
        assert targets["handler"] == f"{db}::personas.getById"
        assert targets["other"] == f"{db}::users.getById"
        # Adversarial: neither caller collapses onto the other's definition.
        assert targets["handler"] != targets["other"]
        store.close()

    def test_ambiguous_call_is_flagged_not_first(self, tmp_path):
        import json

        store = GraphStore(tmp_path / "g.db")
        gen = tmp_path / "gen.ts"
        _build_file(
            store, gen,
            "function helper() { return 1 }\n"
            "function helper() { return 2 }\n"
            "export function run() { return helper(); }\n",
        )
        run_post_processing(store)

        row = next(r for r in _calls(store)
                   if r["source_qualified"].endswith("::run"))
        # Not silently attached to the first definition.
        assert "::" not in row["target_qualified"]
        assert row["confidence_tier"] == "AMBIGUOUS"
        candidates = json.loads(row["extra"])["ambiguous_candidates"]
        assert sorted(c.rsplit("::", 1)[-1] for c in candidates) == [
            "helper", "helper#1",
        ]
        store.close()


class TestEndToEnd:
    """Requirement 6: full build + an incremental line shift."""

    def test_build_resolves_duplicates_and_survives_line_shift(self, tmp_path):
        (tmp_path / ".git").mkdir()
        db = tmp_path / "db.ts"
        api = tmp_path / "api.ts"
        db.write_text(
            "export const personas = { getById(id) { return id } };\n"
            "export const users = { getById(id) { return id } };\n"
        )
        api.write_text(
            'import { personas, users } from "./db";\n'
            "export function handler() { return personas.getById(1); }\n"
            "export function other() { return users.getById(2); }\n"
        )
        store = GraphStore(tmp_path / "g.db")
        with _tracked(["db.ts", "api.ts"]):
            full_build(tmp_path, store)
        run_post_processing(store)

        def caller_targets():
            return {
                r["source_qualified"].rsplit("::", 1)[-1]: r["target_qualified"]
                for r in _calls(store)
            }

        before = caller_targets()
        assert before["handler"] == f"{db}::personas.getById"
        assert before["other"] == f"{db}::users.getById"

        # Shift every line in db.ts; identities and caller edges must hold.
        db.write_text(
            "\n\n"
            "export const personas = { getById(id) { return id } };\n"
            "export const users = { getById(id) { return id } };\n"
        )
        with _tracked(["db.ts", "api.ts"]):
            incremental_update(tmp_path, store, changed_files=["db.ts"])
        run_post_processing(store)

        assert caller_targets() == before
        store.close()


class TestConnectedContainment:
    """Every CONTAINS source exists and every #N node is CONTAINS-reachable."""

    def _assert_no_dangling_contains(self, store):
        node_keys = {r["qualified_name"] for r in store._conn.execute(
            "SELECT qualified_name FROM nodes")}
        sources = {r["source_qualified"] for r in store._conn.execute(
            "SELECT source_qualified FROM edges WHERE kind = 'CONTAINS'")}
        assert sources <= node_keys

    def test_object_literal_binding_gets_container_node(self, tmp_path):
        store = GraphStore(tmp_path / "g.db")
        db = tmp_path / "db.ts"
        _build_file(
            store, db,
            "const personas = {\n"
            "  getById(id) { return id; },\n"
            "  list() { return []; },\n"
            "};\n",
        )
        binding = store._conn.execute(
            "SELECT kind, extra FROM nodes WHERE qualified_name = ?",
            (f"{db}::personas",),
        ).fetchone()
        assert binding is not None
        assert binding["kind"] == "Class"
        import json
        assert json.loads(binding["extra"])["object_literal"] is True
        # Connected chain: File CONTAINS personas CONTAINS personas.getById.
        contains = {
            (r["source_qualified"], r["target_qualified"])
            for r in store._conn.execute(
                "SELECT source_qualified, target_qualified FROM edges "
                "WHERE kind = 'CONTAINS'")
        }
        assert (str(db), f"{db}::personas") in contains
        assert (f"{db}::personas", f"{db}::personas.getById") in contains
        self._assert_no_dangling_contains(store)
        store.close()

    def test_ordinal_nodes_reachable_via_contains(self, tmp_path):
        store = GraphStore(tmp_path / "g.db")
        src = tmp_path / "a.ts"
        _build_file(
            store, src,
            "function setup() { return 1; }\n"
            "function setup() { return 2; }\n",
        )
        targets = [r["target_qualified"] for r in store._conn.execute(
            "SELECT target_qualified FROM edges WHERE kind = 'CONTAINS'")]
        assert sorted(targets) == [f"{src}::setup", f"{src}::setup#1"]
        self._assert_no_dangling_contains(store)
        store.close()

    def test_duplicate_bodies_own_their_outgoing_calls(self, tmp_path):
        store = GraphStore(tmp_path / "g.db")
        src = tmp_path / "a.ts"
        _build_file(
            store, src,
            "function setup() { return alpha(); }\n"
            "function setup() { return beta(); }\n"
            "function alpha() { return 1; }\n"
            "function beta() { return 2; }\n",
        )
        sources = {
            r["target_qualified"].rsplit("::", 1)[-1]:
                r["source_qualified"].rsplit("::", 1)[-1]
            for r in _calls(store)
        }
        assert sources["alpha"] == "setup"
        assert sources["beta"] == "setup#1"
        store.close()


class TestPythonSameNameIdioms:
    """Decorator-linked and conditional defs stay ONE followable symbol."""

    def _node_keys(self, store, name):
        return [r["qualified_name"].rsplit("::", 1)[-1]
                for r in store._conn.execute(
                    "SELECT qualified_name FROM nodes WHERE name = ?", (name,))]

    def test_property_setter_pair_is_one_node(self, tmp_path):
        store = GraphStore(tmp_path / "g.db")
        _build_file(
            store, tmp_path / "p1.py",
            "class Config:\n"
            "    @property\n"
            "    def value(self):\n"
            "        return self._v\n"
            "\n"
            "    @value.setter\n"
            "    def value(self, v):\n"
            "        self._v = v\n",
        )
        assert self._node_keys(store, "value") == ["Config.value"]
        store.close()

    def test_conditional_def_callers_stay_followable(self, tmp_path):
        store = GraphStore(tmp_path / "g.db")
        src = tmp_path / "p2.py"
        _build_file(
            store, src,
            "import sys\n"
            "if sys.version_info >= (3, 12):\n"
            "    def parse(x):\n"
            "        return 1\n"
            "else:\n"
            "    def parse(x):\n"
            "        return 2\n"
            "\n"
            "def main():\n"
            "    return parse(1)\n",
        )
        run_post_processing(store)
        assert self._node_keys(store, "parse") == ["parse"]
        row = next(r for r in _calls(store)
                   if r["source_qualified"].endswith("::main"))
        assert row["target_qualified"] == f"{src}::parse"
        assert row["confidence_tier"] == "EXTRACTED"
        store.close()

    def test_overload_stack_callers_stay_followable(self, tmp_path):
        store = GraphStore(tmp_path / "g.db")
        src = tmp_path / "p3.py"
        _build_file(
            store, src,
            "from typing import overload\n"
            "\n"
            "@overload\n"
            "def get(x: int) -> int: ...\n"
            "@overload\n"
            "def get(x: str) -> str: ...\n"
            "def get(x):\n"
            "    return x\n"
            "\n"
            "def main():\n"
            "    return get(1)\n",
        )
        run_post_processing(store)
        assert self._node_keys(store, "get") == ["get"]
        row = next(r for r in _calls(store)
                   if r["source_qualified"].endswith("::main"))
        assert row["target_qualified"] == f"{src}::get"
        assert row["confidence_tier"] == "EXTRACTED"
        store.close()


class TestNonTreeSitterPaths:
    """Every parse path flows through the identity choke point."""

    def test_vue_duplicate_setups_stay_distinct(self, tmp_path):
        store = GraphStore(tmp_path / "g.db")
        _build_file(
            store, tmp_path / "App.vue",
            "<script>\n"
            "function setup() { return 1; }\n"
            "function setup() { return 2; }\n"
            "</script>\n"
            "<template><div/></template>\n",
        )
        keys = sorted(r["qualified_name"].rsplit("::", 1)[-1]
                      for r in store._conn.execute(
                          "SELECT qualified_name FROM nodes "
                          "WHERE name = 'setup'"))
        assert keys == ["setup", "setup#1"]
        store.close()


class TestAmbiguityLifecycle:
    """Cross-file duplicate targets are flagged, and flags clear on resolve."""

    def _stale_setup(self, tmp_path):
        store = GraphStore(tmp_path / "g.db")
        lib = tmp_path / "lib.ts"
        caller = tmp_path / "caller.ts"
        _build_file(
            store, lib,
            "export function setup() { return 1; }\n"
            "export function setup() { return 2; }\n",
        )
        _build_file(
            store, caller,
            "import { setup } from './lib';\n"
            "function main() { return setup(); }\n",
        )
        store.resolve_bare_call_targets()
        return store, lib

    def test_cross_file_duplicate_target_downgraded(self, tmp_path):
        import json

        store, lib = self._stale_setup(tmp_path)
        row = next(r for r in _calls(store)
                   if r["source_qualified"].endswith("::main"))
        assert row["confidence_tier"] == "AMBIGUOUS"
        assert sorted(
            c.rsplit("::", 1)[-1]
            for c in json.loads(row["extra"])["ambiguous_candidates"]
        ) == ["setup", "setup#1"]
        store.close()

    def test_ambiguity_cleared_when_duplicate_removed(self, tmp_path):
        import json

        store, lib = self._stale_setup(tmp_path)
        _build_file(store, lib, "export function setup() { return 1; }\n")
        store.resolve_bare_call_targets()
        row = next(r for r in _calls(store)
                   if r["source_qualified"].endswith("::main"))
        assert row["confidence_tier"] == "EXTRACTED"
        assert row["target_qualified"] == f"{lib}::setup"
        assert "ambiguous_candidates" not in json.loads(row["extra"])
        store.close()


class TestReferencesToDuplicates:
    """REFERENCES to multiply-defined names resolve or are flagged, never dangle."""

    def test_reference_to_duplicated_name_is_flagged(self, tmp_path):
        store = GraphStore(tmp_path / "g.db")
        _build_file(
            store, tmp_path / "r.ts",
            "function handler() { return 1; }\n"
            "function handler() { return 2; }\n"
            "const DISPATCH = { h: handler };\n",
        )
        store.resolve_bare_call_targets()
        rows = store._conn.execute(
            "SELECT target_qualified, confidence_tier FROM edges "
            "WHERE kind = 'REFERENCES'"
        ).fetchall()
        assert rows
        for row in rows:
            resolved = "::" in row["target_qualified"]
            assert resolved or row["confidence_tier"] == "AMBIGUOUS"
        store.close()


class TestOrdinalSemantics:
    """Ordinals are line-independent but document-order ranked."""

    def _keys(self, tmp_path, text):
        parser = CodeParser()
        path = tmp_path / "a.ts"
        path.write_text(text)
        nodes, _ = parser.parse_file(path)
        return sorted(
            f"{n.name}#{n.disambiguator}" if n.disambiguator else n.name
            for n in nodes if n.kind != "File"
        )

    def test_reorder_keeps_key_set_stable(self, tmp_path):
        first = self._keys(
            tmp_path,
            "function setup() { return 1; }\nfunction setup() { return 2; }\n",
        )
        reordered = self._keys(
            tmp_path,
            "function setup() { return 2; }\nfunction setup() { return 1; }\n",
        )
        # Bodies may land under the other's ordinal, but the keys are stable.
        assert first == reordered == ["setup", "setup#1"]

    def test_deleting_first_renumbers_the_rest(self, tmp_path):
        assert self._keys(
            tmp_path, "function setup() { return 2; }\n",
        ) == ["setup"]


class TestSchemaMigrationForcesReingest:
    """v10 clears stored file hashes so pre-fix graphs re-ingest everything."""

    def test_old_schema_version_clears_file_hashes(self, tmp_path):
        db_path = tmp_path / "g.db"
        store = GraphStore(db_path)
        src = tmp_path / "a.ts"
        parser = CodeParser()
        src.write_text("function setup() { return 1; }\n")
        nodes, edges = parser.parse_file(src)
        store.store_file_nodes_edges(str(src), nodes, edges, fhash="deadbeef")
        # Simulate a database built before the identity change.
        store._conn.execute(
            "UPDATE metadata SET value = '9' WHERE key = 'schema_version'")
        store._conn.commit()
        store.close()

        store = GraphStore(db_path)
        hashes = {r["file_hash"] for r in store._conn.execute(
            "SELECT file_hash FROM nodes")}
        assert hashes == {""}
        store.close()


class TestExistingDatabaseMigration:
    """Requirement 4: a rebuild migrates old collapsed keys and embeddings."""

    def test_rebuild_replaces_collapsed_node_and_purges_embedding(self, tmp_path):
        from code_review_graph.embeddings import EmbeddingStore

        db_path = tmp_path / "g.db"
        store = GraphStore(db_path)
        src = tmp_path / "db.ts"

        # Simulate a pre-fix database: one collapsed node under the bare key.
        collapsed = NodeInfo(
            kind="Function", name="getById", file_path=str(src),
            line_start=1, line_end=1, language="typescript",
        )
        store.upsert_node(collapsed)
        stale_key = f"{src}::getById"

        with patch("code_review_graph.embeddings.get_provider", return_value=None):
            embeddings = EmbeddingStore(db_path)
            embeddings._conn.execute(
                "INSERT INTO embeddings (qualified_name, vector, text_hash, provider)"
                " VALUES (?, ?, '', 'test')",
                (stale_key, b"\x00\x00\x00\x00"),
            )
            embeddings._conn.commit()

            _build_file(
                store, src,
                "export const personas = { getById(id) { return id } };\n"
                "export const users = { getById(id) { return id } };\n",
            )

            assert store.get_node(stale_key) is None
            assert store.get_node(f"{src}::personas.getById") is not None
            # The orphaned embedding for the old key is reclaimed.
            assert embeddings.purge_orphans() == 1
            embeddings.close()
        store.close()
