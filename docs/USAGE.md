# NEO Scavenger Save Guard — Usage Guide

Detailed companion to the [README](../README.md), aimed at day-to-day use and
troubleshooting. A Chinese version is kept at [USAGE.zh-CN.md](USAGE.zh-CN.md).

---

## 1. What it does on its own

The watcher polls every 10 seconds and writes a timestamped snapshot the moment any of
these conditions is met:

1. **`NEOScavenger.exe` starts**
   → snapshot the current save (equivalent to "one automatic backup per game launch").
2. **`nsSGv1.sol` content changes**
   → immediate snapshot (equivalent to "automatic backup after Quit and Save").
3. **It also opens the rollback GUI when the game starts**
   → no need to double-click anything yourself; launched at most once per game start
   → if the GUI is already open, that window is brought to the front (no second window)

Because it watches the **file and the process**, it works no matter how you launch the
game — Steam, desktop shortcut, anything.

---

## 2. Rollback: the GUI

**Opens automatically when you start the game**, so you normally never open it by hand.
To open it manually, double-click:

```
save_rollback_gui.pyw
```

(Launching it twice does not produce two windows: the program uses a named mutex for
single-instance protection, and focuses the existing window instead.)

What the window offers:

- A list of every snapshot (time / size / trigger reason / integrity); the first entry is
  "the last snapshot before death"
- An **[Integrity]** column that validates the `.sol` header — corrupt snapshots are shown
  in red and **refused for rollback**
- **[Restore as current save]** — writes the selected snapshot back to `nsSGv1.sol`
  (double-clicking a list row does the same)
- **[Store in slot 1/2/3]** — writes a snapshot into a NEO Save Manager named slot
- **[Store in Quicksave slot]** — writes `g0`, readable back in-game
- **[Open backup folder] [Refresh] [Restart watcher]**

After a rollback: launch the game → pick **Continue** in the main menu → carry on from that
save.

### Three safety mechanisms

1. **Rollback is refused while the game is running.** Flash holds the save in memory and
   overwrites `nsSGv1.sol` on exit — a rollback done during play would be silently undone,
   and you would conclude "rollback does not work". The GUI offers to end the game process
   first, and only proceeds after you confirm.
2. **The current save is preserved first**, as `*_prerestore.sol`, so the rollback itself is
   also revertible.
3. **The `.sol` header is validated before writing** (magic `00 BF` + a length field that
   must equal `filesize - 6`). A truncated snapshot is rejected — otherwise the game cannot
   load the save, and all you see is "the game will not start after a rollback", which is a
   very hard symptom to trace.

### Command-line equivalents

```
save_rollback_gui.pyw --list                          list all snapshots
save_rollback_gui.pyw --restore latest --yes          restore the newest snapshot
save_rollback_gui.pyw --restore 3 --yes               restore the 3rd entry in the list
save_rollback_gui.pyw --restore-slot 2                write the newest snapshot into slot 2
save_rollback_gui.pyw --status                        show status
```

> The command line needs a **Python build that includes tkinter**. For the GUI you can
> simply double-click without arguments — `.pyw` is associated with `pyw.exe`, so no
> console window appears.
>
> **Interface language**: English by default. For a Chinese interface set the `NSG_LANG=zh`
> environment variable, or put `{"lang": "zh"}` in `config.json`. See the *Language* section
> of the README. Note that the legacy filename `存档回档器.pyw` is still recognised by the
> watcher, so deployments migrated from v1.0.0 need no changes.

---

## 3. Where the backups live

Default backup directory:

```
<user Documents>\NEO Scavenger Save Guard\auto\
  nsSGv1_YYYYmmdd-HHMMSS_save.sol        snapshot triggered by a save change
  nsSGv1_YYYYmmdd-HHMMSS_start.sol       snapshot triggered by a game launch
  nsSGv1_YYYYmmdd-HHMMSS_prerestore.sol  safety copy taken before a rollback
  watcher_state.json                     runtime state
  watcher.log                            run log (rotates above 1 MB)
```

This directory sits **outside** the game installation, so the in-game
`D) Delete ALL saves!` and `W) Wipe NEOScavenger data!` menu entries cannot reach it.

If `<game dir>\NSM\` exists, the newest snapshot is additionally mirrored into the
Quicksave slot there (`nsSGv1_g0.sol`). When that folder does not exist, this step is
skipped automatically.

---

## 4. Retention (time-layered)

```
last 2 hours       keep every snapshot
2 h – 48 h         keep one per hour
older than 48 h    keep one per day
hard cap           600 files / 300 MB (oldest dropped first)
```

A snapshot is roughly the size of the save file (1.3 – 1.8 MB as a run progresses).

---

## 5. Everyday management commands

From the toolkit directory:

```bat
python autobackup_setup.py status      task / process / snapshots / slots
python autobackup_setup.py run         run one cycle now (immediate backup)
python autobackup_setup.py seed        copy the newest snapshot into slot 1 + slot 0
python autobackup_setup.py restart     restart after editing neo_save_watcher.py
python autobackup_setup.py uninstall   stop and delete the task (backups kept)
python autobackup_setup.py install     re-enable
```

You can also manage the same task from the Windows **Task Scheduler** GUI.

---

## 6. Troubleshooting

### Is it running?

`auto\watcher_state.json` is rewritten every 10 seconds — check its modification time. Or
run `python autobackup_setup.py status`.

### It does nothing

1. The scheduled task exists and is **Enabled**
2. `pythonw.exe` exists (the toolkit takes it from the directory of the interpreter that
   is currently running the script; if uninstalling or upgrading Python moved it, run
   `install` again to rebuild the task)
3. Look at the last few lines of `auto\watcher.log`

### `read_stable failed` in the log

The save file kept changing while being read (the game was mid-write). This is the guard
working as intended — better to skip a cycle than to write a possibly corrupt snapshot.
The next cycle retries automatically.

### The save path changed

Reinstalling the game, switching Steam library, or a change to `%APPDATA%` all move the
path. Auto-detection normally keeps up; if it does not, run `--check` to see the resolved
result and set `save_dir` explicitly in `config.json`.

---

## 7. Why it is designed this way (mistakes already paid for)

### Mistake 1: a snapshot "cooldown" is harmful — disproved by a real incident

The first version throttled snapshots to one per 30 seconds, reasoning that "the game
writes periodically, so snapshotting every change would flood the folder".
**That reasoning is wrong**, because it assumes a deferred snapshot can still be taken
later. The actual log:

```
17:01:44  snapshot 200,996 B (last one before death, usable)
17:02:04  change to 198,511 B detected, but only 21 s since the last snapshot < 30 s cooldown, deferred
17:02:14  save file gone (character died, the game deleted it)
```

That state was **lost permanently**; the best available rollback was `17:01:44`.

**The correct approach: no cooldown.** Disk risk is instead bounded by time-layered
retention plus a hard total cap. The general rule: *for any "defer the work" optimisation,
ask what happens if the target disappears inside the deferral window.*

### Mistake 2: log spam while the save is missing

Once the save is deleted, the watcher wrote "save unreadable" every 10 seconds — 60 lines
in ten minutes, drowning the log. Changed to **log once per state transition** (once when
it goes missing, once when it comes back).

### Mistake 3: process detection when only the browser goes through a proxy

Process detection uses a Win32 Toolhelp32 snapshot matching on the **exe name**, and does
not depend on `wmic` text output — `wmic` emits GBK, so a script name containing non-ASCII
characters decodes incorrectly and the process is wrongly reported as absent. By the same
token, verifying "the GUI really was created" must rest on encoding-independent evidence
(named mutex + window handle + reverse PID lookup), not on matching command-line text.

### Mistake 4: deployment is not the same as saving a file

After editing `neo_save_watcher.py` you **must** run
`python autobackup_setup.py restart`. To tell whether the change took effect, do not look
only at the file timestamp — also check the **process start time** and the **startup
banner fields**. The banner prints the resolved paths and feature flags
(e.g. `launch_gui=True`).

---

## 8. Why this mechanism exists at all

Before any backup mechanism existed, one death let the game delete the save, and the data
was then physically zeroed by NVMe TRIM within seconds — confirmed **unrecoverable** by
low-level NTFS forensics.

For games like this, where death means deletion, the **single point of value** in a backup
mechanism is capturing "the last save before death". Any optimisation that defers a
snapshot is gambling with exactly that.
