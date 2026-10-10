# Idle SD card

This card uses one GPT partition formatted as exFAT, labelled `idlebox`.
Applications and games share this partition. Keep one installed system in
`system/`; do not create a `releases/` directory.

**Immediately after `iroll card init`, this guide and empty `games/states/` and `games/saves/`
directories are present.** Initialization does not install applications, firmware,
games, BIOS files or an empty catalogue.
The tree below describes the expected layout after installing a compatible
system bundle and preparing games separately.

## Expected layout

```text
SD root/
├── INSTRUCTIONS.md                 # This guide; refreshed by init and rollout.
├── system/                         # Required: the installed application bundle.
│   ├── iman/                       # Session manager and its configuration.
│   │   ├── iman                    # Required: starts the GUI and games.
│   │   ├── iman.json               # Optional: session, display and input settings.
│   │   └── emulation.json          # Required for games: emulator/core paths and settings.
│   ├── igui/                       # Game browser and its resources.
│   │   ├── igui                    # Required: the user interface executable.
│   │   ├── locales/                # Optional translations; English is built in.
│   │   │   └── ru.json             # Russian translation.
│   │   └── controls/               # Required for normal Linux controller input.
│   │       ├── controller-hid.json # Example: HID profile; filename is arbitrary.
│   │       └── controller-evdev.json # Example: evdev profile; filename is arbitrary.
│   └── retroarch/                  # Required for games: emulation runtime.
│       ├── retroarch               # Emulator frontend executable.
│       └── cores/                  # Matching libretro core libraries.
│           ├── nestopia_libretro.so # Example: NES core.
│           └── picodrive_libretro.so # Example: Sega multi-system core.
├── games/                          # Game library prepared on a PC with icat.
│   ├── catalogue.json              # Required: game index read by IGUI.
│   ├── images/                     # Required for games: ROM data, NOT pictures.
│   │   └── NES/ab/Game.nes          # Example: platform / SHA-256 prefix / original name.
│   ├── thumbs/                     # Optional: game preview images.
│   │   └── ab/<sha256>.png          # Preview named by the ROM's full SHA-256 hash.
│   ├── states/                     # Explicit emulator save states.
│   │   └── <sha256>.state           # Slots may add a suffix, e.g. .state1.
│   └── saves/                      # Ordinary game saves, SaveRAM/SRAM.
│       └── <sha256>.srm             # Named by the ROM hash, not its display name.
├── prefs.json                      # Optional: GUI language, favourites and hidden games.
└── iman-reports/                    # Optional: explicitly enabled diagnostic exports.
```

`NES`, `ab`, `Game.nes` and `<sha256>` are examples, not literal directories or
files to create. `ab` represents the first two hexadecimal characters of the
ROM's SHA-256. Keep paths and letter case exactly as recorded in the catalogue.
Use extracted, supported ROMs; do not put ZIP/7z archives in place of indexed ROMs.
Only use games and other content you are entitled to use.

Current iroll builds provide Nestopia (NES/FDS), Gambatte (GB/GBC),
Snes9x 2010 (SNES), gpSP (GBA), PicoDrive (MD/SMS/GG/SG1000/SC3000/SCD/32X/PICO)
and PCSX ReARMed (PSX). SCD means Sega CD; PSX means PlayStation 1.
PicoDrive replaces Genesis Plus GX for new Mega Drive assignments. Old core
files are not automatically deleted; save states from different cores must not
be assumed compatible. Core availability is not proof of game performance on
RK3032. icat 0.3.0 imports self-contained PSX/SCD CHDs with automatic platform
detection and PC-side libchdr; 32X/PICO/SC3000 and other disc formats remain
unsupported. CHD import does not verify game launch, multidisc switching or SBI support.
Only self-contained single-file game images are in scope. Multi-file sets
such as CUE with external tracks or M3U with separate discs are not supported
and are not planned as part of the new-core integration. Single-file formats
still need explicit importer and launch support; not every such format is
currently accepted. ZIP/7z are input packaging for PC-side extraction of one
supported image, not runtime media. Separate BIOS files are allowed; BIOS
and ROMs must be supplied separately by the owner.

Paths inside `system/iman/emulation.json` are relative to `system/`, for example
`retroarch/retroarch` and `retroarch/cores/nestopia_libretro.so`. They are not
relative to the JSON file. Component entries contain paths, not SHA-256 pins.
The root-level `settings` object contains native RetroArch option names and typed
JSON values. Rollout regenerates this object from the platform profile on every
full or partial update, replacing previous settings by default. Storage safety
options remain mandatory; runtime paths are managed by iman. `iman.json`
configures the session; it is not a build inventory.

New rollouts also supply Sharp Bilinear Simple GLSL resources for known
platform/core pairs. Shared, unmodified upstream files live under
`system/retroarch/shaders/libretro/interpolation/`; referenced platform presets
live under `system/retroarch/shaders/presets/<PLATFORM>.glslp`.
`cores.<PLATFORM>.shader_preset` is a path relative to `system/`, passed by iman
as an explicit `--set-shader` argument. Missing or null means shaders are disabled
at runtime. Automatic preset discovery remains disabled.

Partial updates retain an explicit preset path or null for a reused core; an
absent field receives the shipped default. Replacing a core resets its platform
options. Use null to retain an explicit disable across partial updates, or
`--ask` to keep an edited preset or the whole manifest. Missing supplied resources
are restored without rebuilding RetroArch. Unknown user files are not deleted.
The selected main preset must exist, be regular/single-link and stay on the same
filesystem without symlinks. RetroArch resolves nested references and checks GLSL
compatibility; performance on RK3032 is not yet verified. Update iman and the
manifest/resources together: an older iman ignores the new field.

For Nestopia/FDS, rollout also supplies `library_name: "Nestopia"` and a `remap`
object in the FDS core entry: player 1 RetroPad X becomes L (change disk side),
Y becomes R (switch disks 1/2 for multi-disk images). NES keeps its normal turbo
buttons. These are logical inputs, independent of USB button numbers.
The FDS defaults are regenerated on partial updates too, but never applied to a
foreign core assigned to FDS. Other reused platform options are retained.
Declining the manifest replacement with `--ask` preserves the old file entirely.

Update both iman and `emulation.json` to enable this feature. No persistent RMP
files are deployed: iman prepares the selected remap in its private RAM runtime
and removes it after the game. Automatic remap saving remains disabled. In-game
control changes are temporary; edit the JSON on the PC for persistent changes.
The profile has offline coverage, not a confirmed RK3032 disk-side-switch test.

IGUI reads all immediate `controls/*.json` beside its executable, not from the
working directory. Filenames are arbitrary; each JSON declares its backend,
USB identity, exact device name and bindings. Profiles are not embedded in the
binary. They are read once at startup; editing them needs a restart, not a rebuild.
Rollout copies all immediate source `controls/*.json` and requires at least one,
without a list of mandatory filenames or controller models. IGUI, not iroll,
validates profile contents and limits: 32 KiB per file, 64 JSON files, 512 KiB of
JSON in total and 256 directory entries. Missing/empty profile directories,
invalid JSON and unsupported backends stop normal startup.

Profiles matching the same USB identity and exact name within one backend are
ambiguous. IGUI warns once per conflicting group and excludes only that group's
backend profiles; it still starts. Unique profiles for another backend remain
usable. Automatic joydev fallback is unchanged and uses kernel mappings without
JSON. Rollout does not delete profiles missing from the source: after a rename,
an old installed JSON can remain and conflict with the new one.

Use `--controls DIRECTORY` for another directory. `--hid-profile FILE` replaces
all HID profiles with one explicit file; `--evdev-profile FILE` does the same for
evdev. With both overrides, the directory is not read. Headless and explicit
joydev input need no controller JSON. The raw input probe uses all unambiguous
evdev profiles in the directory, not a particular board model; at least one
evdev profile must be declared, even if conflicts leave none active.
These settings control IGUI, not RetroArch game inputs.

There is no `iroll.json`, `.iroll.lock` or collected `licenses/` directory on the
card. Source notices and distribution obligations remain with their components.

## Preparing the contents

- Install a bundle built for your device. PC binaries are not ARM stick binaries.
  `iroll rollout --platform rk3032 --dest /path/to/card` builds release components
  on the PC, then overlays the selected files. Defaults relative to the working
  directory are `in/components/{igui,iman}` for local projects (symlinks allowed),
  `in/downloads/` for source archives and `in/sdk/rk3032/` for the SDK.
  Without `--dest`, the result goes to `out/`; build data and reports stay in
  `tmp/build/` and `logs/` on the PC. Ready files are assembled under
  `tmp/out_stage/system/` before publication; this stage is then removed.
  See the iroll manual for the build layout and explicit path overrides.
  The destination is an ordinary directory;
  iroll does not check that it is a mounted card or mount/unmount it for you.
- Rollout replaces changed supplied files by default, including `iman.json`,
  `emulation.json`, controller JSON, translations and this guide. Add `--ask` to confirm each
  replacement in an interactive terminal: `y` or `Y` replaces the file; Enter
  or any other answer keeps it. New files need no confirmation; identical files
  are skipped. All questions precede publication; Ctrl+C there cancels it.
  Declining `emulation.json` keeps the whole manifest, not just its settings;
  check compatibility when retaining old configuration alongside new components.
  Unrelated files, especially `prefs.json` and `games/catalogue.json`, are never
  replaced or deleted by rollout. File/directory conflicts are rejected rather
  than removing user directories. Keep your own configuration backup if needed.
- The RK3032 manager bundle sets DRM compatibility in `system/iman/iman.json`.
  By default its `drm,diagnostics` build starts all compiled passive collectors
  and saves reports automatically after games and at orderly exit. Build and
  publish iman with `iroll rollout --platform rk3032 --with-iman --no-diag`
  (and your usual `--dest`) to omit the collectors and their automatic exports.
  This selects `drm` only; RGB565, ordinary error messages and release optimization
  remain enabled. Rebuild without `--no-diag` to restore diagnostics. The flag
  does not affect IGUI, RetroArch or cores; native PC builds already omit these
  diagnostics. If iman is not selected, or its replacement is declined with
  `--ask`, the installed manager is unchanged. There are no runtime diagnostic
  enable/export switches.
  Unknown iman startup arguments before `--` are ignored with one warning,
  without a legacy-name registry. The manager infers its installation root and loads
  system/iman/iman.json automatically; file guards and private RAM runtime for
  logs and configuration stay mandatory.
  Diagnostic option changes alone do not require rewriting NOR when the executable
  path and the rest of the bundle remain compatible. No permission marker files
  are needed. Old
  `BOX2-ALLOW.txt` and `BOX2-EXPORT.txt` files are ignored by current applications;
  rollout leaves them untouched. No stock-menu trampoline or diagnostic games
  are installed.
- Prepare the library with `icat` on a PC. Preserve `games/states/` and
  `games/saves/` when updating it; do not replace the whole directory by deleting
  its old contents. Merely dropping ROM files onto the card does not update the index.
- Keep `prefs.json` in the card root, not inside `games/`. It may be absent initially.
  The GUI writes preferences only when permitted by the selected runtime profile.
- Keep existing `games/states/`, `games/saves/` and `prefs.json` when updating
  applications. Init and rollout prepare both save directories without replacing
  their contents; game launch does not create them.
- Old root-level `states/` and `saves/` remain untouched. Current iman uses
  `games/states/` and `games/saves/` instead; iroll does not move old saves.
- Keep source downloads, build data and PC operation journals off the card.
  Runtime logs, temporary configuration and caches belong in device RAM.

## Ordinary game saves

The owner-approved profiles set `autosave_interval = 60`: RetroArch checks the
core's SaveRAM/RTC once a minute and writes changed data. Compatible iman uses
`--sram-mode load-save`, loading previous data at game start and saving current
data on normal exit too. No separate SaveRAM action in IGUI is required. This is
an explicit policy exception allowing background SD writes, not a Save State.

Files use native RetroArch names `games/saves/<ROM-SHA256>.srm` and, for cores
with RTC, `games/saves/<ROM-SHA256>.rtc`. Names follow the complete ROM hash,
not its display name. iman passes this persistent directory directly; it does
not merely copy a possibly stale RAM file after the game. Update both iman and
`emulation.json`; keeping an old manifest with `--ask` keeps its old interval.
State snapshots remain under `games/states/`, with automatic state saving and
loading disabled. Configuration and remap saving on exit remain disabled too:
`config_save_on_exit` controls configuration, not SRAM.

Power loss can discard progress since the last successful write, commonly the
last minute or longer when writes fail or caches have not reached storage.
Neither the timer nor normal exit confirms `fsync`/`syncfs`, atomic file replacement
or a return to read-only mounting. An interrupted write can damage saves or the
filesystem. This mode is **not power-safe**; RK3032 hardware behavior has not
been verified separately. The read-only policy remains deferred.

## Platform BIOS

Supply your own firmware under `in/bios/<PLATFORM>/<file>` in the iroll working
directory. Every rollout, including partial application updates, overlays these
files into `system/bios/<PLATFORM>/<file>`; `--bios DIR` changes the input root.
Existing replacements follow `--ask`; identical files are skipped. Missing inputs
do not delete installed BIOS files. Firmware is never downloaded or included in
the iroll package. Platform directories use uppercase identifiers matching the
catalogue. Regular files and nested directories are supported; symlinks,
hardlinks and special files are rejected. Empty directories are not published.
No BIOS-specific RAM budget, file-count or depth limits are imposed; the common
iroll artifact size limit (256 MiB per file) still applies.

FDS uses the existing Nestopia core and requires `system/bios/FDS/disksys.rom`
containing exactly 8192 bytes. An iNES cartridge named after the BIOS is not this
raw file and must not simply be renamed. Missing BIOS does not stop building the
applications. The core, not iroll or iman, checks firmware availability and contents.

iman passes the selected platform directory directly as `system_directory`.
The core reads firmware and subdirectories from SD, without copying them to RAM.
There is no filename list, required-file registry or exact-size configuration.
With no BIOS directory, iman supplies an empty runtime directory in RAM instead
of creating a directory on SD or falling back to the ROM directory.
Adding files for an already supported platform needs no manifest change or
iman rebuild. It does not install cores or add supported platforms.

SaveRAM/RTC uses `games/saves/` as described above. Other core-specific writes
are not made safe by this layout. The BIOS directory is not a read-only mount;
while SD is RW, a core can write
there; direct access with this limitation was approved by the owner.
The GUI displays launch failure status without requiring a terminal.

## Safety and compatibility

**Running `iroll card init` again destroys all existing partitions and files on
the selected card, including games, preferences and saves.** It is a quick
format, not a secure erase: old data may remain recoverable. No backup is made.
After explicit confirmation, card init automatically unmounts filesystems on
that card, rechecks its identity and stops before partitioning if unmount fails.
Do not eject or power off the reader as a substitute for unmounting.

Rollout stages only changed files and publishes the emulation manifest last.
An interrupted publication may leave a partially updated application bundle;
there is no whole-release rollback or power-loss guarantee. Stop applications
before updating. On failure, inspect the PC build report before retrying.

Wait for successful synchronization and unmounting before removing the card.
After initialization, disconnect and reconnect the reader before further use;
the old partition layout is intentionally not reloaded during the operation.
On failure or cancellation, keep the PC journal and inspect the reported state;
do not assume the card is ready or retry automatically.

This data card does not contain a bootloader or operating system. Initialization
does not prove that your firmware supports this partition layout or that the
installed applications will run. Firmware must start `system/iman/iman`; firmware
that starts the old flat `system/iman` file must be rebuilt before using this
layout. Older BOX2 diagnostic builds
require their original card geometry and cannot use this layout unchanged.

Read-only mounting from first boot and controlled write intervals are not yet
guaranteed by the current runtime. A successful format or a low write count
does not make removing power during normal use safe. Do not interrupt an
explicit save or another write operation.
