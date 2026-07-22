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
