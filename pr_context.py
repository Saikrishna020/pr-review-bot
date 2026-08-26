"""Fetches exactly the files a PR diff needs from GitHub, on demand, and
assembles deterministic review context from them — same-file structure,
direct imports, and (via GitHub code search) callers/subclasses elsewhere in
the repo. No local checkout, nothing cached across requests: every review
starts from a clean slate and fetches only what this specific diff touches.

Trade-off worth knowing: "callers elsewhere" relies on GitHub's code search,
which only indexes the default branch and isn't guaranteed to be fully
fresh, and is rate-limited (~10 requests/minute authenticated). So this is
capped to the first few changed symbols per review and the first few search
hits per symbol; any search/fetch/parse failure just drops that piece of
context rather than failing the review — see `main.py`'s use of this.

Every fetch here is a GitHub API round-trip, and a single diff can need
dozens of them (imports, then a search + a fetch-to-confirm per changed
symbol). Doing them one at a time made a real diff take minutes, so they're
run through a shared thread pool in flat, independent batches — fetch every
changed file at once, then resolve every import at once, then search for
every changed symbol at once, then confirm every search hit at once. Each
round is submitted from the main thread only (no task submits more tasks to
the same pool), which keeps this simple and avoids the classic thread-pool
deadlock where nested submissions starve each other for workers.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from code_graph import Import, ParsedFile, Symbol, parse_python_source, resolve_python_import_paths
from fetch_real_pr_diff import get_file_content, search_code

log = logging.getLogger("uvicorn.error")

HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

DEFAULT_BUDGET_CHARS = 12_000
MAX_CHANGED_SYMBOLS = 5    # how many changed functions/classes we search callers for
MAX_SEARCH_CANDIDATES = 8  # how many search hits we fetch+confirm per symbol
MAX_WORKERS = 16           # concurrent GitHub API calls


@dataclass
class RepoContext:
    """Assembled context plus whether it is knowingly incomplete.

    `truncated` is true when any cap was hit — the changed-symbol cap, a
    per-symbol search cap, or the character budget. The prose in `text` says
    so too (for the model to read), but callers need it as a flag: Phase 3
    surfaces it on the posted comment and uses it to stop the reviewer from
    claiming a confident "satisfies" off a partial view of the codebase.
    """

    text: str
    truncated: bool = False

    def __bool__(self) -> bool:
        return bool(self.text)


@dataclass
class _DiffFile:
    path: str
    changed_lines: set[int] = field(default_factory=set)


def parse_changed_files(diff: str) -> list[_DiffFile]:
    """Extracts, per file touched by the diff, the set of line numbers changed
    in the new-file version (i.e. added/modified lines), from hunk headers.
    """
    files: dict[str, _DiffFile] = {}
    current: _DiffFile | None = None
    new_line = 0

    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path == "/dev/null":
                current = None
                continue
            if path.startswith("b/"):
                path = path[2:]
            current = files.setdefault(path, _DiffFile(path=path))
            continue

        if current is None:
            continue

        hunk_match = HUNK_HEADER_RE.match(line)
        if hunk_match:
            new_line = int(hunk_match.group(1))
            continue

        if line.startswith("+") and not line.startswith("+++"):
            current.changed_lines.add(new_line)
            new_line += 1
        elif line.startswith("-") or line.startswith("\\"):
            pass  # old-file-only line, or a "\ No newline at end of file" marker
        else:
            # A context line — normally starts with a space, but a blank source
            # line can arrive as a genuinely empty string if trailing whitespace
            # got stripped somewhere upstream. Either way it's still one line.
            new_line += 1

    return list(files.values())


def _fetch_and_parse(owner: str, repo: str, path: str, ref: str | None) -> ParsedFile | None:
    if not path.endswith(".py"):
        return None
    source = get_file_content(owner, repo, path, ref=ref)
    if source is None:
        return None
    try:
        return parse_python_source(source)
    except Exception:
        log.warning("pr_context: failed to parse %s, skipping", path, exc_info=True)
        return None


def _same_file_block(path: str, parsed: ParsedFile, changed_starts: set[int]) -> str:
    lines = [f"### {path}"]
    for symbol in sorted(parsed.symbols, key=lambda s: s.start_line):
        marker = " (changed in this PR)" if symbol.start_line in changed_starts else ""
        lines.append(f"- {symbol.kind} `{symbol.name}` (lines {symbol.start_line}-{symbol.end_line}){marker}")
    return "\n".join(lines)


def _resolve_import(owner: str, repo: str, path: str, imp: Import) -> str | None:
    """Tries `imp`'s candidate paths in turn (usually 1-2 tries) and formats a
    one-line summary of the first one that actually exists. Sequential inside
    one import, but called as an independent leaf task per import so that
    different imports (and different files' imports) still run concurrently.
    """
    for candidate in resolve_python_import_paths(imp, path):
        source = get_file_content(owner, repo, candidate, ref=None)
        if source is None:
            continue
        try:
            target = parse_python_source(source)
        except Exception:
            return None
        names = ", ".join(s.name for s in target.symbols[:12]) or "(no top-level functions/classes found)"
        return f"- {candidate}: {names}"
    return None


def _confirm_usage(owner: str, repo: str, symbol: Symbol, symbol_file: str, candidate_path: str) -> tuple[str, list[int], bool] | None:
    """Fetches `candidate_path` and checks whether it really references
    `symbol.name` (search can return false positives — a comment, a string, an
    unrelated same-named variable) and, if so, whether it imports `symbol_file`
    (-> confirmed) or not (-> possible).
    """
    source = get_file_content(owner, repo, candidate_path, ref=None)
    if source is None:
        return None
    try:
        parsed = parse_python_source(source)
    except Exception:
        return None

    if symbol.kind == "function":
        lines = [c.line for c in parsed.calls if c.name == symbol.name]
    else:
        lines = [s.start_line for s in parsed.symbols if s.kind == "class" and symbol.name in s.bases]
    if not lines:
        return None  # search hit didn't actually contain a real reference

    imports_target = any(
        candidate == symbol_file
        for imp in parsed.imports
        for candidate in resolve_python_import_paths(imp, candidate_path)
    )
    return candidate_path, lines, imports_target


def find_references(owner: str, repo: str, symbol: Symbol, symbol_file: str, pool: ThreadPoolExecutor | None = None) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Finds real (parsed, not just text-matched) references to `symbol`
    elsewhere in the repo via GitHub code search, split into (confirmed,
    possible) — see module docstring for what that split means. This is the
    piece the golden-set eval (`eval/eval_context_retrieval.py`) checks.
    Runs its own short-lived pool if `pool` isn't supplied (e.g. from eval code
    calling this directly); `build_context` passes its shared one instead.
    """
    candidates = [p for p in search_code(owner, repo, symbol.name) if p != symbol_file][:MAX_SEARCH_CANDIDATES]
    if not candidates:
        return [], []

    def confirm_all(executor: ThreadPoolExecutor):
        results = executor.map(lambda path: _confirm_usage(owner, repo, symbol, symbol_file, path), candidates)
        confirmed: list[tuple[str, int]] = []
        possible: list[tuple[str, int]] = []
        for result in results:
            if result is None:
                continue
            path, lines, is_confirmed = result
            bucket = confirmed if is_confirmed else possible
            bucket.extend((path, line) for line in lines)
        return confirmed, possible

    if pool is not None:
        return confirm_all(pool)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as owned_pool:
        return confirm_all(owned_pool)


def _format_references_block(symbol: Symbol, symbol_file: str, confirmed: list[tuple[str, int]], possible: list[tuple[str, int]]) -> str | None:
    if not confirmed and not possible:
        return None
    label = "Callers of" if symbol.kind == "function" else "Subclasses of"
    out = [f"### {label} `{symbol.name}` ({symbol_file}:{symbol.start_line})"]
    for path, line in confirmed:
        out.append(f"- {path}:{line}")
    for path, line in possible:
        out.append(f"- {path}:{line} (possible match by name only, import not confirmed)")
    return "\n".join(out)


def build_context(owner: str, repo: str, diff: str, head_sha: str, budget_chars: int = DEFAULT_BUDGET_CHARS) -> RepoContext:
    """Assembles deterministic repo context for `diff` by fetching only the
    files it touches (at `head_sha`) plus, for the changed symbols, whatever
    GitHub code search turns up elsewhere in the repo. Returns an empty
    RepoContext if there's nothing usable (e.g. only non-Python files changed).
    """
    diff_files = [f for f in parse_changed_files(diff) if f.path.endswith(".py")]
    if not diff_files:
        return RepoContext(text="")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        # Round 1: fetch + parse every changed file, concurrently.
        parsed_files = list(pool.map(lambda f: _fetch_and_parse(owner, repo, f.path, head_sha), diff_files))

        same_file_blocks: list[str] = []
        changed_symbols: list[tuple[Symbol, str]] = []
        import_tasks: list[tuple[str, Import]] = []

        for diff_file, parsed in zip(diff_files, parsed_files):
            if parsed is None:
                continue

            changed_starts: set[int] = set()
            for line in diff_file.changed_lines:
                symbol = parsed.symbol_at(line)
                if symbol is not None and symbol.start_line not in changed_starts:
                    changed_starts.add(symbol.start_line)
                    changed_symbols.append((symbol, diff_file.path))

            same_file_blocks.append(_same_file_block(diff_file.path, parsed, changed_starts))
            import_tasks.extend((diff_file.path, imp) for imp in parsed.imports)

        # Round 2: resolve every import across every changed file, concurrently.
        import_lines = pool.map(lambda task: _resolve_import(owner, repo, task[0], task[1]), import_tasks)
        imports_by_file: dict[str, list[str]] = {}
        for (importing_file, _imp), line in zip(import_tasks, import_lines):
            if line is not None:
                imports_by_file.setdefault(importing_file, []).append(line)
        import_blocks = [
            f"### Imports of {path}\n" + "\n".join(lines)
            for path, lines in imports_by_file.items()
        ]

        # Round 3: one code search per changed symbol (capped), concurrently.
        symbols_to_search = changed_symbols[:MAX_CHANGED_SYMBOLS]
        skipped_symbols = changed_symbols[MAX_CHANGED_SYMBOLS:]

        def _search_symbol(item: tuple[Symbol, str]) -> tuple[list[str], int]:
            symbol, symbol_file = item
            matches = [p for p in search_code(owner, repo, symbol.name) if p != symbol_file]
            return matches[:MAX_SEARCH_CANDIDATES], len(matches)

        search_results = list(pool.map(_search_symbol, symbols_to_search))

        # Round 4: fetch+confirm every (symbol, candidate) pair in one flat batch.
        confirm_tasks = [
            (symbol, symbol_file, candidate)
            for (symbol, symbol_file), (candidates, _total) in zip(symbols_to_search, search_results)
            for candidate in candidates
        ]
        confirm_results = pool.map(
            lambda task: _confirm_usage(owner, repo, task[0], task[1], task[2]), confirm_tasks
        )

        refs_by_symbol: dict[int, tuple[list[tuple[str, int]], list[tuple[str, int]]]] = {}
        for (symbol, _symbol_file, _candidate), result in zip(confirm_tasks, confirm_results):
            confirmed, possible = refs_by_symbol.setdefault(id(symbol), ([], []))
            if result is None:
                continue
            path, lines, is_confirmed = result
            bucket = confirmed if is_confirmed else possible
            bucket.extend((path, line) for line in lines)

        caller_blocks = []
        any_search_truncated = False
        for (symbol, symbol_file), (candidates, total_matches) in zip(symbols_to_search, search_results):
            confirmed, possible = refs_by_symbol.get(id(symbol), ([], []))
            block = _format_references_block(symbol, symbol_file, confirmed, possible)
            truncated = total_matches > len(candidates)
            any_search_truncated = any_search_truncated or truncated
            if truncated:
                log.info(
                    "pr_context: search cap reached for `%s` (%s) — checked %d/%d matches",
                    symbol.name, symbol_file, len(candidates), total_matches,
                )
            if block is None and not truncated:
                continue
            if block is None:
                block = f"### {'Callers' if symbol.kind == 'function' else 'Subclasses'} of `{symbol.name}` ({symbol_file}:{symbol.start_line})"
            if truncated:
                block += f"\n- [only checked the first {len(candidates)} of {total_matches} search matches — some real references may be missing]"
            caller_blocks.append(block)

    truncation_note = ""
    if skipped_symbols:
        log.info(
            "pr_context: changed-symbol cap reached — checked callers for %d/%d changed symbols",
            len(symbols_to_search), len(changed_symbols),
        )
        skipped_names = ", ".join(f"`{s.name}`" for s, _f in skipped_symbols)
        truncation_note = (
            f"\n\nNote: this diff changed {len(changed_symbols)} functions/classes; only the "
            f"first {MAX_CHANGED_SYMBOLS} were checked for callers/subclasses elsewhere in the "
            f"repo (not checked: {skipped_names}) — treat the absence of those from the sections "
            f"below as 'not checked', not 'no callers exist'."
        )

    sections = [
        ("Same-file context", same_file_blocks),
        ("Callers / subclasses (reverse dependencies)", caller_blocks),
        ("Direct imports", import_blocks),
    ]

    assembled = []
    used_chars = 0
    budget_truncated = False
    for title, blocks in sections:
        if not blocks:
            continue
        block_text = f"## {title}\n" + "\n\n".join(blocks)
        if used_chars + len(block_text) > budget_chars:
            remaining = budget_chars - used_chars
            if remaining > 200:
                assembled.append(block_text[:remaining] + "\n[... truncated]")
            budget_truncated = True
            break
        assembled.append(block_text)
        used_chars += len(block_text)

    truncated = bool(skipped_symbols) or any_search_truncated or budget_truncated

    if not assembled:
        return RepoContext(text="", truncated=truncated)

    text = (
        "The following is deterministic static-analysis context — parsed imports, call "
        "sites, and class hierarchy for the code this diff touches, fetched on demand from "
        "GitHub (not embeddings/semantic search, and not a full-repo scan: caller/subclass "
        "matches come from GitHub code search results that were then re-fetched and re-parsed "
        "to confirm they're real references, not a text-search false positive). It may be "
        "incomplete — code search doesn't guarantee full coverage of the repo, and matches "
        "marked as a 'possible'/unconfirmed match should be treated with the same caution as "
        "anything else you can't directly verify." + truncation_note + "\n\n" + "\n\n".join(assembled)
    )
    return RepoContext(text=text, truncated=truncated)


def safe_build_context(owner: str, repo: str, diff: str, head_sha: str) -> RepoContext | None:
    """`build_context`, but any failure (fetch/parse/rate-limit/etc.) is logged
    and swallowed rather than raised. Repo context is a nice-to-have for the
    review, not a hard dependency — every caller of this (the webhook path,
    the manual `/review` endpoint, and `review_real_pr.py`'s standalone demo
    script) should fall back to a diff-only review rather than fail outright.

    Returns None both when context couldn't be built and when it came back
    empty, so callers can treat "no context" uniformly.
    """
    try:
        return build_context(owner, repo, diff, head_sha) or None
    except Exception:
        log.warning("Repo-context build failed for %s/%s; falling back to diff-only review", owner, repo, exc_info=True)
        return None
