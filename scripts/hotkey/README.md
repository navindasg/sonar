# hotkey — F5(dictation) → F13 remap

Remaps the MacBook dictation key (physically **F5** on Apple-silicon laptops) to
**F13**, so Sonar's overlay can catch a plain global hotkey on F13 without
fighting system dictation and **without triggering an Accessibility prompt**.

This is the `hidutil` path from the S1 spike (Karabiner-Elements is the durable
upgrade — see the fallback note at the bottom).

## Why remap at all

F5 on Apple-silicon laptops is a **dedicated dictation key**. It does not emit a
normal keyboard keycode — it emits a **Consumer-page HID usage**. A naive
`keyDown` tap may never even see it, and app-level hotkey APIs cannot override
system dictation. So instead of fighting the key at the tap layer, we remap it
**upstream** to F13 (an ordinary keyboard usage nothing else uses), then register
a plain global hotkey on F13.

## Why these codes

`hidutil` `UserKeyMapping` matches on 64-bit HID usage values: the **high 32
bits are the usage page**, the **low 32 bits are the usage**.

| Role | Human | hidutil value | Decimal (as hidutil prints it) |
|------|-------|---------------|-------------------------------|
| Source (dictation/F5) | Consumer page `0x0C`, usage `0xCF` ("Voice Command" / dictation) | `0x0000000C000000CF` | `51539607759` |
| Destination (F13) | Keyboard page `0x07`, usage `0x68` | `0x700000068` | `30064771176` |

- **Destination F13 = `0x700000068`** is unambiguous: keyboard usage page `0x07`,
  usage `0x68` is F13 in the USB HID Usage Tables. This is the value the task
  brief specified and it is standard.
- **Source = `0x0000000C000000CF`** (equivalently `0xC000000CF`). The dictation
  key lives on the **Consumer page (`0x0C`)**, not the keyboard page — which is
  exactly why a keyboard-page tap can't catch it — and its usage is `0xCF`
  ("Voice Command" / dictation). Packed the hidutil way, `(0x0C << 32) | 0xCF`
  = `0x0000000C000000CF`. The `docs/RESEARCH.md` §2 shorthand `0x000c00cf` just
  names the page/usage pair loosely; the real 64-bit hidutil operand is the
  packed value above. (Earlier drafts used `0x000C0000000000CF`, which packs the
  page one 32-bit field too high — page `0xC0000`, invalid — so it would never
  match a real keypress. Corrected and re-verified on this machine.)

> Note: `scripts/README.md` mentions the source as `0x000c000CF` — that is a
> loose short-form; the correct 64-bit hidutil operand is `0x0000000C000000CF`
> as used here and verified on this machine (see "What we observed").

## What we observed (this machine)

macOS 26.5.1 (build 25F80), Apple-silicon MacBook Pro. Verified non-destructively:

1. `hidutil property --get "UserKeyMapping"` → empty (clean, no prior remap).
2. Applied `remap.sh`'s mapping → `hidutil` accepted it (exit 0) and read back:
   `Src = 51539607759` (`0x0000000C000000CF`),
   `Dst = 30064771176` (`0x700000068`). **Mapping applies cleanly.**
3. `doctor.sh` → `STATUS: ACTIVE`. Cleared with `unmap.sh` → mapping gone,
   machine left un-remapped.

The mapping is **accepted and stored** by `hidutil` on this hardware. Whether a
physical F5 press then delivers F13 into an app was **not** exercised
end-to-end here (that needs an interactive key viewer — see `doctor.sh` for the
exact check to run when you apply it for real). If F13 does **not** arrive after
applying, that points to the Karabiner fallback below.

## Files

| File | What it does |
|------|--------------|
| `com.sonar.hotkey-remap.plist` | LaunchAgent that runs `hidutil` to apply the remap at login (`RunAtLoad`). No sudo. |
| `remap.sh`   | Apply the remap now (same command the LaunchAgent runs). |
| `unmap.sh`   | Clear the remap for the current session. |
| `doctor.sh`  | Print current `UserKeyMapping` + LaunchAgent state, and how to confirm F13 arrives. |
| `install.sh` | Copy the plist into `~/Library/LaunchAgents` and `launchctl load` it (persists across login). |
| `uninstall.sh` | Unload + remove the plist and clear the active mapping. |

## How to use

```sh
cd scripts/hotkey

# Try it for one session (no persistence):
./remap.sh
./doctor.sh          # confirm it's ACTIVE + how to test F13 arrives
./unmap.sh           # undo

# Make it stick across reboots/logins:
./install.sh         # copies plist to ~/Library/LaunchAgents and loads it (applies now too)
./doctor.sh
./uninstall.sh       # remove it permanently and restore default keys
```

`hidutil` remaps are **volatile** — lost on reboot and on keyboard reconnect.
That's the whole reason for the LaunchAgent: it re-applies the mapping at every
login. (It does **not** re-apply on a mid-session keyboard reconnect; log out/in
or re-run `remap.sh` if that happens.)

## What to expect

- After applying, pressing the physical dictation/**F5** key should register as
  **F13** and should **no longer** start macOS dictation.
- No Accessibility (TCC) prompt — `hidutil` user key mapping needs no special
  permission and no root.
- Every other key is untouched.

## Fallback: Karabiner-Elements

If `hidutil` can't reliably deliver F13 from this key on your hardware/OS (e.g.
the Consumer-page source isn't honored, or the remap doesn't survive the way you
need), switch to **Karabiner-Elements**. It installs a virtual-HID system
extension, survives reboot/sleep without a LaunchAgent, and can remap the
dictation key to F13 via a simple `from`/`to` complex-modification rule. It's the
heavier but more durable option flagged in `docs/DECISIONS.md` (Remap tool:
"start `hidutil`; Karabiner-Elements is the durable upgrade").
