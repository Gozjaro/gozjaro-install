# gozjaro-install

The Gozjaro Linux installer: a curses TUI (with a plain-text fallback) that
partitions, formats and installs a Gozjaro live ISO to a disk, builds the
target's own initramfs **inside the target's chroot with the target's own
cpio** (the self-bootstrap step), and installs GRUB — UEFI NVRAM entry
registration included.

Shipped *from* this repo into the Gozjaro live ISO: `gozjaro-bootstrap`
stage 20 fetches this repository and stage 77 installs it into the live
tree. It is no longer a file inside the bootstrap repo.

## Layout

```
gozjaro-install            entry shim (-> /usr/bin/gozjaro-install)
lib/disks.py               pure GPT geometry + sgdisk command generation
lib/actions.py             stateful install steps (the only module that execs)
tui.py                     curses front-end
text.py                    plain-text front-end (fallback)
build-initramfs.sh         /usr/sbin/gozjaro-build-initramfs (chroot builder)
test/test_disks.py         host-runnable unittest for the pure layers
```

## Why this shape

- **sgdisk, not parted.** The live image's parted 3.6 segfaults in its
  geometry-probe/exit path on some virtual SCSI disks. sgdisk (util-linux,
  already in the live tree) is the robust GPT tool; `lib/disks.py` emits the
  exact command strings, and they are unit-tested against a real 21.5 GiB
  sfdisk dump (sectors 2048 / 1050623 / 1050624 / 41943007).
- **Kernel/initramfs naming that GRUB actually globs.** GRUB 2.14's
  `10_linux` only discovers `/boot/vmlinuz-*`. The installer stages
  `vmlinuz-<kver>` + `initrd-<kver>.img` (initrd candidate #2 in
  `10_linux`), never the bare `vmlinuz`/`initrd.img` that produced the
  first empty boot menu.
- **The /run bug.** The squashfs has no `/run`; systemd's API-filesystem
  setup then fails ("No such file or directory") and the installed system
  freezes at first boot. `actions.copy_root` creates all API-filesystem
  mountpoints after rsync.
- **Grub order.** On UEFI, `grub-mkconfig` must run *before* `grub-install`:
  the EFI image embeds the generated config, so the old order shipped a
  stale/empty config into the ESP.
- **chroot initramfs.** `build-initramfs.sh` mirrors the module list and
  dual-mode `/init` of bootstrap `stages/85-initramfs.sh`; keep the two in
  sync when one changes. It refuses to run if cpio is missing (pre-cpio ISOs)
  rather than silently handing off to the host.

## Test

```
python3 test/test_disks.py
```

## Build wiring (gozjaro-bootstrap)

- stage 20 fetches this repo (pinned, checksummed) into `$SOURCES_DIR`.
- stage 77 (chroot) installs: `gozjaro-install` → `/usr/bin`,
  `lib/ tui.py text.py` → `/usr/share/gozjaro-install/`,
  `build-initramfs.sh` → `/usr/sbin/gozjaro-build-initramfs`.
