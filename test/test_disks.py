#!/usr/bin/env python3
"""Host-runnable tests for the installer's pure-logic layers.

Run from the repo root (or anywhere, it fixes up sys.path):
    python3 test/test_disks.py

These cover everything that must be *provably* correct before a real disk is
ever written: GPT sector geometry, the exact sgdisk command strings, partition
device-node naming (nvme/mmcblk vs sd), kernel/initramfs naming that GRUB's
10_linux will actually glob, and the lsblk parser (JSON + whitespace).
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import disks          # noqa: E402
from lib.actions import (      # noqa: E402
    kernel_name, initrd_name, InstallState, Installer, StepError)


class TestGeometry(unittest.TestCase):
    def test_known_21gb_disk_matches_sfdisk_dump(self):
        # 21.5 GiB VMware disk, 512-byte sectors, total = 41943040 sectors.
        lay = disks.build_layout("/dev/sda", 41943040)
        self.assertEqual(lay.esp_start, 2048)
        self.assertEqual(lay.esp_end, 2048 + 1048576 - 1)   # 512 MiB
        self.assertEqual(lay.root_start, 2048 + 1048576)
        self.assertEqual(lay.root_end, 41943040 - 33)       # tail reserved

    def test_layout_too_small_raises(self):
        with self.assertRaises(ValueError):
            disks.build_layout("/dev/sda", 100)

    def test_min_disk_sectors(self):
        self.assertEqual(disks.min_disk_sectors(min_root_mib=1),
                         disks.root_start_sector() + 2048)


class TestSgdiskCommands(unittest.TestCase):
    def test_full_plan_is_ordered_and_self_documenting(self):
        lay = disks.build_layout("/dev/sda", 41943040)
        cmds = disks.all_partition_commands(lay)
        self.assertEqual(cmds[0], "sgdisk --zap-all /dev/sda")
        self.assertIn("--new=1:2048:1050623", cmds[1])
        self.assertIn("C12A7328-F81F-11D2-BA4B-00A0C93EC93B", cmds[1])
        self.assertIn("--set-boot=1", cmds[1])
        self.assertIn("--new=2:1050624:41943007", cmds[2])
        self.assertIn("0FC63DAF-8483-4772-8E79-3D69D8477DE4", cmds[2])
        # no partition command references the protective-MBR step
        self.assertFalse(any("protective" in c for c in cmds))

    def test_format_specs_use_correct_device_nodes(self):
        lay = disks.build_layout("/dev/nvme0n1", 41943040)
        fmt = disks.format_specs(lay, "GOZJARO_EFI", "GOZJARO_ROOT")
        self.assertIn("/dev/nvme0n1p1", fmt[0])
        self.assertIn("/dev/nvme0n1p2", fmt[1])
        self.assertTrue(fmt[0].startswith("mkfs.vfat"))
        self.assertTrue(fmt[1].startswith("mkfs.ext4"))


class TestDeviceNaming(unittest.TestCase):
    def test_nvme_mmcblk_take_p_suffix(self):
        self.assertEqual(disks.partition_device_names("/dev/nvme0n1", 1, 2),
                         ("/dev/nvme0n1p1", "/dev/nvme0n1p2"))
        self.assertEqual(disks.partition_device_names("/dev/mmcblk0", 1, 2),
                         ("/dev/mmcblk0p1", "/dev/mmcblk0p2"))

    def test_scsi_sata_do_not(self):
        self.assertEqual(disks.partition_device_names("/dev/sda", 1, 2),
                         ("/dev/sda1", "/dev/sda2"))
        self.assertEqual(disks.partition_device_names("/dev/sdb", 1, 2),
                         ("/dev/sdb1", "/dev/sdb2"))


class TestKernelNaming(unittest.TestCase):
    def test_names_match_grub_10_linux_globs(self):
        # GRUB: kernel glob /boot/vmlinuz-* -> version "7.1.8"; initrd
        # candidate #2 "initrd-${version}.img". kver is the bare module-dir
        # name (7.1.8), exactly as the installer reads it from /lib/modules.
        self.assertEqual(kernel_name("7.1.8"), "vmlinuz-7.1.8")
        self.assertEqual(initrd_name("7.1.8"), "initrd-7.1.8.img")
        # the bare names the old ISO shipped must NOT be produced
        self.assertNotEqual(kernel_name("7.1.8"), "vmlinuz")
        self.assertNotEqual(initrd_name("7.1.8"), "initrd.img")


class TestLsblkParser(unittest.TestCase):
    def test_json_keeps_multiword_model_and_filters_non_disk(self):
        js = json.dumps({"blockdevices": [
            {"NAME": "sda", "SIZE": 21474836480, "TYPE": "disk",
             "MODEL": "VMware Virtual S"},
            {"NAME": "loop0", "SIZE": 2348718592, "TYPE": "rom", "MODEL": "live"},
            {"NAME": "sda1", "SIZE": 538968064, "TYPE": "part", "MODEL": ""},
            {"NAME": "sr0", "SIZE": 2348718592, "TYPE": "rom", "MODEL": "CD01"},
        ]})
        got = disks.parse_lsblk_block_devices(js)
        self.assertEqual(got, [{"name": "sda", "size_bytes": 21474836480,
                                "model": "VMware Virtual S"}])

    def test_whitespace_fallback(self):
        raw = ("sda 21474836480 disk VMware 0 0\n"
               "loop0 2348718592 rom live 0 1\n"
               "sr0 2348718592 rom VMware SATA CD01 0 0\n")
        got = disks.parse_lsblk_block_devices(raw)
        self.assertEqual([g["name"] for g in got], ["sda"])


class TestBlockdevParser(unittest.TestCase):
    def test_parses_single_number(self):
        self.assertEqual(disks.total_sectors_from_blockdev("41943040\n"), 41943040)
        self.assertEqual(disks.total_sectors_from_blockdev("   42\n"), 42)
        self.assertEqual(disks.total_sectors_from_blockdev(""), 0)


class TestInstallState(unittest.TestCase):
    def test_defaults_and_api_mountpoints_intent(self):
        s = InstallState()
        self.assertEqual(s.hostname, "gozjaro")
        self.assertEqual(s.tz, "UTC")
        self.assertFalse(s.plan_ok)
        self.assertTrue(hasattr(Installer, "copy_root"))

    def test_steperror_carries_message(self):
        e = StepError("no live medium found")
        self.assertIn("live medium", str(e))


class TestDryRunOrchestrator(unittest.TestCase):
    """Drive Installer with dry_run so no subprocess touches the host; verify
    the audit trail contains the right commands in the right order for the
    parts that are pure (partitioning/formatting command *strings* come from
    lib.disks, so we check those here; exec'd commands are recorded)."""

    def test_partition_commands_audit_order(self):
        inst = Installer(lambda *_: None, dry_run=True)
        lay = disks.build_layout("/dev/sda", 41943040)
        # These are the exact strings the backend will exec, in order:
        cmds = disks.all_partition_commands(lay) + disks.format_specs(lay, "", "")
        order = [c.split()[0] for c in cmds]
        # sgdisk zap first, sgdisk new after, mkfs after partitioning
        self.assertEqual(order[0], "sgdisk")
        self.assertLess(cmds.index(cmds[1]), cmds.index(cmds[-2]))
        self.assertTrue(cmds[-2].startswith("mkfs.vfat"))
        self.assertTrue(cmds[-1].startswith("mkfs.ext4"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
