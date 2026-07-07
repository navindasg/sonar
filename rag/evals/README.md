# rag/evals

Retrieval eval harness for the Obsidian RAG server — measures whether a golden
set of questions retrieves the right notes.

- `run_eval.py` — builds the FAISS index (read-only on your vault; artifacts land
  under `~/.obsidian-rag/<name>/`, never in the vault) and scores golden
  questions by **hit-rate@5** (expected note in top-5). Grades the ranker at
  `--threshold 0.0` and also reports how many hits survive the production
  `similarity_threshold`.
- `config.example.yaml`, `golden.example.json` — safe templates using the
  checked-in fictional sample fixtures.

## Setup (local — not committed)

```sh
cp config.example.yaml config.yaml       # point path: at your real vault
cp golden.example.json golden.json       # your own question -> expected-note pairs
```

`config.yaml` and `golden.json` are **gitignored**: they carry personal vault
content (questions, facts, absolute paths) and this is a public repo. Keep them
local.

## Run (from `rag/`)

```sh
uv run python evals/run_eval.py            # human-readable report
uv run python evals/run_eval.py --json     # machine summary on stdout
uv run python evals/run_eval.py --k 5 --threshold 0.0
```
