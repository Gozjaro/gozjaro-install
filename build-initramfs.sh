#!/bin/bash
# gozjaro-build-initramfs — build an installed-system initramfs *inside* the
# installed target, using the target's own cpio/kmod.
#
# Bash on purpose: process substitution (< <(...)) below is used to feed
# `modprobe -D` output into the module-copy loop without a temp file. The
# target always ships bash (it is the /init interpreter).
#
# The need_bins / module list / /init below mirror gozjaro-bootstrap's
# stages/85-initramfs.sh VERBATIM (the 19 binaries, the 6 sbin-routed names,
# the dual-mode /init) — keep the two in sync when one changes. gzip/ldd/
# readlink are NOT staged: the builder runs inside the target chroot, so it
# uses the target's own tools for packing; only /init's runtime set goes in.
#
# Installed to /usr/sbin/gozjaro-build-initramfs by stage 77; invoked by the
# installer (chroot) before GRUB install. This is the self-bootstrap step the
# live ISO's host-side cpio handoff used to paper over: the target lays its
# own egg with its own tools.
#
# Usage (inside the target's chroot, /proc /sys /dev /run bound):
#   gozjaro-build-initramfs <kernel-version>     e.g. 7.1.8
#
# Produces: /boot/initrd-<kver>.img   (gzip cpio newc)
#
# NOTE on naming: <kver> is the bare module-dir name as it appears under
# /lib/modules (e.g. "7.1.8" — stage 80 stamps /lib/modules/<release> with
# the un-suffixed kernelrelease). GRUB 2.14's 10_linux derives the version
# from the kernel basename (vmlinuz-<kver> -> <kver>) and finds the initrd
# as candidate #2 "initrd-${version}.img", so the initrd MUST be named
# initrd-<kver>.img to match. No extra -gozjaro suffix is appended here.
#
# The /init and module list mirror stages/85-initramfs.sh of
# gozjaro-bootstrap — keep them in sync when one changes.

set -eu

KVER="${1:-}"
[ -n "$KVER" ] || { echo "usage: gozjaro-build-initramfs <kver>" >&2; exit 2; }

MODDIR="/lib/modules/$KVER"
[ -d "$MODDIR" ] || { echo "no kernel modules under $MODDIR" >&2; exit 1; }
[ -f "$MODDIR/modules.dep" ] || { echo "$MODDIR/modules.dep missing" >&2; exit 1; }

command -v cpio >/dev/null 2>&1 || {
    echo "cpio not installed in the target — the ISO is older than the cpio" \
         "package (gozjaro-bootstrap 60.cpio). Rebuild the ISO; this installer" \
         "will not fall back to a host handoff." >&2
    exit 1
}

STAGING=$(mktemp -d /tmp/gozjaro-initramfs.XXXXXX)
trap 'rm -rf "$STAGING"' EXIT INT TERM
echo "  staging: $STAGING"

mkdir -p "$STAGING"/{bin,sbin,lib,lib64,etc,proc,sys,dev,run,newroot}
ln -s bin "$STAGING/usr" 2>/dev/null || true   # merged-usr trick inside the ramfs

# --- binaries ----------------------------------------------------------------
need_bins="seq bash mount umount mkdir sleep cat ls echo ln cp mv rm sed grep
          findmnt blkid switch_root modprobe insmod losetup kmod gzip ldd
          readlink install tar"

resolve_bin() {
    name="$1"
    for p in "/bin/$name" "/usr/bin/$name" "/sbin/$name" "/usr/sbin/$name"; do
        [ -x "$p" ] && { printf '%s' "$p"; return 0; }
    done
    command -v "$name" 2>/dev/null || return 1
}

for name in $need_bins; do
    b=$(resolve_bin "$name" || true)
    [ -n "$b" ] || { echo "  warn: $name missing — skipping"; continue; }
    dest="/bin/$name"
    case "$name" in
        blkid|switch_root|modprobe|insmod|losetup|kmod|gzip|readlink|install|tar)
            dest="/sbin/$name" ;;
    esac
    install -Dm755 "$b" "$STAGING$dest" 2>/dev/null || cp -P "$b" "$STAGING$dest"
done

# --- libraries ----------------------------------------------------------------
copy_lib() {
    lib="$1"
    [ -e "$lib" ] || return 0
    real=$(readlink -f "$lib")
    install -Dm755 "$real" "$STAGING$real" 2>/dev/null || cp -P "$real" "$STAGING$real"
    if [ "$real" != "$lib" ]; then
        mkdir -p "$STAGING$(dirname "$lib")"
        cp -P "$lib" "$STAGING$lib" 2>/dev/null || true
    fi
}

for b in "$STAGING"/bin/* "$STAGING"/sbin/*; do
    [ -x "$b" ] || continue
    ldd "$b" 2>/dev/null | sed 's/^[[:space:]]*//' | awk '/=>/ {print $3}' |
        while read -r path; do
            case "$path" in /*) copy_lib "$path" ;; esac
        done
done
for ld in /lib64/ld-linux-x86-64.so.2 /lib/ld-linux-x86-64.so.2; do
    [ -e "$ld" ] && copy_lib "$ld"
done

# --- modules ------------------------------------------------------------------
# Same set as 85-initramfs: storage/USB/SCSI host controllers so the first
# boot can find the root disk on any VMware controller flavour.
modules="loop squashfs isofs overlay ext4 xfs btrfs
         sr_mod cdrom usb-storage uas
         xhci-hcd ehci-hcd uhci-hcd ohci-hcd
         nvme nvme-core
         sd_mod BusLogic mptspi mptscsih mptbase mpt3sas vmw_pvscsi"

mkdir -p "$STAGING$MODDIR"
cp -a "$MODDIR"/modules.* "$STAGING$MODDIR/" 2>/dev/null || true
for mod in $modules; do
    while read -r verb path; do
        [ "$verb" = "insmod" ] || continue
        [ -n "$path" ] && [ -f "$path" ] || continue
        install -Dm644 "$path" "$STAGING$path" 2>/dev/null || cp -P "$path" "$STAGING$path"
    done < <(modprobe -D -S "$KVER" "$mod" 2>/dev/null || true)
    # shellcheck disable=SC2005
done

# --- /init --------------------------------------------------------------------
# Identical to 85-initramfs.sh's /init (dual live+installed mode).
cat > "$STAGING/init" <<'INIT'
#!/bin/bash
# Gozjaro initramfs /init. Dual mode:
#   Live:     root=live:LABEL=<label>
#   Disk:     root=UUID=<uuid> | root=/dev/<dev>
# No set -e: one failed command must not kill PID 1.

# Standard PATH for the whole boot: the kernel no longer seeds PATH into the
# init environment (HOME=/ and TERM=linux only), and switch_root passes our
# environment to /sbin/init. Without an exported PATH, PID 1 (systemd) has
# none, and every unit with a bare ExecStart (e.g. modprobe@.service's
# `modprobe -abq %i`) fails with "Unable to locate executable".
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

rescue() {
    echo "[gozjaro-initramfs] FAIL: $*"
    echo "[gozjaro-initramfs] last error: $*" > /dev/kmsg 2>/dev/null
    echo "=== gozjaro-initramfs rescue shell; diagnostics: ==="
    echo "    cat /proc/cmdline"
    echo "    ls /dev/sr* /dev/sd* /dev/nvme*"
    echo "    blkid"
    while true; do
        exec /bin/bash
    done
}

echo "[gozjaro-initramfs] starting (PID $$)"
mount -t proc     proc     /proc || rescue "mount /proc"
mount -t sysfs    sysfs    /sys  || rescue "mount /sys"
mount -t devtmpfs devtmpfs /dev  || rescue "mount /dev"

echo "[gozjaro-initramfs] loading modules"
for m in loop squashfs isofs overlay ext4 xfs btrfs \
         sr_mod cdrom usb-storage uas \
         xhci-hcd ehci-hcd uhci-hcd ohci-hcd \
         nvme nvme-core \
         sd_mod BusLogic mptspi mptscsih mptbase mpt3sas vmw_pvscsi; do
    modprobe -q "$m" && echo "  + $m" || echo "  - $m (missing/builtin)"
done

get_cmdline_val() {
    key="$1"
    for tok in $(cat /proc/cmdline); do
        case "$tok" in "${key}="*) printf '%s' "${tok#${key}=}"; return ;; esac
    done
}
ROOT_PARAM=$(get_cmdline_val root)
echo "[gozjaro-initramfs] root=$ROOT_PARAM"

case "$ROOT_PARAM" in
    live:LABEL=*)
        LIVE_LABEL="${ROOT_PARAM#live:LABEL=}"
        echo "[gozjaro-initramfs] mode: live (label $LIVE_LABEL)"
        for i in $(seq 1 15); do
            blkid -L "$LIVE_LABEL" >/dev/null 2>&1 && break
            sleep 1
        done
        LIVE_DEV=$(blkid -L "$LIVE_LABEL" 2>/dev/null)
        if [ -z "$LIVE_DEV" ]; then
            for d in /dev/sr0 /dev/sr1 /dev/sd[a-z] /dev/nvme[0-9]n[0-9]p[0-9]; do
                [ -b "$d" ] || continue
                mkdir -p /run/probe
                if mount -o ro "$d" /run/probe 2>/dev/null; then
                    [ -f /run/probe/live/filesystem.squashfs ] && LIVE_DEV="$d" && umount /run/probe && break
                    umount /run/probe
                fi
            done
        fi
        [ -n "$LIVE_DEV" ] || rescue "no live medium found"
        echo "[gozjaro-initramfs] live device: $LIVE_DEV"
        mkdir -p /run/livecd /run/squashfs /run/overlay /run/newroot
        mount -o ro "$LIVE_DEV" /run/livecd || rescue "mount livecd"
        mount -o loop,ro /run/livecd/live/filesystem.squashfs /run/squashfs || rescue "mount squashfs"
        mount -t tmpfs tmpfs /run/overlay || rescue "mount overlay tmpfs"
        mkdir -p /run/overlay/upper /run/overlay/work
        mount -t overlay overlay -o lowerdir=/run/squashfs,upperdir=/run/overlay/upper,workdir=/run/overlay/work /run/newroot || rescue "mount overlay"
        mkdir -p /run/newroot/run/livecd /run/newroot/run/squashfs /run/newroot/run/overlay
        mount --move /run/livecd   /run/newroot/run/livecd   || true
        mount --move /run/squashfs /run/newroot/run/squashfs || true
        mount --move /run/overlay  /run/newroot/run/overlay  || true
        ;;
    UUID=*)
        WANT_UUID="${ROOT_PARAM#UUID=}"
        echo "[gozjaro-initramfs] mode: real-disk (UUID=$WANT_UUID)"
        ROOT_DEV=""
        for i in $(seq 1 15); do
            ROOT_DEV=$(blkid -U "$WANT_UUID" 2>/dev/null || true)
            [ -n "$ROOT_DEV" ] && break
            sleep 1
        done
        [ -n "$ROOT_DEV" ] || rescue "UUID=$WANT_UUID not found after 15s"
        echo "[gozjaro-initramfs] root device: $ROOT_DEV"
        mkdir -p /run/newroot
        mount "$ROOT_DEV" /run/newroot || rescue "mount $ROOT_DEV"
        ;;
    /dev/*)
        echo "[gozjaro-initramfs] mode: real-disk ($ROOT_PARAM)"
        ROOT_DEV="$ROOT_PARAM"
        for i in $(seq 1 10); do
            [ -b "$ROOT_DEV" ] && break
            sleep 1
        done
        [ -b "$ROOT_DEV" ] || rescue "device $ROOT_DEV not found"
        mkdir -p /run/newroot
        mount "$ROOT_DEV" /run/newroot || rescue "mount $ROOT_DEV"
        ;;
    "")
        rescue "no root= on kernel cmdline — check grub.cfg"
        ;;
    *)
        rescue "unrecognised root= format: $ROOT_PARAM"
        ;;
esac

mkdir -p /run/newroot/dev /run/newroot/proc /run/newroot/sys
mount --move /dev  /run/newroot/dev  || true
mount --move /proc /run/newroot/proc || true
mount --move /sys  /run/newroot/sys  || true

INIT_BIN=""
for candidate in /run/newroot/sbin/init /run/newroot/lib/systemd/systemd \
                 /run/newroot/usr/lib/systemd/systemd; do
    if [ -x "$candidate" ]; then
        INIT_BIN="${candidate#/run/newroot}"
        break
    fi
done
[ -n "$INIT_BIN" ] || rescue "no init found in /run/newroot"

echo "[gozjaro-initramfs] switching root to $INIT_BIN"
exec switch_root /run/newroot "$INIT_BIN"
rescue "switch_root returned"
INIT
chmod 755 "$STAGING/init"

# --- pack -----------------------------------------------------------------------
out="/boot/initrd-$KVER.img"
mkdir -p /boot
( cd "$STAGING" && find . -print0 | cpio --null --create --format=newc ) |
    gzip -9 > "$out"
chmod 644 "$out"
size=$(wc -c < "$out" 2>/dev/null || echo "?")
echo "  wrote $out ($size bytes)"
