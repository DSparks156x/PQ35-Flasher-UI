# PQ35 Flasher UI

A desktop app for [PQ35-Flasher](https://github.com/DSparks156x/PQ35-Flasher): back up,
patch and flash Haldex Gen4 and ZF EPS rack modules, and read EPS diagnostics, without
touching the command line.

Same rules as the flasher: you're at your own risk, and it can't stop you flashing the
wrong firmware in recovery mode. It does try hard to stop everything else.

## Install

**Windows** (PowerShell, no admin needed):

```powershell
irm https://raw.githubusercontent.com/DSparks156x/PQ35-Flasher-UI/main/install.ps1 | iex
```

**Linux**:

```bash
curl -LsSf https://raw.githubusercontent.com/DSparks156x/PQ35-Flasher-UI/main/install.sh | sh
```

The installer sets up [uv](https://docs.astral.sh/uv/), a private Python for the app,
and on Windows a private 32-bit Python for J2534 adapters. It doesn't touch any Python
you already have. Then it adds a Start menu / app menu shortcut. Run it again to update.

From a checkout: `git clone --recurse-submodules …`, then `.\install.ps1` or `./install.sh`.

## Adapters

| Adapter | Windows | Linux |
|---|---|---|
| J2534 (Scanmatik, Tactrix, …) | Install the vendor's driver | Not supported |
| CANable / candleLight (gs_usb firmware) | Works out of the box on Windows 10/11 | Shows up as `can0`; pick SocketCAN |
| SocketCAN | n/a | `sudo ip link set can0 up type can bitrate 500000` first |
| Panda | `uv pip install pandacan` into `.venv` (untested) | same |

candleLight firmware tells Windows to use the built-in WinUSB driver, so no Zadig is
needed. Use Zadig (bind WinUSB) only if the adapter doesn't appear, for example because
another driver claimed it earlier. CANables shipped with the older slcan firmware show
up as a COM port and need [candleLight firmware](https://canable.io/updater/) instead.

## How it works

```
PySide6 UI (64-bit)  ──JSON lines──▶  worker  ──▶  flasher (submodule)  ──▶  CAN adapter
                                     │
                                     └─ 32-bit worker for J2534 (stdlib only)
```

- The UI never opens the adapter. A worker process owns it, one job at a time, behind a
  lock file, so two windows can't talk to a module at once.
- J2534 DLLs are 32-bit, so on Windows J2534 jobs run in a worker under a 32-bit Python.
  Every other adapter uses the app's own Python.
- `flasher/` is the [PQ35-Flasher](https://github.com/DSparks156x/PQ35-Flasher)
  submodule. Change the flasher there first, then bump the submodule here.

## Safety rails

- **The module must identify as the one you picked** before anything past Connect unlocks.
- **Images are checked when you choose them**: size, internal checksums, which module
  they're for, and whether the part number matches the connected ECU. A Haldex image
  can't be flashed with the EPS selected.
- **A backup is required before the first flash** of each module. Backups go into a
  per-module library.
- **The flash range is chosen for you.** The new image is diffed against the module's
  latest backup (or the last image flashed on top of it), and the smallest whole range
  covering every changed byte is preselected. With no trustworthy reference, the full
  range is preselected.
- **A checklist and a 3-second hold-to-flash** replace typing `YES`. Recovery mode
  needs `RECOVER` typed.
- **While flashing**, the window refuses to close and the computer is kept awake.
- **After a flash**, the worker releases the adapter and identifies the module again from
  scratch. Success is only reported after that.
- Fixing checksums, bench simulator patches, recovery and flashing without a backup are
  hidden under advanced options.

## Where things are kept

- Windows: `%LOCALAPPDATA%\PQ35-Flasher`
- Linux: `~/.local/share/pq35-flasher`

That folder holds `library/<module>/` (backups, flash history, `state.bin`), `logs/`
(one log per session) and `settings.json`. **Log → Save support bundle** zips the session
log and module history (no firmware) for bug reports.

## Development

```bash
uv sync
uv run python -m pq35ui            # the app
uv run python -m pq35ui.worker     # a worker on stdin/stdout, for poking at the protocol
```
