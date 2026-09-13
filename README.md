# neoscav-save-guard

**A save guard for NEO Scavenger — because the game deletes your save when you die.**

NEO Scavenger is permadeath. When your character dies, the game does not merely mark the
save as dead — it **deletes `nsSGv1.sol` outright**. There is no in-game rollback, and no
trash can to recover from. This toolkit keeps a continuous, timestamped history of that
file *outside* the game directory, so a death costs you minutes instead of a run.

- A **background watcher** snapshots the save on game start and on *every* save change.
- A **rollback GUI** (auto-launched when you start the game) restores any snapshot.
- Retains history by time layers, not "last N files", so a burst of writes cannot
  wipe out your older history.

Windows only. No third-party dependencies (standard library + tkinter).

---

## The problem this solves

| | |
|---|---|
| Save file | `nsSGv1.sol` (Flash SharedObject) |
| Location | `%APPDATA%\Macromedia\Flash Player\#SharedObjects\<random>\localhost\<game path>\` |
| On death | the game **deletes** the file |
| In-game backup | none |
| Windows Recycle Bin | not involved — the file is unlinked, not trashed |

Measured on real hardware: after deletion, the data was physically zeroed by NVMe TRIM
within seconds and could not be recovered with NTFS forensics. **If you want a save back,
something must have already copied it out.**

## What it does

```
Scheduled Task (at logon, hidden window, pythonw.exe)
  └─ neo_save_watcher.py — polls every 10 s
       ├─ game process starts        → snapshot (reason=start) + launch the rollback GUI
       ├─ nsSGv1.sol content changes → snapshot (reason=save)
       ├─ optional: mirror newest snapshot into the NSM Quicksave slot
       └─ prune old snapshots by time layer
```

Because it watches the **file and the process**, it works no matter how you launch the
game (Steam, desktop shortcut, anything).

## Requirements

- Windows 10 / 11
- Python 3.8+ **with tkinter** (the official python.org installer includes it)
- NEO Scavenger installed locally

## Install

```bat
git clone https://github.com/DRDRDRRDDRDR/neoscav-save-guard.git
cd neoscav-save-guard

:: 1) verify that the save directory is found automatically
python neo_save_watcher.py --check

:: 2) register the background watcher (starts immediately, no re-login needed)
python autobackup_setup.py install
```

`--check` prints how each path was resolved and flags anything it could not find. If
auto-detection fails, copy `config.example.json` to `config.json` and fill in the paths.

## Uninstall

```bat
python autobackup_setup.py uninstall     :: removes the task; your backups are kept
```

## Path resolution

Paths are **never hard-coded**. Resolution order, highest first:

1. Environment variables — `NSG_SAVE_DIR`, `NSG_GAME_DIR`, `NSG_BACKUP_ROOT`
2. `config.json` (next to the scripts, or passed with `--config <file>`)
3. Automatic detection

Auto-detection works because Flash stores local SWF objects under
`#SharedObjects\<random>\localhost\<full exe path>\`. Scanning for `nsSGv1.sol` therefore
locates the save directory, and the **game directory is reverse-derived** from that same
path — so a different username, drive letter, or Steam library needs no edits.

## Command line

**Watcher** — `neo_save_watcher.py`

| Flag | Meaning |
|---|---|
| *(none)* | run resident (this is what the scheduled task uses) |
| `--check` | print path resolution + environment probe, then exit |
| `--once` | run a single poll cycle, then exit |
| `--dry-run` | with `--once`: report only, write nothing |
| `--config FILE` | use a specific `config.json` |
| `--interval N` | poll interval in seconds (default 10) |

**Setup** — `autobackup_setup.py`

| Subcommand | Meaning |
|---|---|
| `install` | register the scheduled task and start it |
| `uninstall` | delete the task (backups are left alone) |
| `status` | task state, process, snapshot count, NSM slots |
| `run` | run one watcher cycle now |
| `seed` | copy the newest snapshot into NSM slots (`--slots 1,0`) |
| `restart` | restart the task — **required after editing the watcher** |

**Rollback** — `存档回档器.pyw` (double-click for the GUI)

| Flag | Meaning |
|---|---|
| `--list` | list snapshots with time, size and integrity |
| `--status` | task / save / snapshot summary |
| `--restore <name\|latest>` | restore a snapshot over the live save |
| `--restore-slot <n>` | copy a snapshot into NSM slot *n* |
| `--snapshot` / `--label` | pick the source snapshot / label for a slot write |
| `--yes` | skip the confirmation prompt |
| `--kill-game` | kill `NEOScavenger.exe` first (see safety rule 1) |
| `--config FILE` | use a specific `config.json` |

## Rollback safety

1. **Rollback is refused while the game is running.** Flash holds the save in memory and
   overwrites `nsSGv1.sol` on exit — a rollback done during play would be silently undone,
   and you would conclude "rollback doesn't work". The GUI offers to end the process.
2. **The current save is preserved first**, as `*_prerestore.sol`, so a rollback is itself
   reversible.
3. **Snapshot integrity is checked before writing.** The `.sol` header is verified
   (magic `00 BF` + a big-endian length field that must equal `filesize - 6`); a truncated
   snapshot is flagged in the GUI and refused. Restoring a corrupt snapshot would give you
   a save the game cannot load — a confusing failure that looks like "rollback broke the game".

## Retention

```
last 2 hours     keep every snapshot
2 h – 48 h       keep one per hour
older            keep one per day
hard cap         600 files / 300 MB (oldest dropped first)
```

A snapshot is roughly the size of the save file (about 1.3–1.8 MB as a run progresses).

## Design notes (the non-obvious parts)

- **Stable reads.** While the game is running, Flash may be mid-write. `read_stable()`
  reads the file twice and only accepts it if `(mtime, size, content)` are identical —
  otherwise the cycle is skipped. Better to miss a snapshot than to write a corrupt one.
- **No snapshot cooldown.** An early version throttled snapshots to one per 30 s. That was
  wrong, and it cost a real save: the log showed a change detected at `17:02:04` being
  deferred, then at `17:02:14` the file was gone. *Any* "defer execution" optimisation must
  ask what happens if the target disappears inside the deferral window. Disk usage is
  bounded by the retention policy instead.
- **Time-layered retention**, not "keep last N": with frequent writes, "last 50 files"
  can span only a few minutes.
- **Edge-triggered logging.** After a death the save is missing; logging that every 10 s
  would bury the log. It is logged once on the transition, in both directions.
- **Single-instance GUI.** The watcher may launch the rollback GUI on every game start, so
  the GUI takes a named kernel mutex (`ERROR_ALREADY_EXISTS` → focus the existing window
  and exit). A mutex is used rather than a process scan because a scan cannot distinguish
  this script from any other Python process, and because scanning races with spawning.
- **Detached spawn.** The GUI is started with `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`
  so it does not die with the watcher.
- **Deployment discipline.** Editing `neo_save_watcher.py` requires
  `python autobackup_setup.py restart`; a resident process does not hot-reload. The startup
  banner echoes the resolved paths and feature flags, so the log tells you which build is
  actually running.

## Disclaimer

Not affiliated with or endorsed by Blue Bottle Games. *NEO Scavenger* is their property.
This tool only reads and writes files in your own user profile; it does not modify the
game. The optional NSM slot mirroring writes only inside `<game>\NSM\` and is skipped
entirely when that folder does not exist. Use at your own risk — keep your own backups too.

## License

MIT. See [LICENSE](LICENSE).

---

## 中文说明

**NEO Scavenger 是永久死亡制，角色一死，游戏会直接删除存档文件 `nsSGv1.sol`** ——
不是标记为死亡，是删掉。游戏内没有回档，回收站里也没有它（是解除链接，不是移到回收站）。
实测：删除后数据在几秒内被 NVMe TRIM 物理抹零，NTFS 取证也无法恢复。
**想拿回存档，必须在删除之前就已经有副本存在。**

本工具做的事：在游戏目录**之外**持续保存带时间戳的存档历史。

- 后台监视器：游戏启动时、存档每次变化时，立刻快照一份
- 回档图形界面：**开游戏时自动弹出**，可把任意快照还原成当前存档
- 按时间分层保留，而不是"只留最近 N 份"——后者在频繁写盘时可能只覆盖几分钟

**因为监视的是文件与进程，所以从 Steam、桌面还是任何方式启动游戏都生效。**

### 安装

```bat
git clone https://github.com/DRDRDRRDDRDR/neoscav-save-guard.git
cd neoscav-save-guard

python neo_save_watcher.py --check      :: 先确认能自动找到存档目录
python autobackup_setup.py install      :: 注册后台监视器（立即启动，无需重新登录）
```

### 卸载

```bat
python autobackup_setup.py uninstall    :: 只删计划任务，备份文件保留
```

### 路径配置

**代码里不写死任何路径**，解析顺序为：环境变量 → `config.json` → 自动探测。
自动探测的原理是 Flash 会把本地 SWF 的存档放在
`#SharedObjects\<随机串>\localhost\<游戏 exe 全路径>\` 下，
所以搜 `nsSGv1.sol` 就能定位存档目录，并**从同一路径反推出游戏目录** ——
换用户名、换盘符、换 Steam 库都不需要改代码。

探测失败时，把 `config.example.json` 复制为 `config.json` 并填写即可。

### 回档的三条安全机制

1. **游戏运行中拒绝回档。** Flash 把存档握在内存里，退出时会用内存内容覆盖文件，
   此时回档会被抹掉，而用户只会以为"回档没用"。界面会问你是否结束游戏进程。
2. **覆盖前先把当前存档另存为 `*_prerestore.sol`**，让回档本身也能再回退。
3. **回档前校验 `.sol` 头部结构**（魔数 `00 BF` + 大端长度字段 == 文件大小 − 6）。
   被截断的快照会标红并拒绝回档 —— 否则游戏读不出存档，
   现象只是"回档后游戏打不开"，很难定位。

### 常用命令

```bat
python autobackup_setup.py status       :: 任务/进程/快照/槽位
python autobackup_setup.py restart      :: 改过 neo_save_watcher.py 后重启生效
python neo_save_watcher.py --check      :: 查看路径解析明细

:: 回档器（双击即 GUI）
存档回档器.pyw --list                   :: 列出全部快照（含完整性）
存档回档器.pyw --restore latest --yes   :: 回档到最新一份
存档回档器.pyw --status                 :: 查看状态
```

> **改完代码必须重启才生效**：常驻进程不会自动加载新代码。
> 判断线上跑的是不是新版本，看 `auto\watcher.log` 里的启动横幅 ——
> 它会打印解析出的路径与功能开关。
