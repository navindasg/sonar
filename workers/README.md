# workers/

Scheduled **deterministic** scripts (launchd LaunchAgents). Each does bounded work, may call the
LLM as a **leaf step** (classify/draft/summarize), and writes to the state store — control flow is
plain code, never an LLM loop. Workers are MCP **clients** of the same servers the harness uses.

Planned: `brief-builder` (assemble any-time brief → SQLite + a vault note), `email-poll` (pull
unread, classify, **draft** suggested replies — never send), `calendar-sync`, misc `watchers`.

**Status:** none built yet. First proof = spike **S6** (`brief-builder` writes SQLite + a vault note
on schedule). launchd chosen over cron (cron silently skips sleep-missed jobs).
