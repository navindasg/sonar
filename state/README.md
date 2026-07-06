# state/

Live/ephemeral state in **SQLite (WAL mode)** — the fast, disposable layer workers write and the
harness reads (inbox lane, merged calendar, open loops, worker/training status).

Durable, semantic memory lives in the **Obsidian vault** (via `rag/`), **not here**. The DB file
itself lives **outside the vault** (Obsidian's indexer and SQLite `-wal`/`-shm` files fight) and is
**gitignored** (`*.sqlite*`, `state/*.db`, `state/data/`). This dir holds **schema/migrations only**.

**Status:** schema TBD. First proof = spike **S6**.
