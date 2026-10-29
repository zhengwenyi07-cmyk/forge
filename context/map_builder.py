"""Task retrieval, graph ranking and token-bounded symbol rendering."""
from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

from context.graph_rank import pagerank
from context.repo_index import RepoIndex
from context.token_budget import estimate_tokens


def words(text):
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return set(re.findall(r"[a-z][a-z0-9]*", text.lower()))


class RankedRepoMap:
    def __init__(self, repo_path):
        self._root = Path(repo_path).resolve()
        self.index = RepoIndex(self._root)
        self.last_diagnostics = {}

    def cache_key(self, task="", mentioned_paths=(), budget=4000, strategy="personalized"):
        return (str(self._root), self.index.refresh(), task, tuple(mentioned_paths), budget, strategy)

    def build(self, budget=4000, task="", mentioned_paths=(), strategy="personalized"):
        if strategy not in ("personalized", "global", "lexical", "legacy"):
            raise ValueError("Unknown repo-map strategy")
        if strategy == "legacy":
            return self._build_legacy(budget)
        self.index.refresh()
        if budget <= 0:
            return ""
        records = self.index.files
        if not records:
            return "(empty repository)" if estimate_tokens("(empty repository)") <= budget else ""
        query = words(task) - {"the", "a", "an", "to", "of", "and", "please", "fix", "bug"}
        path_words = {p: words(p) for p in records}
        symbol_words = {p: words(" ".join(d.signature for d in f.definitions)) for p, f in records.items()}
        frequency = Counter(w for p in records for w in (path_words[p] | symbol_words[p]))
        lexical = {p: sum((math.log((len(records) + 1) / (frequency[w] + 1)) + 1)
                           * (4 if w in path_words[p] else 1) for w in query & (path_words[p] | symbol_words[p]))
                   for p in records}
        normalized_task = task.replace("\\", "/")
        explicit = {p for p in records if re.search(r"(?<![\w.-])" + re.escape(p) + r"(?![\w.-])", normalized_task)}
        mentioned = set()
        for path in mentioned_paths:
            path = str(path).replace("\\", "/")
            if path.startswith(self._root.as_posix() + "/"):
                path = path[len(self._root.as_posix()) + 1:]
            if path in records:
                mentioned.add(path)
        seeds = {p: lexical[p] + (20 if p in explicit else 0) + (10 if p in mentioned else 0) + 0.01
                 for p in records}
        ranks = pagerank(self.index.edges, seeds if strategy == "personalized" else None)
        lex_order = sorted(records, key=lambda p: (-lexical[p], p))
        graph_order = sorted(records, key=lambda p: (-ranks[p], p))
        lex_pos = {p: i + 1 for i, p in enumerate(lex_order)}
        graph_pos = {p: i + 1 for i, p in enumerate(graph_order)}
        def score(path):
            if strategy == "global":
                return ranks[path]
            if strategy == "lexical":
                return lexical[path]
            return (1 / (60 + lex_pos[path]) if lexical[path] else 0) + 1 / (60 + graph_pos[path])
        ranked = sorted(records, key=lambda p: (p not in explicit, -score(p), p))
        references = Counter()
        for record in records.values():
            references.update(record.references)
            references.update({name: count for (owner, name), count in record.attributes.items()})
        definitions = {}
        for path in ranked[:64]:
            definitions[path] = sorted(records[path].definitions, key=lambda d: (
                -len(words(d.qualified_name) & query), -math.log1p(references[d.name]), d.line))
        selected, included = {}, []
        def render():
            return "\n".join(path + (":" if selected[path] else "") + "\n" +
                             "\n".join(f"  {d.signature} (line {d.line})" for d in sorted(selected[path], key=lambda d: d.line))
                             for path in included).strip()
        # Admit compact blocks; an oversized candidate never terminates selection.
        for path in ranked[:64]:
            included.append(path)
            selected[path] = definitions[path][:2]
            while estimate_tokens(render()) > budget and selected[path]:
                selected[path].pop()
            if estimate_tokens(render()) > budget:
                included.pop()
                del selected[path]
                continue
            if estimate_tokens(render()) >= budget * 0.65:
                break
        # Spend the remaining budget on additional symbols, in file rank order.
        for path in included:
            for definition in definitions[path]:
                if definition in selected[path]:
                    continue
                selected[path].append(definition)
                if estimate_tokens(render()) > budget:
                    selected[path].pop()
        output = render()
        omitted = len(records) - len(included)
        marker = f"\n... ({omitted} more files not shown)" if omitted else ""
        if estimate_tokens(output + marker) <= budget:
            output += marker
        self.last_diagnostics = {"strategy": strategy, "index_version": self.index.version,
                                 "files": len(records), "edges": sum(len(e) for e in self.index.edges.values()),
                                 "included_files": included, "estimated_tokens": estimate_tokens(output),
                                 "parse_status": dict(Counter(f.parse_status for f in records.values()))}
        return output
