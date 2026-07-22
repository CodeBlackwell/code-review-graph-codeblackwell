"""Post-build CSS Modules linker.

Creates STYLES edges from components to the CSS selectors that style them,
restricted to the stylesheet actually imported. The parser records each CSS
Module import as its RAW import string (``css_module_imports``); this pass
resolves that string to a stylesheet file, so a reference can only ever link
to selectors defined there — never to a same-named class elsewhere in the
repository. Resolving here (not at parse time) means a stylesheet that appears
or is renamed after the component was indexed links on the next update, since
this pass re-runs whenever any CSS-relevant file changes. An import that does
not resolve produces no edge.

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
    from pathlib import Path

    from .graph import GraphStore

logger = logging.getLogger(__name__)

# A single simple class selector (``.btn``), the only shape a CSS Module maps to.
_SIMPLE_CLASS = re.compile(r"^\.[A-Za-z_][\w-]*$")


def _camel_to_kebab(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"-\1", name).lower()


def resolve_css_styles(store: GraphStore, repo_root: Path) -> dict:
    """Link component CSS Module references to their imported selectors.

    Safe to call repeatedly: rebuilds all STYLES edges from scratch.
    Returns a dict with the STYLES edge count for telemetry.
    """
    from .parser import CodeParser, EdgeInfo

    parser = CodeParser(repo_root)

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

    # file_path -> {import_name: resolved stylesheet path}. Imports are stored
    # as raw strings and resolved here, each run; unresolvable ones are dropped
    # so linking can never fall back to a repository-wide join.
    file_imports: dict[str, dict[str, str]] = {}
    for row in conn.execute(
        "SELECT file_path, language, extra FROM nodes "
        "WHERE kind = 'File' AND extra LIKE '%css_module_imports%'"
    ).fetchall():
        try:
            raw_imports = json.loads(
                row["extra"] or "{}"
            ).get("css_module_imports", {})
        except (json.JSONDecodeError, TypeError):
            continue
        resolved_imports = {}
        for name, module in raw_imports.items():
            resolved = parser._resolve_module_to_file(
                module, row["file_path"], row["language"] or "typescript",
            )
            if resolved:
                resolved_imports[name] = resolved
        if resolved_imports:
            file_imports[row["file_path"]] = resolved_imports

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
