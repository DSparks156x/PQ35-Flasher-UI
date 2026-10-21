# PQ35 Flasher UI

A slick UI for [PQ35-Flasher](https://github.com/DSparks156x/PQ35-Flasher): back up,
patch and flash Haldex Gen4 and ZF EPS rack modules, and read EPS diagnostics, without
touching the command line.

You are at your own risk using this project. I am not responsible for thermonuclear war, broken timing chains, divorce, or you being sad because your EPS no longer works if something goes wrong. 

This project is intended to limit the ways you can screw things up, and make the flashing process simple - but it cannot stop you from being a dumbass. If you use the ignore safeguards buttons and flash a binary of the game tetris to your EPS, I cannot help you. 

A failed flash means that module does not function until it has been successfully recovered. In very rare cases, it may not be recoverable.  This means no EPS. 

In practice its pretty hard to brick shit beyond recovery, but please - 
Do not Dumb. 

## Install

**Windows** (PowerShell, no admin needed):

```powershell
irm https://raw.githubusercontent.com/DSparks156x/PQ35-Flasher-UI/main/install.ps1 | iex
```

**Linux and macOS** (needs git; on macOS run `xcode-select --install` first if you
don't have it):

```bash
curl -LsSf https://raw.githubusercontent.com/DSparks156x/PQ35-Flasher-UI/main/install.sh | sh
```

The installer sets up [uv](https://docs.astral.sh/uv/) and a private Python for the
app. On Windows it also adds a private 32-bit Python for J2534 adapters. It doesn't touch
any Python you already have. It then asks whether to add shortcuts: Start menu and
desktop on Windows, app menu and desktop on Linux, Applications folder on macOS.
Set `PQ35_YES=1` to answer yes to everything. Run the installer again to update.

Without shortcuts, start it with `run.cmd` (Windows) or `pq35-flasher` (Linux/macOS)
in the install folder:

| | Install folder |
|---|---|
| Windows | `%LOCALAPPDATA%\PQ35-Flasher-UI\app` |
| Linux | `~/.local/share/pq35-flasher-ui/app` |
| macOS | `~/Library/Application Support/PQ35-Flasher-UI/app` |

From a checkout: `git clone --recurse-submodules …`, then `.\install.ps1` or `./install.sh`.

## Updating

The bottom-left corner shows the installed version (and the flasher commit). Press
**Check for updates**: if GitHub has something newer, it lists what changed and offers
**Update now**. That closes the app, runs the installer in update mode (pulls, or
re-downloads a zip install, re-syncs, keeps your shortcuts and data), and reopens the app.
On Windows you see the update in a console window. Updating is blocked while the app is
talking to a module.

Re-running the one-line installer does the same thing by hand.

## Adapters

| Adapter | Windows | Linux | macOS |
|---|---|---|---|
| J2534 (Scanmatik, Tactrix, …) | Install the vendor's driver | Not supported | Not supported |
| CANable / candleLight (gs_usb firmware) | Works out of the box on Windows 10/11 | Shows up as `can0`; pick SocketCAN | gs_usb (untested) |
| SocketCAN | n/a | Brought up from the Connect page | n/a |
| Panda | `uv pip install pandacan` into `.venv` (untested) | same | same |

candleLight firmware tells Windows to use the built-in WinUSB driver, so no Zadig is
needed. Use Zadig (bind WinUSB) only if the adapter doesn't appear, for example because
another driver claimed it earlier. CANables shipped with the older slcan firmware show
up as a COM port and need [candleLight firmware](https://canable.io/updater/) instead.

On Linux, a SocketCAN interface that is down, or not at 500 kbit/s, gets a **Bring up**
button on the Connect page. It runs `ip link set … up type can bitrate 500000` through
`pkexec`, so you're asked for your password.

## SGO files (EPS)

Pick or drop an EPS `.sgo` and it's converted to a `.bin` in
`Documents/PQ35 Flasher/Images` first: a full firmware SGO becomes a 384 KiB image, a
steer-dataset SGO becomes a 4 KiB 0x5E dataset. Some dataset SGOs (v074N…) also carry a
0x5D block and two small low-address records; only the 0x5E block is used. Gen2
datasets (firmware below 3000) and partial-code SGOs are refused. Everything after that (checks, patches,
flashing) uses the `.bin`, which goes through exactly the same rules as any other
image. Only PQ35 racks (1K0/8J0 909 144) are accepted; SGOs for other controllers,
such as 5N1 909 144 PQ46 racks, are refused. The converter follows
`unpack_firmware_sgos.py` in [DSparks156x/pq-flasher](https://github.com/DSparks156x/pq-flasher)
(a fork of Willem Melching's pq-flasher, MIT) and reproduces its output byte for byte. Haldex: `.bin` only.

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
- **Haldex: a backup is required before the first flash**, and goes into a per-module
  library. The full application is the default. Smaller ranges appear under advanced
  options, and only when they cover every byte that differs from the module's last known
  image.
- **EPS: no backup step**, because racks can't be read back over OBD. What can be written
  depends on the firmware the module reports:
  - 0x5E steer dataset: firmware 3000 or newer, and never the factory `000` placeholder.
    It's the default.
  - 0x5D configuration block: only onto the exact firmware version it was built for. It
    becomes the default when the image has a 0x5D patch such as HCA applied.
  - Full firmware: modules on 2901 or newer. Upgrading from older firmware isn't
    understood yet.
  - A full flash of a factory image with the `000` placeholder dataset prompts you to
    flash a real dataset straight after.
  - **Patch…** on the Flash page patches the chosen image before you flash it. The
    choices are HCA timeout/minimum speed, slew and HCA gains, whichever fit its firmware.
    The patched copy goes in `Patched` and the chosen file never changes. Patching again
    starts from the original, so it replaces the earlier choice. The default write is the
    block the patches changed. When they change both 0x5D and 0x5E, write the two
    blocks in two flashes.
  - Full EPS flashes are disabled (only Recovery may try one): the loader's erase of the
    application drops the connection, so the flash fails and leaves the rack erased in
    its bootloader.
- **A checklist and a 3-second hold-to-flash** replace typing `YES`. Recovery mode
  needs `RECOVER` typed.
- **While flashing**, the window refuses to close and the computer is kept awake.
- **After a flash**, the worker releases the adapter and identifies the module again from
  scratch. Success is only reported after that.
- Fixing checksums, bench simulator patches, recovery and flashing without a backup are
  hidden under advanced options.

## Where things are kept

Your files go in **Documents/PQ35 Flasher** on every OS:

| Folder | What's in it |
|---|---|
| `Modules/<module>/Backups` | Readouts of that module, one folder each |
| `Modules/<module>/Flashed` | A copy of every image written to it, timestamped |
| `Modules/<module>/history.json` | Backups and flashes, with results |
| `Images` | Default place the file picker opens; keep your firmware here |
| `Patched` | Where patched images are saved by default |
| `Logs` | One log per session; the newest 20 are kept, each capped at 5 MB |

A module's folder is named after its family and part number, not its software
version, so backups stay with it across flashes. Demo mode uses `PQ35 Flasher/Demo`.
Settings and lock files live in the per-user app-data folder (`%LOCALAPPDATA%\PQ35-Flasher`,
`~/.local/share/pq35-flasher`, `~/Library/Application Support/PQ35-Flasher`).
**Log → Save support bundle** zips the session log and module history (no firmware).

## Demo mode

`python -m pq35ui --demo` adds a "Demo · pretend module" interface that simulates
identify, backup, flash and diagnostics with no hardware. Images still go through the
real flasher checks. The pretend module remembers what you flash to it, and its backups
are kept apart from the real library. To start it from a real image, set
`PQ35_DEMO_HALDEX` / `PQ35_DEMO_EPS` to a 320 KiB / 384 KiB file.

## Development

```bash
uv sync
uv run python -m pq35ui            # the app
uv run python -m pq35ui --demo     # with the pretend module
uv run python -m pq35ui.worker     # a worker on stdin/stdout, for poking at the protocol
```
