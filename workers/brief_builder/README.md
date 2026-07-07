# brief-builder

Spike **S6** — proof of the **deterministic-worker + state-store** pattern.

`brief-builder` is a plain-code scheduled worker: control flow is ordinary
Python and the LLM is only a **bounded leaf call**. It assembles a short "daily
brief" and writes it to **both**:

1. the **SQLite live-state DB** (`briefs` + `worker_runs` rows), and
2. a **markdown note** in the Obsidian vault, under `Sonar/Briefs/` only.

## Flow

```
gather (recent vault notes, plain code)
  -> llm.summarize (ONE Ollama leaf call, non-streaming, bounded, timeout)
  -> compose markdown (deterministic)
  -> write vault note  (path-safe, under Sonar/Briefs/ only)
  -> write DB rows     (briefs + worker_runs audit, parameterized SQL, WAL)
```

Schema lives at [`state/schema.sql`](../../state/schema.sql) (single source of
truth). WAL is enabled in code (`db.py`), not in the DDL, because
`journal_mode` is a runtime pragma.

## Vault-write safety (critical)

- The worker **only ever** writes to `<vault>/Sonar/Briefs/<YYYY-MM-DD>-<window>.md`.
- It **never** overwrites or touches any existing user note outside that folder.
  Every computed path is re-resolved and asserted to be inside `Sonar/Briefs/`.
- If the target file already exists, a **timestamped variant** is written
  (`...-HHMMSS.md`) — the original is never clobbered.
- The `Sonar/` tree is **excluded** from input gathering, so briefs never feed
  on themselves.
- `--dry-run` composes and prints the brief to stdout and writes **nothing**
  (no note, no DB file).

## Layout

| File | Concern |
|------|---------|
| `config.py`   | env vars + defaults, immutable `Config` |
| `db.py`       | open/init WAL, parameterized inserts, worker-run audit |
| `gather.py`   | read N most-recent vault notes (excl. `Sonar/`), titles + first lines |
| `llm.py`      | the ONE Ollama leaf call + prompt builder + graceful errors |
| `vault.py`    | path-safety + note writing |
| `brief.py`    | orchestrate gather → leaf → compose → write |
| `__main__.py` | CLI (`--window`, `--vault`, `--db`, `--dry-run`) |

## Configuration

| Setting | Env | Default |
|---------|-----|---------|
| Ollama endpoint | `OLLAMA_HOST` | `http://127.0.0.1:11434` |
| Fast model | `MODEL_FAST` | `gemma4:e4b-mlx` |
| Vault root | `SONAR_VAULT` / `--vault` | `~/Documents/Obsidian Vault` |
| State DB | `SONAR_DB` / `--db` | `~/.config/sonar/state/sonar.db` |
| Schema path | `SONAR_SCHEMA` | auto-located (`state/schema.sql`) |

No secrets are used or stored — the model endpoint is localhost.

## Develop / test

```bash
cd workers/brief_builder
uv sync
uv run pytest        # unit tests; the LLM leaf call is MOCKED (no live model)
```

## Manual smoke (uses the live local model)

Run against a **temp** vault first — never point a smoke run at your real vault
until you trust it. `--dry-run` writes nothing:

```bash
cd workers/brief_builder
mkdir -p /tmp/sonar-smoke-vault
# put a couple of .md notes in /tmp/sonar-smoke-vault first, then:
uv run python -m brief_builder --window any --vault /tmp/sonar-smoke-vault --dry-run
# real write into the temp vault + a temp db:
uv run python -m brief_builder --window any \
  --vault /tmp/sonar-smoke-vault \
  --db /tmp/sonar-smoke.db
```

The **first real brief against your actual vault** is something you run
yourself, e.g.:

```bash
uv run python -m brief_builder --window any --vault "$HOME/Documents/Obsidian Vault"
```

## Schedule (launchd)

`com.sonar.brief-builder.plist` is a **template** (placeholders substituted by
`install.sh`). launchd is used over cron because cron silently skips jobs
missed while the Mac is asleep (see `docs/DECISIONS.md`). `RunAtLoad` is
`false`, so installing does not immediately fire a brief.

```bash
cd workers/brief_builder
./install.sh      # installs + loads ~/Library/LaunchAgents/com.sonar.brief-builder.plist (07:00 daily)
./uninstall.sh    # unloads + removes it
```

Logs: `~/Library/Logs/sonar/brief-builder.{out,err}.log`.
