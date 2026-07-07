#!/usr/bin/env python3
"""Reusable retrieval eval harness for the Obsidian RAG server.

What it does:
  1. Loads evals/config.yaml (the real vault, quoted path — read-only on notes).
  2. Builds / refreshes the FAISS index via the real indexer (artifacts land in
     ~/.obsidian-rag/<vault-name>/, never in the vault).
  3. Runs every golden question in evals/golden.json through the SAME retriever
     the MCP `search` tool uses (obsidian_rag.retriever.search).
  4. Reports notes+chunks indexed and hit-rate@5 (expected note in top-5), plus
     passing/failing examples so the golden set can be corrected.

Usage (from rag/):
    uv run python evals/run_eval.py
    uv run python evals/run_eval.py --json        # machine-readable summary on stdout
    uv run python evals/run_eval.py --k 5 --threshold 0.0

Ranking hit@5 is measured at similarity_threshold=0.0 by default so we grade the
ranker itself, not a threshold cut. The harness ALSO reports how many hits
survive the production threshold (retrieval.similarity_threshold in config) as a
tuning signal.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the package importable whether or not it's installed.
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import ollama  # noqa: E402

from obsidian_rag.config import load_config  # noqa: E402
from obsidian_rag.indexer import build_index  # noqa: E402
from obsidian_rag.retriever import search as retriever_search  # noqa: E402

_HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = _HERE / "config.yaml"
DEFAULT_GOLDEN = _HERE / "golden.json"


def load_golden(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    questions = data["questions"]
    for q in questions:
        if not q.get("expected"):
            raise ValueError(f"Golden question {q.get('id')!r} has no 'expected' notes")
    return questions


def embed_query(cfg, query: str) -> list[float]:
    client = ollama.Client(host=cfg.embedding.ollama_url)
    resp = client.embed(model=cfg.embedding.model, input=[query])
    return resp.embeddings[0]


def run(config_path: Path, golden_path: Path, k: int, threshold: float) -> dict:
    cfg = load_config(str(config_path))
    vault_cfg = cfg.vaults[0]

    # --- Build the index (read-only on the vault; artifacts under storage dir) ---
    index, metadata, _hashes = build_index(cfg, vault_cfg)
    note_count = len({m["file"] for m in metadata.values()})
    chunk_count = index.ntotal
    prod_threshold = cfg.retrieval.similarity_threshold

    questions = load_golden(golden_path)

    results: list[dict] = []
    hits = 0
    hits_at_prod_threshold = 0

    for q in questions:
        emb = embed_query(cfg, q["question"])
        # Grade the ranker: threshold=0.0 so nothing is cut before top-k.
        res = retriever_search(
            index,
            metadata,
            emb,
            top_k=k,
            similarity_threshold=threshold,
            max_context_tokens=cfg.retrieval.max_context_tokens,
            query_text=q["question"],
        )
        ranked = res.get("results", [])
        top_paths = [r["source_path"] for r in ranked]
        expected = set(q["expected"])

        hit_rank = next(
            (i + 1 for i, p in enumerate(top_paths) if p in expected), None
        )
        is_hit = hit_rank is not None
        hits += int(is_hit)

        # Would this hit survive the production similarity threshold?
        if is_hit:
            hit_score = ranked[hit_rank - 1]["relevance_score"]
            if hit_score >= prod_threshold:
                hits_at_prod_threshold += 1

        results.append(
            {
                "id": q["id"],
                "question": q["question"],
                "expected": q["expected"],
                "hit": is_hit,
                "hit_rank": hit_rank,
                "hit_score": ranked[hit_rank - 1]["relevance_score"] if is_hit else None,
                "top5": [
                    {"path": r["source_path"], "score": r["relevance_score"]}
                    for r in ranked
                ],
            }
        )

    total = len(questions)
    return {
        "notes_indexed": note_count,
        "chunks_indexed": chunk_count,
        "embedding_model": cfg.embedding.model,
        "vault_path": str(vault_cfg.path),
        "k": k,
        "eval_threshold": threshold,
        "prod_threshold": prod_threshold,
        "questions": total,
        "hits": hits,
        "hit_rate_at_k": round(hits / total, 3) if total else 0.0,
        "hits_at_prod_threshold": hits_at_prod_threshold,
        "hit_rate_at_prod_threshold": round(hits_at_prod_threshold / total, 3)
        if total
        else 0.0,
        "results": results,
    }


def print_report(summary: dict) -> None:
    print("=" * 72)
    print("Obsidian RAG — retrieval eval")
    print("=" * 72)
    print(f"Vault:            {summary['vault_path']}")
    print(f"Embedding model:  {summary['embedding_model']}")
    print(f"Notes indexed:    {summary['notes_indexed']}")
    print(f"Chunks indexed:   {summary['chunks_indexed']}")
    print(
        f"hit-rate@{summary['k']}:      {summary['hit_rate_at_k']:.3f} "
        f"({summary['hits']}/{summary['questions']}) "
        f"[eval threshold={summary['eval_threshold']}]"
    )
    print(
        f"  survives prod threshold {summary['prod_threshold']}: "
        f"{summary['hit_rate_at_prod_threshold']:.3f} "
        f"({summary['hits_at_prod_threshold']}/{summary['questions']})"
    )
    print("-" * 72)
    passing = [r for r in summary["results"] if r["hit"]]
    failing = [r for r in summary["results"] if not r["hit"]]

    print(f"PASSING ({len(passing)}):")
    for r in passing:
        print(
            f"  [rank {r['hit_rank']}, score {r['hit_score']:.2f}] {r['id']}: "
            f"{r['expected'][0]}"
        )
    print()
    print(f"FAILING ({len(failing)}):")
    for r in failing:
        top = ", ".join(f"{t['path']}({t['score']:.2f})" for t in r["top5"][:3])
        print(f"  {r['id']}: expected {r['expected']}")
        print(f"    Q: {r['question']}")
        print(f"    top3: {top or '(none)'}")
    print("=" * 72)


def main() -> None:
    ap = argparse.ArgumentParser(description="Obsidian RAG retrieval eval")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--threshold", type=float, default=0.0)
    ap.add_argument("--json", action="store_true", help="Emit JSON summary on stdout")
    args = ap.parse_args()

    summary = run(args.config, args.golden, args.k, args.threshold)

    if args.json:
        # Drop per-question detail's verbose top5 for a compact machine summary.
        compact = {k: v for k, v in summary.items() if k != "results"}
        compact["results"] = [
            {"id": r["id"], "hit": r["hit"], "hit_rank": r["hit_rank"]}
            for r in summary["results"]
        ]
        print(json.dumps(compact, indent=2))
    else:
        print_report(summary)


if __name__ == "__main__":
    main()
