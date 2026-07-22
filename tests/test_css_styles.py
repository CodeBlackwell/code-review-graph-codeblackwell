"""Tests for CSS Modules parsing and import-scoped STYLES linking.

Mirrors the adversarial cases that must hold: multi-file same-class isolation,
incremental rename correctness, scoped SFC styles, repeated selectors, a bounded
cross-file join, and honest import resolution.
"""

from __future__ import annotations

import json

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path, incremental_update
from code_review_graph.parser import CodeParser
from code_review_graph.tools.query import query_graph


def _build(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    store = GraphStore(tmp_path / "graph.db")
    stats = full_build(tmp_path, store)
    return store, stats


def _styles_edges(store):
    return list(store._conn.execute(
        "SELECT source_qualified, target_qualified, extra FROM edges WHERE kind = 'STYLES'"
    ).fetchall())


def _selector_nodes(store):
    rows = store._conn.execute(
        "SELECT name, file_path, extra FROM nodes "
        "WHERE kind = 'Class' AND extra LIKE '%\"css_kind\": \"selector\"%'"
    ).fetchall()
    return [(r["name"], r["file_path"], json.loads(r["extra"])) for r in rows]


# --- Parser-level -----------------------------------------------------------

def test_scope_module_vs_global(tmp_path):
    (tmp_path / "a.module.css").write_text(".btn { color: red; }\n")
    (tmp_path / "b.css").write_text(".btn { color: red; }\n")
    parser = CodeParser(tmp_path)
    mod = parser.parse_file(tmp_path / "a.module.css")[0]
    glob = parser.parse_file(tmp_path / "b.css")[0]
    assert [n.extra["scope"] for n in mod if n.extra.get("css_kind")] == ["module"]
    assert [n.extra["scope"] for n in glob if n.extra.get("css_kind")] == ["global"]


def test_repeated_selector_distinct_nodes(tmp_path, monkeypatch):
    # T5: repeated selectors stay distinct — no identity collapse, no self-edges.
    (tmp_path / "a.module.css").write_text(
        ".btn { color: red; }\n.btn { padding: 4px; }\n"
    )
    store, _ = _build(tmp_path, monkeypatch)
    names = sorted(n for n, _, e in _selector_nodes(store) if e["selector"] == ".btn")
    assert names == [".btn", ".btn#1"]
    overrides = store._conn.execute(
        "SELECT COUNT(*) c FROM edges WHERE kind IN ('OVERRIDES', 'POTENTIAL_CONFLICT')"
    ).fetchone()["c"]
    assert overrides == 0


def test_scss_partial_import_resolves(tmp_path, monkeypatch):
    # T7: @use / @import resolve to real partial nodes; nothing dangling.
    (tmp_path / "_variables.scss").write_text("$x: 1;\n")
    (tmp_path / "main.scss").write_text('@use "variables";\n.btn { color: red; }\n')
    store, _ = _build(tmp_path, monkeypatch)
    targets = [
        r["target_qualified"] for r in store._conn.execute(
            "SELECT target_qualified FROM edges WHERE kind = 'IMPORTS_FROM'"
        ).fetchall()
    ]
    assert any(t.endswith("_variables.scss") for t in targets)
    # No dangling: every import target is a real node.
    for t in targets:
        assert store.get_node(t) is not None


def test_unresolvable_import_produces_no_edge(tmp_path, monkeypatch):
    # T8: URL / package imports are left unresolved rather than dangling.
    (tmp_path / "real.css").write_text(".x { color: red; }\n")
    (tmp_path / "main.css").write_text(
        '@import "https://cdn.example.com/x.css";\n@import "./real.css";\n'
    )
    store, _ = _build(tmp_path, monkeypatch)
    targets = [
        r["target_qualified"] for r in store._conn.execute(
            "SELECT target_qualified FROM edges WHERE kind = 'IMPORTS_FROM'"
        ).fetchall()
    ]
    assert any(t.endswith("real.css") for t in targets)
    assert not any("cdn.example.com" in t for t in targets)


def test_scss_ampersand_nesting_resolves_parent(tmp_path):
    # 6a: `&` resolves against the parent selector; plain nested rules do not.
    (tmp_path / "a.scss").write_text(
        ".btn {\n  color: red;\n  &:hover { color: blue; }\n"
        "  &.active { color: green; }\n  .icon { width: 1em; }\n}\n"
    )
    parser = CodeParser(tmp_path)
    nodes, _ = parser.parse_file(tmp_path / "a.scss")
    selectors = sorted(n.extra["selector"] for n in nodes if n.extra.get("css_kind"))
    assert selectors == [".btn", ".btn.active", ".btn:hover", ".icon"]


def test_sfc_style_selector_line_numbers(tmp_path):
    # 6b: selector lines are offset by the <style> block's position in the SFC.
    (tmp_path / "W.vue").write_text(
        "<template>\n  <button class=\"btn\">x</button>\n</template>\n"
        "<style scoped>\n.btn { color: red; }\n\n.other { color: blue; }\n</style>\n"
    )
    parser = CodeParser(tmp_path)
    nodes, _ = parser.parse_file(tmp_path / "W.vue")
    lines = {
        n.extra["selector"]: n.line_start for n in nodes if n.extra.get("css_kind")
    }
    assert lines == {".btn": 5, ".other": 7}


# --- Linking ----------------------------------------------------------------

def test_multi_file_same_class_isolated(tmp_path, monkeypatch):
    # T1: identical class name in two modules — each component links only to
    # the file it imported.
    (tmp_path / "A.module.css").write_text(".btn-primary { color: red; }\n")
    (tmp_path / "B.module.css").write_text(".btn-primary { color: blue; }\n")
    (tmp_path / "CompA.tsx").write_text(
        "import styles from './A.module.css';\n"
        "export function CompA() { return <button className={styles.btnPrimary}>A</button>; }\n"
    )
    (tmp_path / "CompB.tsx").write_text(
        "import styles from './B.module.css';\n"
        "export function CompB() { return <button className={styles.btnPrimary}>B</button>; }\n"
    )
    store, stats = _build(tmp_path, monkeypatch)
    assert stats["css_resolution"]["styles_edges"] == 2
    for src, tgt, _ in _styles_edges(store):
        # CompA links only to A.module.css, CompB only to B.module.css.
        expected = "A.module.css" if "CompA" in src else "B.module.css"
        assert expected in tgt


def test_unresolved_module_import_no_edge(tmp_path, monkeypatch):
    # T3: a styles.* reference whose import does not resolve gets no edge.
    (tmp_path / "Comp.tsx").write_text(
        "import styles from './missing.module.css';\n"
        "export function Comp() { return <div className={styles.card}>x</div>; }\n"
    )
    store, stats = _build(tmp_path, monkeypatch)
    assert stats["css_resolution"]["styles_edges"] == 0


def test_camel_to_kebab_and_raw_match(tmp_path, monkeypatch):
    # Kebab fallback fires only when the exact spelling has no selector.
    (tmp_path / "s.module.css").write_text(".btn-primary { color: red; }\n")
    (tmp_path / "C.tsx").write_text(
        "import styles from './s.module.css';\n"
        "export function C() { return <button className={styles.btnPrimary}>x</button>; }\n"
    )
    store, stats = _build(tmp_path, monkeypatch)
    assert stats["css_resolution"]["styles_edges"] == 1
    assert _styles_edges(store)[0][1].endswith("::.btn-primary")


def test_exact_match_wins_over_kebab_fallback(tmp_path, monkeypatch):
    # S6: a module defining BOTH .btnPrimary and .btn-primary yields exactly
    # one edge, to the exact spelling.
    (tmp_path / "s.module.css").write_text(
        ".btnPrimary { color: red; }\n.btn-primary { color: blue; }\n"
    )
    (tmp_path / "C.tsx").write_text(
        "import styles from './s.module.css';\n"
        "export function C() { return <button className={styles.btnPrimary}>x</button>; }\n"
    )
    store, stats = _build(tmp_path, monkeypatch)
    assert stats["css_resolution"]["styles_edges"] == 1
    assert _styles_edges(store)[0][1].endswith("::.btnPrimary")


def test_scale_bounded_join(tmp_path, monkeypatch):
    # T6: N components each importing their own module => O(N) edges, not O(N^2).
    n = 40
    for i in range(n):
        (tmp_path / f"m{i}.module.css").write_text(".shared { color: red; }\n")
        (tmp_path / f"C{i}.tsx").write_text(
            f"import styles from './m{i}.module.css';\n"
            f"export function C{i}() {{ return <div className={{styles.shared}}>x</div>; }}\n"
        )
    store, stats = _build(tmp_path, monkeypatch)
    assert stats["css_resolution"]["styles_edges"] == n


def test_incremental_rename_repoints(tmp_path, monkeypatch):
    # T2: renaming the imported module repoints STYLES and leaves nothing stale.
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    (tmp_path / "old.module.css").write_text(".card { color: red; }\n")
    (tmp_path / "other.module.css").write_text(".card { color: blue; }\n")
    (tmp_path / "Comp.tsx").write_text(
        "import styles from './old.module.css';\n"
        "export function Comp() { return <div className={styles.card}>x</div>; }\n"
    )
    store = GraphStore(tmp_path / "graph.db")
    full_build(tmp_path, store)
    assert _styles_edges(store)[0][1].endswith("old.module.css::.card")

    # Rename the stylesheet and update the import to point at the new file.
    (tmp_path / "old.module.css").unlink()
    (tmp_path / "new.module.css").write_text(".card { color: red; }\n")
    (tmp_path / "Comp.tsx").write_text(
        "import styles from './new.module.css';\n"
        "export function Comp() { return <div className={styles.card}>x</div>; }\n"
    )
    store.remove_file_data(str(tmp_path / "old.module.css"))
    incremental_update(
        tmp_path, store,
        changed_files=["new.module.css", "Comp.tsx"],
    )
    edges = _styles_edges(store)
    assert len(edges) == 1
    assert edges[0][1].endswith("new.module.css::.card")
    assert not any("old.module.css" in t for _, t, _ in edges)


def test_param_shadowed_styles_no_edge(tmp_path, monkeypatch):
    # S4a: a function parameter named ``styles`` shadows the import.
    (tmp_path / "s.module.css").write_text(".foo { color: red; }\n")
    (tmp_path / "Comp.tsx").write_text(
        "import styles from './s.module.css';\n"
        "export function Comp({ styles }: any) { return <div className={styles.foo}>x</div>; }\n"
    )
    store, stats = _build(tmp_path, monkeypatch)
    assert stats["css_resolution"]["styles_edges"] == 0


def test_local_const_shadowed_styles_no_edge(tmp_path, monkeypatch):
    # S4b: a local ``const styles = ...`` shadows the import.
    (tmp_path / "s.module.css").write_text(".foo { color: red; }\n")
    (tmp_path / "Comp.tsx").write_text(
        "import styles from './s.module.css';\n"
        "export function Comp() { const styles = useTheme(); "
        "return <div className={styles.foo}>x</div>; }\n"
    )
    store, stats = _build(tmp_path, monkeypatch)
    assert stats["css_resolution"]["styles_edges"] == 0


def test_bare_arrow_param_shadow_no_edge(tmp_path, monkeypatch):
    # R6: unparenthesized arrow param uses the singular `parameter` field.
    (tmp_path / "s.module.css").write_text(".foo { color: red; }\n")
    (tmp_path / "C.tsx").write_text(
        "import styles from './s.module.css';\n"
        "export const C = () => ['a'].map(styles => <div className={styles.foo}>x</div>);\n"
    )
    store, stats = _build(tmp_path, monkeypatch)
    assert stats["css_resolution"]["styles_edges"] == 0


def test_for_of_binding_shadow_no_edge(tmp_path, monkeypatch):
    # R7: `for (const styles of themes)` binds inside the loop body.
    (tmp_path / "s.module.css").write_text(".foo { color: red; }\n")
    (tmp_path / "C.tsx").write_text(
        "import styles from './s.module.css';\n"
        "export function C({themes}: any) {\n"
        "  for (const styles of themes) { return <div className={styles.foo}>x</div>; }\n"
        "  return null;\n}\n"
    )
    store, stats = _build(tmp_path, monkeypatch)
    assert stats["css_resolution"]["styles_edges"] == 0


def test_catch_param_shadow_no_edge(tmp_path, monkeypatch):
    # R10: `catch (styles)` binds inside the catch block.
    (tmp_path / "s.module.css").write_text(".foo { color: red; }\n")
    (tmp_path / "C.tsx").write_text(
        "import styles from './s.module.css';\n"
        "export function C() { try { return null; } catch (styles) "
        "{ return <div className={styles.foo}>x</div>; } }\n"
    )
    store, stats = _build(tmp_path, monkeypatch)
    assert stats["css_resolution"]["styles_edges"] == 0


def test_param_default_value_does_not_shadow(tmp_path, monkeypatch):
    # R8: `styles` inside a destructured param's DEFAULT VALUE is a use of the
    # import, not a binding — refs in the function must still link.
    (tmp_path / "s.module.css").write_text(".root { color: red; }\n.inner { color: blue; }\n")
    (tmp_path / "C.tsx").write_text(
        "import styles from './s.module.css';\n"
        "export function C({ className = styles.root }: any) {\n"
        "  return <div className={styles.inner}>x</div>;\n}\n"
    )
    store, stats = _build(tmp_path, monkeypatch)
    assert stats["css_resolution"]["styles_edges"] == 1
    assert _styles_edges(store)[0][1].endswith("::.inner")


def test_type_annotation_mention_does_not_shadow(tmp_path, monkeypatch):
    # R15: `keyof typeof styles` in a TS type annotation is not a binding.
    (tmp_path / "s.module.css").write_text(".inner { color: blue; }\n")
    (tmp_path / "C.tsx").write_text(
        "import styles from './s.module.css';\n"
        "export function C(props: { k: keyof typeof styles }) "
        "{ return <div className={styles.inner}>x</div>; }\n"
    )
    store, stats = _build(tmp_path, monkeypatch)
    assert stats["css_resolution"]["styles_edges"] == 1
    assert _styles_edges(store)[0][1].endswith("::.inner")


def test_incremental_heal_stylesheet_created_after_component(tmp_path, monkeypatch):
    # S1: component indexed while its stylesheet is missing; the stylesheet
    # appearing later must link on an incremental update of the CSS alone.
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    (tmp_path / "Comp.tsx").write_text(
        "import styles from './s.module.css';\n"
        "export function Comp() { return <div className={styles.card}>x</div>; }\n"
    )
    store = GraphStore(tmp_path / "graph.db")
    stats = full_build(tmp_path, store)
    assert stats["css_resolution"]["styles_edges"] == 0

    (tmp_path / "s.module.css").write_text(".card { color: red; }\n")
    stats = incremental_update(tmp_path, store, changed_files=["s.module.css"])
    assert stats["css_resolution"]["styles_edges"] == 1
    edges = _styles_edges(store)
    assert len(edges) == 1
    assert edges[0]["target_qualified"].endswith("s.module.css::.card")


def test_incremental_deletion_removes_selectors_and_edges(tmp_path, monkeypatch):
    # 6c: deleting the stylesheet (import kept) through incremental's real
    # missing-file path removes both selector nodes and STYLES edges.
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    (tmp_path / "s.module.css").write_text(".card { color: red; }\n")
    (tmp_path / "Comp.tsx").write_text(
        "import styles from './s.module.css';\n"
        "export function Comp() { return <div className={styles.card}>x</div>; }\n"
    )
    store = GraphStore(tmp_path / "graph.db")
    stats = full_build(tmp_path, store)
    assert stats["css_resolution"]["styles_edges"] == 1

    (tmp_path / "s.module.css").unlink()
    stats = incremental_update(tmp_path, store, changed_files=["s.module.css"])
    assert stats["css_resolution"]["styles_edges"] == 0
    assert _styles_edges(store) == []
    assert _selector_nodes(store) == []


def test_scoped_sfc_no_conflict(tmp_path, monkeypatch):
    # T4: scoped Vue/Svelte styles are tagged scoped and never produce conflicts.
    (tmp_path / "W.vue").write_text(
        "<template><button class=\"btn\">x</button></template>\n"
        "<style scoped>\n.btn { color: red; }\n</style>\n"
    )
    (tmp_path / "T.svelte").write_text(
        "<button class=\"btn\">x</button>\n<style>\n.btn { color: blue; }\n</style>\n"
    )
    store, _ = _build(tmp_path, monkeypatch)
    scopes = {e["scope"] for _, _, e in _selector_nodes(store)}
    assert scopes == {"scoped"}
    conflicts = store._conn.execute(
        "SELECT COUNT(*) c FROM edges WHERE kind = 'POTENTIAL_CONFLICT'"
    ).fetchone()["c"]
    assert conflicts == 0


def test_sfc_indented_sass_not_parsed_as_scss(tmp_path, monkeypatch):
    # lang="sass" is indented Sass, not SCSS: skip it rather than mis-parse.
    (tmp_path / "W.vue").write_text(
        "<template><button class=\"btn\">x</button></template>\n"
        "<style lang=\"sass\">\n.btn\n  color: red\n</style>\n"
    )
    store, _ = _build(tmp_path, monkeypatch)
    assert _selector_nodes(store) == []


def test_styles_of_and_styled_by_queries(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    (tmp_path / "s.module.css").write_text(".card { color: red; }\n")
    (tmp_path / "C.tsx").write_text(
        "import styles from './s.module.css';\n"
        "export function C() { return <div className={styles.card}>x</div>; }\n"
    )
    db_path = get_db_path(tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with GraphStore(db_path) as store:
        full_build(tmp_path, store)

    styles_of = query_graph("styles_of", str(tmp_path / "C.tsx") + "::C",
                            repo_root=str(tmp_path))
    assert styles_of["status"] == "ok"
    assert [r["name"] for r in styles_of["results"]] == [".card"]

    styled_by = query_graph("styled_by", str(tmp_path / "s.module.css") + "::.card",
                            repo_root=str(tmp_path))
    assert styled_by["status"] == "ok"
    assert [r["name"] for r in styled_by["results"]] == ["C"]
