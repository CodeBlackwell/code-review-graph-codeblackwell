"""Post-build CSS Modules linker.

Creates STYLES edges from components to the CSS selectors that style them,
restricted to the stylesheet actually imported. Each ``className={styles.foo}``
reference is resolved through the importing file's ``css_module_imports`` map to
one file, so a reference can only ever link to selectors defined there — never
to a same-named class elsewhere in the repository.

Only CSS Modules are linked. Global stylesheets carry no import to scope them,
and scoped Vue/Svelte styles are component-local; both are parsed into the graph
but left unlinked here.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)

# A single simple class selector (``.btn``), the only shape a CSS Module maps to.
_SIMPLE_CLASS = re.compile(r"^\.[A-Za-z_][\w-]*$")


def _camel_to_kebab(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"-\1", name).lower()


def resolve_css_styles(store: GraphStore) -> dict:
    """Link component CSS Module references to their imported selectors.

    Safe to call repeatedly: rebuilds all STYLES edges from scratch.
    Returns a dict with the STYLES edge count for telemetry.
    """
    from .parser import EdgeInfo

    conn = store._conn
    conn.execute("DELETE FROM edges WHERE kind = 'STYLES'")

    # Node file paths are stored unresolved while resolved imports are realpaths;
    # normalize both sides through this cache so symlinks (e.g. /tmp) still match.
    realpath_cache: dict[str, str] = {}

    def norm(path: str) -> str:
        cached = realpath_cache.get(path)
        if cached is None:
            cached = os.path.realpath(path)
            realpath_cache[path] = cached
        return cached

    # (realpath, bare_class) -> [selector qualified_name]
    index: dict[tuple[str, str], list[str]] = {}
    for row in conn.execute(
        "SELECT qualified_name, file_path, extra FROM nodes "
        "WHERE kind = 'Class' AND extra LIKE '%\"css_kind\": \"selector\"%'"
    ).fetchall():
        try:
            extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        selector = extra.get("selector", "")
        if not _SIMPLE_CLASS.match(selector):
            continue
        bare = selector.lstrip(".")
        index.setdefault((norm(row["file_path"]), bare), []).append(
            row["qualified_name"],
        )

    if not index:
        conn.commit()
        return {"styles_edges": 0}

    # file_path -> {import_name: resolved_stylesheet_path}
    file_imports: dict[str, dict[str, str]] = {}
    for row in conn.execute(
        "SELECT file_path, extra FROM nodes "
        "WHERE kind = 'File' AND extra LIKE '%css_module_imports%'"
    ).fetchall():
        try:
            file_imports[row["file_path"]] = json.loads(
                row["extra"] or "{}"
            ).get("css_module_imports", {})
        except (json.JSONDecodeError, TypeError):
            continue

    count = 0
    for row in conn.execute(
        "SELECT qualified_name, file_path, line_start, extra FROM nodes "
        "WHERE extra LIKE '%css_module_refs%'"
    ).fetchall():
        try:
            extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        refs = extra.get("css_module_refs", [])
        imports = file_imports.get(row["file_path"], {})
        for ref in refs:
            imp_name = ref.get("import", "")
            prop = ref.get("property", "")
            resolved_file = imports.get(imp_name)
            if not resolved_file or not prop:
                continue
            resolved_file = norm(resolved_file)
            seen: set[str] = set()
            for bare in (prop, _camel_to_kebab(prop)):
                for target in index.get((resolved_file, bare), ()):
                    if target in seen:
                        continue
                    seen.add(target)
                    store.upsert_edge(EdgeInfo(
                        kind="STYLES",
                        source=row["qualified_name"],
                        target=target,
                        file_path=row["file_path"],
                        line=row["line_start"] or 0,
                        extra={
                            "resolution": "css_module",
                            "property": prop,
                            "class_name": bare,
                        },
                    ))
                    count += 1

    conn.commit()
    logger.info("CSS resolver: %d STYLES edges", count)
    return {"styles_edges": count}
