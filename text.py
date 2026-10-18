#!/usr/bin/env python3
"""Gozjaro installer — plain-text front-end (fallback when curses/ncursesw
is unavailable, e.g. TERM=dumb, serial console, or the ISO predates the
ncurses package). Same install flow as tui.py, driven by the same
lib.actions.Installer, just stdin/stdout prompts instead of a widget tree.
"""

from __future__ import annotations

import sys

from lib import actions, disks


def _ask(prompt: str, default: str = "") -> str:
    line = input(f"{prompt} [{default}]: ").strip()
    return line or default


def _confirm(prompt: str, default_yes: bool = False) -> bool:
    tag = "Y/n" if default_yes else "y/N"
    line = input(f"{prompt} [{tag}]: ").strip().lower()
    if not line:
        return default_yes
    return line in ("y", "yes")


def _pick(rows: list[str], title: str) -> int:
    """Return 0-based index, or -1 to go back."""
    print()
    print(title)
    for i, r in enumerate(rows):
        print(f"  [{i + 1}] {r}")
    while True:
        line = input(f"select 1-{len(rows)} (0 to go back): ").strip()
        if line == "0":
            return -1
        if line.isdigit() and 1 <= int(line) <= len(rows):
            return int(line) - 1
        print("  (pick a number in range, or 0)")


def run() -> int:
    inst = actions.Installer(lambda kind, text: print(
        {"step": ">> ", "cmd": "  $ ", "out": "    ", "warn": "!! ",
         "info": "", "done": "== ", "fatal": "!! "}.get(kind, "") + text))
    st = inst.state

    try:
        print("== Gozjaro Linux Installer ==")
        print(">> checking environment + locating live source")
        inst.check_env()
        inst.list_disks()
        if not st.disks:
            print("!! no whole disk >= 1 GiB found to install onto")
            return 1

        while True:
            rows = [f"{d['name']}  {d['size_bytes'] >> 30} GiB  {d.get('model') or 'unknown'}"
                    for d in st.disks]
            i = _pick(rows, "Select the disk to install onto")
            if i < 0:
                continue
            st.disk = st.disks[i]["name"]
            st.total_sectors = inst.disk_total_sectors(st.disk)
            if st.total_sectors < disks.min_disk_sectors(min_root_mib=2):
                print(f"!! {st.disk} is too small; need ~"
                      f"{disks.min_disk_sectors(min_root_mib=2)} sectors")
                continue
            break

        lay = disks.build_layout(st.disk, st.total_sectors)
        esp, root = disks.partition_device_names(st.disk, lay.esp_no, lay.root_no)
        print(f"\nTarget: {st.disk} ({st.total_sectors} sectors)")
        print(f"  {esp}  : 1 MiB .. 513 MiB   FAT32 ESP")
        print(f"  {root} : 513 MiB .. end     ext4  root")
        print("Then: copy system, build initramfs (target's own cpio), install GRUB"
              + (" + NVRAM entry (UEFI)" if st.is_uefi else " (BIOS MBR)"))
        if not _confirm("\nALL DATA ON " + st.disk + " WILL BE DESTROYED. Proceed?",
                       default_yes=False):
            print("!! cancelled — no changes made")
            return 1

        print(">> partitioning + formatting (sgdisk)")
        inst.run_partitioning()

        host = _ask("hostname", "gozjaro")
        tz = _ask("timezone (zoneinfo path, e.g. Asia/Shanghai)", "UTC")
        reboot = _confirm("Reboot into the installed system now?", default_yes=True)

        inst.run_post_partition(host, tz, reboot)
        return 0
    except KeyboardInterrupt:
        print("\n!! interrupted — no changes were made past the last completed step")
        return 130
    except actions.StepError as e:
        print("\n!! " + str(e))
        return 1


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
