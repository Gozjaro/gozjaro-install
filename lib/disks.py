#!/usr/bin/env python3
"""Pure, testable disk-layout logic for the Gozjaro installer.

Nothing in here touches the kernel: it builds *commands* (sgdisk) and
*filesystem specs* that the TUI backend executes. Keeping it pure means we
can unit-test the GPT geometry and the exact sgdisk invocations without a
running VM.

Partition plan (GPT, x86_64 UEFI; also valid for a legacy-BIOS boot via the
protected-MBR + LegacyBIOSBootable attribute on p1):

    p1  ESP   1 MiB .. 513 MiB   FAT32   GUID C12A7328-... (EFI)   LegacyBIOSBootable
    p2  root  513 MiB .. end     ext4    GUID 0FC63DAF-... (Linux)

Why sgdisk and not parted: the live image's parted 3.6 segfaults in its
geometry-probe/exit path on some virtual SCSI disks (reproduced: even
`parted --version` aborts after printing). sgdisk (util-linux, already in
the live tree) is the robust, scriptable GPT tool and is what LFS ships.
"""

from __future__ import annotations
import re
from dataclasses import dataclass
from typing import List, Tuple

# GPT partition type GUIDs (upper-case, canonical).
ESP_GUID   = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"   # EFI System
LINUX_GUID = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"   # Linux filesystem

SECTOR = 512
ESP_START_SECTOR = 2048          # 1 MiB
ESP_SIZE_MIB     = 512
GPT_TAIL_RESERVE = 33            # backup GPT + protective MBR, sgdisk reserves it


@dataclass
class GPTLayout:
    disk: str
    esp_no: int
    root_no: int
    esp_start: int
    esp_end: int
    root_start: int
    root_end: int
    esp_guid: str
    root_guid: str


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
def esp_end_sector(esp_mib: int = ESP_SIZE_MIB) -> int:
    """Inclusive end sector of the ESP given 512-byte sectors."""
    return ESP_START_SECTOR + int(esp_mib * (1024 * 1024) / SECTOR) - 1


def root_start_sector(esp_mib: int = ESP_SIZE_MIB) -> int:
    return esp_end_sector(esp_mib) + 1


def root_end_sector(total_sectors: int) -> int:
    """Last usable sector for the root partition.

    sgdisk reserves 33 sectors at the tail for the backup GPT header +
    protective MBR; a partition must not run past total-33.
    """
    if total_sectors <= 0:
        raise ValueError("total_sectors must be positive")
    usable = total_sectors - GPT_TAIL_RESERVE
    if usable <= 0:
        raise ValueError("disk too small for a GPT layout")
    return usable


def build_layout(disk: str, total_sectors: int,
                 esp_no: int = 1, root_no: int = 2) -> GPTLayout:
    layout = GPTLayout(
        disk=disk,
        esp_no=esp_no,
        root_no=root_no,
        esp_start=ESP_START_SECTOR,
        esp_end=esp_end_sector(),
        root_start=root_start_sector(),
        root_end=root_end_sector(total_sectors),
        esp_guid=ESP_GUID,
        root_guid=LINUX_GUID,
    )
    # Hard invariant: the ESP must sit entirely inside the disk and the root
    # must have at least one sector. `root_end = total - 33` stays positive
    # for anything larger than 33 sectors, so check the two bounds explicitly.
    if layout.esp_end >= total_sectors:
        raise ValueError(
            f"disk too small: {total_sectors} sectors cannot hold the "
            f"512 MiB ESP (ends at sector {layout.esp_end})")
    if layout.root_end < layout.root_start:
        raise ValueError(
            f"disk too small: no room left for the root partition "
            f"(need at least sector {layout.root_start}, got total "
            f"{total_sectors})")
    return layout


def min_disk_sectors(esp_mib: int = ESP_SIZE_MIB, min_root_mib: int = 1) -> int:
    """Smallest disk (sectors) that fits the ESP + a min-size root."""
    return root_start_sector(esp_mib) + int(min_root_mib * (1024 * 1024) / SECTOR)


# ---------------------------------------------------------------------------
# sgdisk command generation (pure strings — the backend runs them)
# ---------------------------------------------------------------------------
def zap_commands(disk: str) -> List[str]:
    """Erase all partitioning on the device.

    `sgdisk --zap-all` wipes the GPT headers, the partition table and the
    protective MBR signature, leaving the disk with no partitioning. A GPT
    (and its protective MBR) is re-created automatically when the first
    `--new` below runs on an unpartitioned device — so we do NOT need a
    separate protective-MBR step (running one before any GPT exists would
    fail)."""
    return [
        f"sgdisk --zap-all {disk}",
    ]


def partition_commands(layout: GPTLayout) -> List[str]:
    """Create p1 (ESP) and p2 (root). sgdisk `--new=no:start:end` is
    inclusive on both ends; `--type` accepts the `EFI`/`L` keywords, but we
    spell the GUIDs explicitly so the command is self-documenting and
    independent of sgdisk's keyword table."""
    d = layout.disk
    return [
        (f"sgdisk --new={layout.esp_no}:{layout.esp_start}:{layout.esp_end} "
         f"--type={layout.esp_no}:{layout.esp_guid} "
         f"--change-name={layout.esp_no}:ESP "
         f"--set-boot={layout.esp_no} {d}"),
        (f"sgdisk --new={layout.root_no}:{layout.root_start}:{layout.root_end} "
         f"--type={layout.root_no}:{layout.root_guid} "
         f"--change-name={layout.root_no}:root {d}"),
    ]


def format_specs(layout: GPTLayout, esp_uuid: str, root_uuid: str) -> List[str]:
    """FAT32 ESP (label GOZJARO_EFI) + ext4 root (label GOZJARO_ROOT).

    The UUID placeholders are filled by the backend after mkfs via blkid.
    ESP device node: nvme/mmcblk use p-suffixed minors, sd* don't.
    """
    esp_dev, root_dev = partition_device_names(layout.disk,
                                               layout.esp_no, layout.root_no)
    return [
        f"mkfs.vfat -F32 -n GOZJARO_EFI {esp_dev}",
        f"mkfs.ext4 -L GOZJARO_ROOT -F {root_dev}",
    ]


def partition_device_names(disk: str, esp_no: int, root_no: int) -> Tuple[str, str]:
    """Map a whole-disk name + partition numbers to device nodes.

    nvmeXnY and mmcblkX take a `p` between the whole-disk name and the
    minor (nvme0n1p1); SCSI/SATA/USB (sdX) do not (sda1).
    """
    if re.search(r"(nvme[0-9]+n[0-9]+|mmcblk[0-9]+)$", disk):
        esp, root = f"{disk}p{esp_no}", f"{disk}p{root_no}"
    else:
        esp, root = f"{disk}{esp_no}", f"{disk}{root_no}"
    return esp, root


def all_partition_commands(layout: GPTLayout) -> List[str]:
    """Full, ordered partitioning sequence the backend runs (no mkfs)."""
    return zap_commands(layout.disk) + partition_commands(layout)


# ---------------------------------------------------------------------------
# Parsing `lsblk` / `blockdev` output (backend supplies raw strings)
# ---------------------------------------------------------------------------
def parse_lsblk_block_devices(raw: str) -> List[dict]:
    """Return candidate whole disks.

    The backend feeds the JSON form: ``lsblk -d -b -J -o NAME,SIZE,TYPE,
    MODEL,ROTA``. JSON is the canonical input because the MODEL column is
    whitespace-padded in plain-text lsblk (a multi-word model like "VMware
    Virtual S" collapses under a single-space split), so we parse the
    structured output instead. A whitespace fallback is kept for testing
    / hand-built strings; it treats field 4 as a single-word model only.

    A candidate is a physical whole-disk: TYPE == 'disk', not a loop
    device. Partitions (TYPE 'part'), CD-ROMs ('rom') and loopback are
    excluded. Sizes are bytes when the backend passes ``-b``.
    """
    import json
    out: List[dict] = []
    raw = (raw or "").lstrip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
        for dev in data.get("blockdevices", []):
            if _is_candidate(dev.get("TYPE"), dev.get("NAME")):
                out.append(_from_lsblk_dev(dev))
        return out

    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        name, size, ptype = parts[0], parts[1], parts[2]
        if not _is_candidate(ptype, name):
            continue
        out.append({"name": name,
                    "size_bytes": int(size) if size.isdigit() else 0,
                    "model": parts[3] if len(parts) > 3 else ""})
    return out


def _is_candidate(ptype: str, name: str) -> bool:
    if not ptype or ptype != "disk":
        return False
    if not name or name.startswith("loop"):
        return False
    return True


def _from_lsblk_dev(dev: dict) -> dict:
    name = dev.get("NAME", "")
    return {
        "name": name,
        "size_bytes": int(dev.get("SIZE", 0) or 0),
        "model": dev.get("MODEL") or "",
    }


def total_sectors_from_blockdev(raw: str) -> int:
    """`blockdev --getsz DEV` prints a single sector count (512-byte units)."""
    m = re.search(r"\d+", raw or "")
    return int(m.group(0)) if m else 0


# ---------------------------------------------------------------------------
# CLI subcommands (used by the bash backend to exec the exact commands)
#   part <disk> <total_sectors>   -> the sgdisk partitioning commands
#   fmt  <disk> <total_sectors>   -> the mkfs format commands
#   plan <disk> <total_sectors>   -> both, partition commands first
# ---------------------------------------------------------------------------
def main(argv: List[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print("usage: disks.py {part|fmt|plan} <disk> <total_sectors>")
        return 2
    sub, disk, sectors = argv[1], argv[2], int(argv[3])
    lay = build_layout(disk, sectors)
    if sub == "part":
        cmds = all_partition_commands(lay)
    elif sub == "fmt":
        cmds = format_specs(lay, "", "")
    elif sub == "plan":
        cmds = all_partition_commands(lay) + format_specs(lay, "", "")
    else:
        print(f"unknown subcommand: {sub}")
        return 2
    for c in cmds:
        print(c)
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv))
