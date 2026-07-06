# config/

Templated, **secret-free** configuration. Real secrets live in the **macOS Keychain** or
`~/.config/sonar/` and are **never committed** (this repo is public).

- `.env.example` — copy to `.env` (gitignored) and fill locally, or prefer Keychain.
- Local overrides use the `*.local.yaml` / `*.local.json` suffix (gitignored).

**Status:** `.env.example` seeded; structured config lands with the harness.
