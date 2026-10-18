#!/usr/bin/env python3
"""Gozjaro installer — curses TUI front-end (top-level module `tui`).

Thin over `lib.actions.Installer`: the install is a small state machine of
modes (log / list / input / confirm). curses is optional: the
`gozjaro-install` shim imports this module and falls back to `text` when
`import curses` fails (dumb TERM, missing ncursesw).
"""

from __future__ import annotations

import sys
from typing import List, Optional

from lib import actions

# Import hard: if ncursesw/_curses is missing this module simply fails to
# import, and the gozjaro-install shim falls back to the text front-end.
# (No try/except here — a half-imported curses would be worse than none.)
import curses  # noqa: E402


# ---------------------------------------------------------------------------
# small curses helpers
# ---------------------------------------------------------------------------
class UI:
    def __init__(self, scr):
        self.scr = scr
        scr.nodelay(False)
        scr.curs_set(0)
        scr.keypad(True)
        if hasattr(scr, "border"):
            try:
                scr.border()
            except curses.error:
                pass
        self.h, self.w = scr.getmaxyx()

    def title(self, text: str):
        full = (" Gozjaro — " + text)[: self.w - 2].ljust(self.w - 2)
        try:
            self.scr.addnstr(1, 1, full, self.w - 4)
        except curses.error:
            pass
        self.scr.refresh()

    def status(self, text: str):
        try:
            self.scr.addnstr(self.h - 2, 1, text[: self.w - 2], self.w - 2)
        except curses.error:
            pass
        self.scr.refresh()

    def key(self) -> int:
        try:
            return self.scr.getch()
        except KeyboardInterrupt:
            return -1


def _init_colors():
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)    # step / done
    curses.init_pair(2, curses.COLOR_CYAN, -1)     # info
    curses.init_pair(3, curses.COLOR_YELLOW, -1)   # highlight / warn
    curses.init_pair(4, curses.COLOR_RED, -1)      # fatal
    curses.init_pair(5, curses.COLOR_WHITE, -1)    # out


def _scrollbox(scr, lines: List[str], top: int, height: int,
               cursor: int = -1, first: int = 0) -> int:
    """Render `lines` into body rows [top, top+height). cursor==-1 pins the
    view to the tail (log mode); otherwise the view scrolls to keep `cursor`
    visible. Returns the new `first` so the caller can remember it across
    frames (curses windows are C objects — we must not stash state on them)."""
    if height <= 0:
        return first
    h, w = scr.getmaxyx()
    height = min(height, h - top - 2)
    n = len(lines)
    if n == 0:
        return first
    if cursor < 0 or cursor >= n:
        first = max(0, n - height)
    else:
        if cursor < first:
            first = cursor
        elif cursor >= first + height:
            first = cursor - height + 1
    for i in range(height):
        row = top + i
        idx = first + i
        if idx < n:
            text = lines[idx][: w - 3]
            highlight = cursor >= 0 and idx == cursor
            attr = curses.color_pair(3) if highlight else curses.A_NORMAL
            try:
                scr.addnstr(row, 1, text, w - 3, attr)
            except curses.error:
                pass
    return first


def _blank_body(scr, top: int, height: int):
    h, w = scr.getmaxyx()
    for r in range(top, min(top + height, h - 2)):
        try:
            scr.addnstr(r, 1, " " * (w - 2), w - 3)
        except curses.error:
            pass


_KIND_PREFIX = {"step": "► ", "cmd": "  $ ", "out": "    ", "warn": "!! ",
                "info": "  ", "done": "✓ ", "fatal": "✗ "}


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------
class TUI:
    def __init__(self, scr):
        _init_colors()
        self.ui = UI(scr)
        self.inst = actions.Installer(self._on_log)
        self.lines: List[str] = []
        scr.first = 0

    def _on_log(self, kind: str, text: str):
        self.lines.append(_KIND_PREFIX.get(kind, "  ") + text)
        self.lines = self.lines[-2000:]

    def _log_view(self, prompt: str):
        body_h = self.ui.h - 4
        _blank_body(self.ui.scr, 2, body_h)
        _scrollbox(self.ui.scr, self.lines, 2, body_h, cursor=-1)
        self.ui.status(prompt)

    def _list_prompt(self, rows: List[str], title: str) -> int:
        """Return the selected index, or -1 on cancel."""
        sel = 0
        body_h = self.ui.h - 4
        self.ui.title(title)
        while True:
            view_h = body_h
            first = 0
            if sel >= first + view_h:
                first = sel - view_h + 1
            _blank_body(self.ui.scr, 2, view_h)
            shown = [f"{('*' if i == sel else ' ')} {i + 1:>2}. {r}"
                     for i, r in enumerate(rows)]
            first = _scrollbox(self.ui.scr, shown, 2, view_h, cursor=sel)
            self.ui.status("↑/↓ move   ENTER select   ESC cancel")
            c = self.ui.key()
            if c in (27, ord("q"), ord("Q")):
                return -1
            if c == curses.KEY_UP:
                sel = max(0, sel - 1)
            elif c in (curses.KEY_DOWN, curses.KEY_NPAGE):
                sel = min(len(rows) - 1, sel + (10 if c == curses.KEY_NPAGE else 1))
            elif c == curses.KEY_PPAGE:
                sel = max(0, sel - 10)
            elif c in (10, 13, curses.KEY_ENTER):
                return sel

    def _text_input(self, label: str, default: str = "") -> Optional[str]:
        self.ui.title(label)
        buf = default
        while True:
            _blank_body(self.ui.scr, 2, 1)
            self.ui.scr.addnstr(2, 1, label[: self.ui.w - 3])
            try:
                self.ui.scr.addstr(3, 1, "> " + buf)
            except curses.error:
                pass
            self.ui.status("ENTER accept   BACKSPACE delete   ESC keep default")
            c = self.ui.key()
            if c in (10, 13, curses.KEY_ENTER):
                return buf if buf else default
            if c == 27:
                return default
            if c in (127, curses.KEY_BACKSPACE, curses.KEY_DC, 8):
                buf = buf[:-1]
            elif c >= 32 and c < 127:
                buf += chr(c)

    def _confirm(self, question: str, default_yes: bool = False) -> bool:
        self.ui.title("Confirm")
        while True:
            _blank_body(self.ui.scr, 2, 6)
            for r, line in enumerate(question.splitlines()):
                try:
                    self.ui.scr.addnstr(2 + r, 1, line[: self.ui.w - 3])
                except curses.error:
                    pass
            opts = "[Y]es   [n]o" if default_yes else "[y]es   [n]o"
            try:
                self.ui.scr.addnstr(2 + question.count("\n") + 2, 1, opts)
            except curses.error:
                pass
            self.ui.status("y / n / ESC (= default)")
            c = self.ui.key()
            if c in (ord("y"), ord("Y")):
                return True
            if c in (ord("n"), ord("N")):
                return False
            if c == 27:
                return default_yes

    # ------------------------------------------------------------------
    def run(self) -> int:
        try:
            return self._run()
        except KeyboardInterrupt:
            self._on_log("fatal", "interrupted — no changes were made past the last completed step")
            self._log_view("aborted")
            self.ui.key()
            return 130
        except actions.StepError as e:
            self._on_log("fatal", str(e))
            self._log_view("error — press a key")
            self.ui.key()
            return 1

    def _run(self) -> int:
        st = self.inst.state
        from lib import disks as _d

        self._on_log("step", "checking environment and locating the live source")
        self._log_view("working…")
        self.inst.check_env()
        self.inst.list_disks()
        if not st.disks:
            self._on_log("fatal", "no whole disk >= 1 GiB found to install onto")
            self._log_view("error — press a key")
            self.ui.key()
            return 1

        # --- disk selection ---
        while True:
            rows = [f"{d['name']:<10} {d['size_bytes'] >> 30:>4} GiB   "
                    f"{d.get('model') or 'unknown'}"
                    for d in st.disks]
            i = self._list_prompt(rows, "Select the disk to install onto")
            if i < 0:
                continue
            st.disk = st.disks[i]["name"]
            st.total_sectors = self.inst.disk_total_sectors(st.disk)
            if st.total_sectors < _d.min_disk_sectors(min_root_mib=2):
                self._on_log("warn", f"{st.disk} is too small "
                             f"({st.total_sectors} sectors); need "
                             f"~{_d.min_disk_sectors(min_root_mib=2)}")
                continue
            break

        self._on_log("info", f"target {st.disk} ({st.total_sectors} sectors)")
        lay = _d.build_layout(st.disk, st.total_sectors)
        esp, root = _d.partition_device_names(st.disk, lay.esp_no, lay.root_no)
        plan = ("\n".join([
            f"  {esp}  : 1 MiB .. 513 MiB   FAT32   ESP (Gozjaro boot)",
            f"  {root} : 513 MiB .. end     ext4    root",
            "Then: copy system  →  build initramfs (target's own cpio)"
            "  →  install GRUB" + (" + NVRAM entry (UEFI)" if st.is_uefi
                                   else "  (BIOS MBR)"),
        ]))
        if not self._confirm("ALL DATA ON " + st.disk + " WILL BE DESTROYED.\n"
                             + plan, default_yes=False):
            self._on_log("warn", "partitioning cancelled — no changes made")
            self._log_view("cancelled")
            self.ui.key()
            return 1

        self._on_log("step", "partitioning + formatting " + st.disk + " (sgdisk)")
        self._log_view("working…")
        self.inst.run_partitioning()
        self._log_view("partitioned — press a key")
        self.ui.key()

        host = self._text_input("hostname", "gozjaro") or "gozjaro"
        tz = self._text_input("timezone (zoneinfo path, e.g. Asia/Shanghai)", "UTC") or "UTC"
        reboot = self._confirm("Reboot into the installed system now?",
                               default_yes=True)
        self.inst.run_post_partition(host, tz, reboot)
        self._log_view("installation complete — press a key")
        self.ui.key()
        return 0


# ---------------------------------------------------------------------------
def main() -> int:
    return curses.wrapper(_curses_main)


def _curses_main(scr) -> int:
    return TUI(scr).run()


if __name__ == "__main__":
    sys.exit(main())
