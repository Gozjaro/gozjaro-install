#!/usr/bin/env python3
"""Gozjaro installer backend: stateful, audited install steps.

Every destructive action funnels through here so the front-end
(curses TUI or plain text) stays a thin view. All commands run via
subprocess with argument lists (no shell), so disk names with spaces
can never break quoting.

The one non-python actor is the chrooted initramfs builder
(/usr/sbin/gozjaro-build-initramfs, a shell script installed by stage 77)
which packs the target's initramfs with the *target's own cpio* — the
self-bootstrap step the live ISO's host-cpio handoff used to paper over.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import traceback

from . import disks

# ---------------------------------------------------------------------------
# Mount layout (fixed, documented, always cleaned up in reverse order)
# ---------------------------------------------------------------------------
ROOT_MOUNT = "/mnt/install"
ISO_MOUNT = "/mnt/iso"
SRC_MOUNT = "/mnt/src"

LIVE_LABEL = "GOZJARO_LIVE"
ESP_LABEL = "GOZJARO_EFI"
ROOT_LABEL = "GOZJARO_ROOT"

# Kernel/initramfs naming. GRUB 2.14's 10_linux discovers kernels via the
# glob /boot/vmlinuz-* and derives the version as the basename with the
# leading "vmlinuz-" stripped (sed 's,^[^0-9]*-,,g'), then finds the initramfs
# as candidate #2 "initrd-${version}.img". The kver we read from the target's
# /lib/modules is the BARE kernelrelease (e.g. "7.1.8" — stage 80 stamps
# /lib/modules/<release> with the un-suffixed kernelrelease), so we stage:
#   /boot/vmlinuz-<kver>           matches the vmlinuz-* glob  (version=<kver>)
#   /boot/initrd-<kver>.img        = initrd-${version}.img, candidate #2
# build-initramfs.sh receives the same bare <kver> and emits initrd-<kver>.img,
# so kernel + initrd + module-dir all agree. Bare names from the old ISO
# ("vmlinuz"/"initrd.img") match NO 10_linux glob — that is the exact bug that
# produced an empty boot menu on the first installed boot. Idempotent: kver is
# used verbatim, so passing "7.1.8" never yields a bare "vmlinuz".
def kernel_name(kver: str) -> str:
    return f"vmlinuz-{kver}"

def initrd_name(kver: str) -> str:
    return f"initrd-{kver}.img"


class StepError(Exception):
    """Fatal installer error; carries a human-readable message for the UI."""


class InstallState:
    """Everything the front-end needs to show + what cleanup needs to undo."""

    def __init__(self) -> None:
        self.is_uefi = os.path.isdir("/sys/firmware/efi")
        self.disks: list = []          # [{name,size_bytes,model,rota}]
        self.disk: str = ""            # whole-disk chosen by the user
        self.total_sectors: int = 0
        self.layout = None             # disks.GPTLayout
        self.esp_dev = ""
        self.root_dev = ""
        self.root_uuid = ""
        self.esp_uuid = ""
        self.kver = ""
        self.hostname = "gozjaro"
        self.tz = "UTC"
        self.src = ""            # populated by check_env()
        self.iso = ""

    @property
    def plan_ok(self) -> bool:
        return bool(self.disk and self.total_sectors > 0)


class Installer:
    """Drives a full install. Front-ends call step_* methods in order and
    relay `log(kind, text)` lines into their view."""

    def __init__(self, log_fn, progress_fn=None, dry_run: bool = False):
        self.log = log_fn                       # log(kind, text)
        self.progress = progress_fn            # progress(done, total) or None
        self.dry_run = dry_run                 # record commands, do not exec
        self.state = InstallState()
        self._ran_cmds: list = []               # audit trail of exec'd cmds
        # mount bookkeeping for cleanup (reverse-order on exit)
        self._mounts: list = []                 # (mountpoint, how)
        self._chroot_binds: list = []          # (src, dst) in target
        self._live_unmounts: list = []         # our own live mounts

    # ------------------------------------------------------------------
    # execution primitives
    # ------------------------------------------------------------------
    def _run(self, argv, *, stream: bool = False, check: bool = True):
        """Run argv (list). stream=True relays output lines to log('cmd',..).
        Returns (rc, captured_text)."""
        self.log("cmd", "$ " + " ".join(a if len(a) < 48 else a[:45] + "..." for a in argv))
        if self.dry_run:
            self._ran_cmds.append(argv)
            return 0, ""
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
        except FileNotFoundError as e:
            raise StepError(f"cannot exec {argv[0]}: {e}")
        out_lines = []
        if stream:
            for line in proc.stdout:
                out_lines.append(line.rstrip())
                self.log("out", line.rstrip())
            rc = proc.wait()
        else:
            data, _ = proc.communicate()
            out_lines = data.splitlines()
            rc = proc.returncode
        if check and rc != 0:
            tail = "\n".join(out_lines[-12:])
            raise StepError(f"{argv[0]} failed (rc={rc}):\n{tail}")
        return rc, "\n".join(out_lines)

    def _sh(self, *argv, stream=False, check=True):
        return self._run(list(argv), stream=stream, check=check)

    # ------------------------------------------------------------------
    # step 0 — environment + source
    # ------------------------------------------------------------------
    REQUIRED_HOST_TOOLS = ("sgdisk", "mkfs.vfat", "mkfs.ext4", "blkid",
                           "wipefs", "rsync", "cpio", "grub-install",
                           "partprobe", "efibootmgr", "python3")

    def check_env(self):
        missing = [t for t in self.REQUIRED_HOST_TOOLS
                   if shutil.which(t) is None]
        if missing:
            self.log("warn", "missing tools: " + ", ".join(missing))
        self.state.is_uefi = os.path.isdir("/sys/firmware/efi")
        self.log("info", "UEFI firmware: " + ("yes" if self.state.is_uefi else "no (BIOS path)"))
        # live source
        if os.path.isdir("/run/squashfs") and os.listdir("/run/squashfs"):
            self.state.src = "/run/squashfs"
            self.state.iso = "/run/livecd"
            self.log("info", "reusing live mounts (source=/run/squashfs)")
            return
        live = self._sh("blkid", "-L", LIVE_LABEL, check=False)[1].strip()
        if not live:
            # fall back to probing cdrom/sd devices
            for cand in ("/dev/sr0", "/dev/sr1", "/dev/sda"):
                if os.path.exists(cand):
                    live = cand
                    break
        if not live:
            raise StepError("cannot find the live medium (label " + LIVE_LABEL + ")")
        os.makedirs(ISO_MOUNT, exist_ok=True)
        self._sh("mount", "-o", "ro", live, ISO_MOUNT)
        self._live_unmounts.append(ISO_MOUNT)
        squash = f"{ISO_MOUNT}/live/filesystem.squashfs"
        if not os.path.isfile(squash):
            self._sh("umount", ISO_MOUNT, check=False)
            raise StepError(f"{live} is not the Gozjaro live medium (no {squash})")
        os.makedirs(SRC_MOUNT, exist_ok=True)
        self._sh("mount", "-o", "ro,loop", squash, SRC_MOUNT)
        self._live_unmounts.append(SRC_MOUNT)
        self.state.src = SRC_MOUNT
        self.state.iso = ISO_MOUNT
        self.log("info", f"live source loop-mounted at {SRC_MOUNT}")
        self.state.is_uefi = os.path.isdir("/sys/firmware/efi")

    # ------------------------------------------------------------------
    # step 1 — disk discovery
    # ------------------------------------------------------------------
    def list_disks(self):
        _, js = self._sh("lsblk", "-d", "-b", "-J",
                        "-o", "NAME,SIZE,TYPE,MODEL,ROTA")
        disks_list = disks.parse_lsblk_block_devices(js)
        # only physical whole disks with a plausible size for ESP+root (>=1GiB)
        disks_list = [d for d in disks_list
                      if d["size_bytes"] >= 1024 * 1024 * 1024]
        disks_list.sort(key=lambda d: d["name"])
        self.state.disks = disks_list
        self.log("info", f"{len(disks_list)} candidate disk(s)")
        return disks_list

    def disk_total_sectors(self, dev: str) -> int:
        _, out = self._sh("blockdev", "--getsz", dev)
        return int(out.strip() or 0)

    # ------------------------------------------------------------------
    # step 2/3 — partition + format (sgdisk, not parted)
    # ------------------------------------------------------------------
    def partition_and_format(self, dev: str, total_sectors: int):
        s = self.state
        s.disk = dev
        s.total_sectors = total_sectors
        s.layout = disks.build_layout(dev, total_sectors)
        s.esp_dev, s.root_dev = disks.partition_device_names(
            dev, s.layout.esp_no, s.layout.root_no)

        self.log("step", "partitioning " + dev + " (GPT via sgdisk)")
        self._sh("wipefs", "-a", dev, check=False)
        for cmd in disks.all_partition_commands(s.layout):
            # sgdisk prints its own confirmation lines; relay them
            argv = cmd.split()
            self._run(argv, stream=True)

        # make the kernel see the new table
        self._sh("partprobe", dev, check=False)
        for _ in range(10):
            if os.path.exists(s.root_dev) and os.path.exists(s.esp_dev):
                break
            self._sh("sleep", "1", check=False)
        for d in (s.esp_dev, s.root_dev):
            if not os.path.exists(d):
                raise StepError(f"partition {d} did not appear after partprobe")

        self.log("step", "formatting " + s.esp_dev + " (vfat) and " + s.root_dev + " (ext4)")
        for cmd in disks.format_specs(s.layout, ESP_LABEL, ROOT_LABEL):
            argv = cmd.split()
            self._run(argv, stream=True)

        s.root_uuid = self._sh("blkid", "-s", "UUID", "-o", "value", s.root_dev)[1].strip()
        s.esp_uuid = self._sh("blkid", "-s", "UUID", "-o", "value", s.esp_dev)[1].strip()
        if not s.root_uuid:
            raise StepError("cannot read UUID of " + s.root_dev)
        self.log("info", f"root uuid={s.root_uuid}  esp uuid={s.esp_uuid}")

    # ------------------------------------------------------------------
    # step 4 — mount target
    # ------------------------------------------------------------------
    def mount_target(self):
        s = self.state
        os.makedirs(ROOT_MOUNT, exist_ok=True)
        self._sh("mount", s.root_dev, ROOT_MOUNT)
        self._mounts.append(ROOT_MOUNT)
        if s.is_uefi:
            os.makedirs(f"{ROOT_MOUNT}/boot/efi", exist_ok=True)
            self._sh("mount", s.esp_dev, f"{ROOT_MOUNT}/boot/efi")
            self._mounts.append(f"{ROOT_MOUNT}/boot/efi")
        self.log("info", "target mounted at " + ROOT_MOUNT)

    # ------------------------------------------------------------------
    # step 5 — copy root
    # ------------------------------------------------------------------
    def copy_root(self):
        s = self.state
        self.log("step", "copying root filesystem (this takes a few minutes)")
        self._sh("rsync", "-aHAXx",
                 "--exclude=/run", "--exclude=/tmp",
                 "--exclude=/proc", "--exclude=/sys", "--exclude=/dev",
                 f"{s.src}/", f"{ROOT_MOUNT}/", stream=True)
        # THE /run BUG (2026-10-18 first installed boot): the squashfs has no
        # /run (stage 90 excludes it), rsync --exclude=/run keeps it absent,
        # and systemd's mount_setup() then fails "Failed to mount tmpfs on
        # /run: No such file or directory" -> "Failed to mount API
        # filesystems" -> freezing. Create the API-filesystem mountpoints.
        for d in ("run", "dev", "proc", "sys", "tmp", "boot"):
            os.makedirs(f"{ROOT_MOUNT}/{d}", exist_ok=True)
        self.log("info", "root filesystem copied; API-filesystem mountpoints created")

    # ------------------------------------------------------------------
    # step 6 — kernel + config + fstab
    # ------------------------------------------------------------------
    def install_kernel_and_config(self, hostname: str, tz: str):
        s = self.state
        moddir = f"{ROOT_MOUNT}/lib/modules"
        if not os.path.isdir(moddir):
            raise StepError("no " + moddir + " in the target (rsync incomplete?)")
        kvers = [d for d in os.listdir(moddir) if os.path.isdir(f"{moddir}/{d}")]

        def vkey(v):
            # "7.1.8-gozjaro" -> (7, 1, 8) ; non-numeric tails sort last
            parts = re.findall(r"\d+", v)
            return (tuple(int(p) for p in parts) if parts else (0,), v)

        kvers.sort(key=vkey)
        if not kvers:
            raise StepError("no kernel under " + moddir)
        s.kver = kvers[-1]
        self.log("info", "kernel " + s.kver)

        boot = f"{ROOT_MOUNT}/boot"
        os.makedirs(boot, exist_ok=True)
        # kernel comes from the ISO (built by stage 80, renamed bare by 90);
        # we re-stage it under the versioned name GRUB's 10_linux requires.
        shutil.copy2(f"{s.iso}/boot/vmlinuz", f"{boot}/{kernel_name(s.kver)}")
        os.chmod(f"{boot}/{kernel_name(s.kver)}", 0o644)

        s.hostname = hostname
        s.tz = tz
        self._write_fstab()
        with open(f"{ROOT_MOUNT}/etc/hostname", "w") as f:
            f.write(hostname + "\n")
        hosts = ("127.0.0.1\tlocalhost\n127.0.1.1\t%s localhost\n"
                 "::1\tlocalhost ip6-localhost ip6-node\n" % hostname)
        with open(f"{ROOT_MOUNT}/etc/hosts", "w") as f:
            f.write(hosts)
        tzfile = f"{ROOT_MOUNT}/usr/share/zoneinfo/{tz}"
        link = f"{ROOT_MOUNT}/etc/localtime"
        if os.path.isfile(tzfile):
            if os.path.lexists(link):
                os.remove(link)
            os.symlink(f"/usr/share/zoneinfo/{tz}", link)
            with open(f"{ROOT_MOUNT}/etc/timezone", "w") as f:
                f.write(tz + "\n")
        else:
            self.log("warn", f"timezone {tz} not found, using UTC")
        # systemd regenerates machine-id on first boot when it is empty
        open(f"{ROOT_MOUNT}/etc/machine-id", "w").close()
        # strip live-only overrides so the installed system prompts for login
        for drop in (f"{ROOT_MOUNT}/etc/systemd/system/getty@tty1.service.d/autologin.conf",
                     f"{ROOT_MOUNT}/etc/systemd/journald.conf.d/live.conf"):
            if os.path.isfile(drop):
                os.remove(drop)
        self.log("info", "fstab/hostname/timezone written, live overrides removed")

    def _write_fstab(self):
        s = self.state
        lines = [
            f"# generated by gozjaro-install {self._stamp()}",
            f"UUID={s.root_uuid}\t/\text4\tnoatime\t0\t1",
        ]
        if s.is_uefi:
            lines.append(f"UUID={s.esp_uuid}\t/boot/efi\tvfat\tumask=0077\t0\t2")
        lines += [
            "proc\t/proc\tproc\tdefaults\t0\t0",
            "sysfs\t/sys\tsysfs\tnosuid,noexec,nodev\t0\t0",
            "devpts\t/dev/pts\tdevpts\tgid=5,mode=620\t0\t0",
            "tmpfs\t/run\ttmpfs\tnosuid,nodev\t0\t0",
            "tmpfs\t/dev/shm\ttmpfs\tnosuid,nodev\t0\t0",
        ]
        with open(f"{ROOT_MOUNT}/etc/fstab", "w") as f:
            f.write("\n".join(lines) + "\n")

    @staticmethod
    def _stamp() -> str:
        import datetime
        return datetime.date.today().isoformat()

    # ------------------------------------------------------------------
    # step 7 — chroot: build the initramfs (the chicken lays the egg)
    # ------------------------------------------------------------------
    def _bind_chroot_fs(self):
        s = self.state
        for src, dst in (("/dev", f"{ROOT_MOUNT}/dev"),
                         ("/dev/pts", f"{ROOT_MOUNT}/dev/pts"),
                         ("/proc", f"{ROOT_MOUNT}/proc"),
                         ("/sys", f"{ROOT_MOUNT}/sys"),
                         ("/run", f"{ROOT_MOUNT}/run")):
            self._sh("mount", "--bind", src, dst)
            self._chroot_binds.append((src, dst))
        # efibootmgr inside the chroot needs the live efivarfs visible
        if s.is_uefi:
            host_efivars = "/sys/firmware/efi/efivars"
            if os.path.isdir(host_efivars):
                tgt = f"{ROOT_MOUNT}{host_efivars}"
                os.makedirs(tgt, exist_ok=True)
                self._sh("mount", "--bind", host_efivars, tgt)
                self._chroot_binds.append((host_efivars, tgt))

    def build_initramfs(self):
        s = self.state
        self.log("step", f"building initramfs for kernel {s.kver} (in target, with its own cpio)")
        self._bind_chroot_fs()
        self._run(["chroot", ROOT_MOUNT, "/usr/sbin/gozjaro-build-initramfs", s.kver],
                  stream=True)
        out = f"{ROOT_MOUNT}/boot/{initrd_name(s.kver)}"
        if not os.path.isfile(out):
            raise StepError(out + " was not produced by the chroot build")
        self.log("info", "initramfs built: " + out)

    # ------------------------------------------------------------------
    # step 8 — bootloader
    # ------------------------------------------------------------------
    def install_grub(self):
        s = self.state
        self.log("step", "installing GRUB" + (" (UEFI)" if s.is_uefi else " (BIOS)"))
        bootdir = f"{ROOT_MOUNT}/boot/grub"
        os.makedirs(bootdir, exist_ok=True)
        cfg = "/boot/grub/grub.cfg"

        def chroot(cmd: str):
            # single shell string, run inside the target
            return self._run(["chroot", ROOT_MOUNT, "/usr/bin/env", "-i",
                              "HOME=/root", "PATH=/usr/bin:/usr/sbin:/bin:/sbin",
                              "/bin/sh", "-c", cmd], stream=True)

        if s.is_uefi:
            # ORDER MATTERS: mkconfig first, THEN install. GRUB's EFI
            # grub-install embeds the generated grub.cfg into the ESP; running
            # it before mkconfig leaves the ESP image booting with a stale/
            # empty config (the "menu has no Linux entry" symptom).
            chroot("grub-mkconfig -o /boot/grub/grub.cfg")
            chroot("grub-install --target=x86_64-efi --efi-directory=/boot/efi "
                   "--bootloader-id=Gozjaro --recheck")
            # register + make current the NVRAM entry
            chroot("efibootmgr -v || true")
            self._set_current_boot_entry()
        else:
            chroot("grub-mkconfig -o /boot/grub/grub.cfg")
            chroot(f"grub-install --target=i386-pc --recheck {s.disk}")
        self.log("info", "bootloader installed")

    def _set_current_boot_entry(self):
        """efibootmgr -b <the Gozjaro entry>: the new entry is added as
        BootNext only, so without this the firmware still boots the live
        ISO after reboot."""
        _, out = self._sh("chroot", ROOT_MOUNT, "efibootmgr", "-v", check=False)
        m = re.search(r"(Boot[0-9A-F]+)\s+Gozjaro", out)
        if m:
            self._sh("chroot", ROOT_MOUNT, "efibootmgr", "-b", m.group(1),
                     check=False)
            self.log("info", "NVRAM current boot entry set: " + m.group(1))
        else:
            self.log("warn", "could not find the Gozjaro NVRAM entry; the "
                             "firmware boot order may still point at the live ISO")

    # ------------------------------------------------------------------
    # finalise
    # ------------------------------------------------------------------
    def finalise(self, reboot: bool):
        s = self.state
        for src, dst in reversed(self._chroot_binds):
            self._sh("umount", dst, check=False)
        self._chroot_binds.clear()
        for mnt in reversed(self._mounts):
            self._sh("umount", mnt, check=False)
        self._mounts.clear()
        for mnt in reversed(self._live_unmounts):
            self._sh("umount", mnt, check=False)
        self._live_unmounts.clear()
        self._sh("sync")
        self.log("done", "installation complete: " + s.disk + " (" + s.kver + ")")
        if reboot:
            self._sh("systemctl", "reboot", check=False)

    # ------------------------------------------------------------------
    # orchestration (used by both front-ends)
    # ------------------------------------------------------------------
    def run_partitioning(self):
        self.partition_and_format(self.state.disk, self.state.total_sectors)

    def run_post_partition(self, hostname: str, tz: str, reboot: bool):
        self.mount_target()
        self.copy_root()
        self.install_kernel_and_config(hostname, tz)
        self.build_initramfs()
        self.install_grub()
        self.finalise(reboot)

    def audit_trail(self) -> str:
        return "\n".join("  $ " + " ".join(c) for c in self._ran_cmds)
