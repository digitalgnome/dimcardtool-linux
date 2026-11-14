#!/usr/bin/env python3
"""DIMcard tool for Linux.

A native re-implementation of mrblinky's Windows "DIMcard Tool v1.02"
(http://mrblinky.net/digimon/vb/dimcardtool/) for a CH341A programmer wired
to a micro-SD breakout. Same four operations, same on-card behaviour:

  check    read JEDEC ID + status registers + Vital Bracelet link
  unlock   erase the 4 KiB sector at 0x10000 that holds the bracelet link
  backup   read the whole 4 MiB chip to a file
  restore  erase the chip and program a 4 MiB image, then verify

Credit: original tool, reverse-engineering and hardware design by Mr. Blinky.
License: PolyForm Noncommercial 1.0.0 (see LICENSE.md).

Run with no arguments for the GUI (PySide6), or with a subcommand for CLI.
Talks to the CH341A through libusb-1.0 via ctypes; no extra packages needed.
"""

import argparse
import ctypes
import ctypes.util
import datetime
import glob
import os
import sys
import time

CHIP_SIZE = 0x400000          # 4 MiB, the size of every DIMcard image
LINK_ADDR = 0x10000           # Vital Bracelet link: 8 bytes, sector 0x10000-0x10FFF
LINK_LEN = 8
PAGE = 256
READ_CHUNK = 4096
STANDARD_ID = (0xC2, 0x20, 0x16)   # Macronix MX25L3233F, 32 Mbit
PROTECT_BITS = 0x3C                # BP0-BP3 in status register 1


class DimError(Exception):
    pass


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------
# CH341A USB transport (protocol as used by flashrom's ch341a_spi driver)
# --------------------------------------------------------------------------

VID, PID = 0x1A86, 0x5512
EP_OUT, EP_IN = 0x02, 0x82
PACKET = 32
CMD_SPI_STREAM = 0xA8
CMD_I2C_STREAM = 0xAA
CMD_UIO_STREAM = 0xAB
I2C_STM_SET, I2C_STM_END = 0x60, 0x00
UIO_STM_OUT, UIO_STM_DIR, UIO_STM_END = 0x80, 0x40, 0x20
TIMEOUT_MS = 2000
# SPI packets sent per USB round trip. 1 is the safest; raise to speed up.
BATCH = max(1, int(os.environ.get("DIMCARD_BATCH", "1")))

_REV = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))   # CH341 shifts LSB first


def _load_libusb():
    for name in ("libusb-1.0.so.0", ctypes.util.find_library("usb-1.0")):
        if not name:
            continue
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError:
            continue
    else:
        raise DimError("libusb-1.0 not found on this system.")
    vp = ctypes.c_void_p
    lib.libusb_init.argtypes = [ctypes.POINTER(vp)]
    lib.libusb_exit.argtypes = [vp]
    lib.libusb_open_device_with_vid_pid.argtypes = [vp, ctypes.c_uint16, ctypes.c_uint16]
    lib.libusb_open_device_with_vid_pid.restype = vp
    lib.libusb_close.argtypes = [vp]
    lib.libusb_kernel_driver_active.argtypes = [vp, ctypes.c_int]
    lib.libusb_detach_kernel_driver.argtypes = [vp, ctypes.c_int]
    lib.libusb_claim_interface.argtypes = [vp, ctypes.c_int]
    lib.libusb_release_interface.argtypes = [vp, ctypes.c_int]
    lib.libusb_bulk_transfer.argtypes = [vp, ctypes.c_ubyte, ctypes.POINTER(ctypes.c_ubyte),
                                         ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_uint]
    lib.libusb_error_name.argtypes = [ctypes.c_int]
    lib.libusb_error_name.restype = ctypes.c_char_p
    return lib


def _ch341_present():
    for d in glob.glob("/sys/bus/usb/devices/*"):
        try:
            with open(f"{d}/idVendor") as v, open(f"{d}/idProduct") as p:
                if int(v.read(), 16) == VID and int(p.read(), 16) == PID:
                    return True
        except OSError:
            pass
    return False


class CH341A:
    def __init__(self):
        self.lib = _load_libusb()
        self.ctx = ctypes.c_void_p()
        if self.lib.libusb_init(ctypes.byref(self.ctx)) != 0:
            raise DimError("libusb_init failed.")
        self.h = self.lib.libusb_open_device_with_vid_pid(self.ctx, VID, PID)
        if not self.h:
            self.lib.libusb_exit(self.ctx)
            if _ch341_present():
                raise DimError("CH341A found but permission denied.\n"
                               "Install the udev rule (see README) and replug the programmer.")
            raise DimError("CH341A programmer not found! Check USB connection.")
        if self.lib.libusb_kernel_driver_active(self.h, 0) == 1:
            self.lib.libusb_detach_kernel_driver(self.h, 0)
        r = self.lib.libusb_claim_interface(self.h, 0)
        if r != 0:
            self.close()
            raise DimError(f"Cannot claim CH341A interface ({self._err(r)}). "
                           "Is another program using it?")
        self._write(bytes([CMD_I2C_STREAM, I2C_STM_SET | 1, I2C_STM_END]))   # stream speed, as flashrom
        self._write(bytes([CMD_UIO_STREAM, UIO_STM_OUT | 0x37, UIO_STM_DIR | 0x3F, UIO_STM_END]))

    def _err(self, r):
        return self.lib.libusb_error_name(r).decode()

    def _write(self, data):
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        n = ctypes.c_int()
        r = self.lib.libusb_bulk_transfer(self.h, EP_OUT, buf, len(data), ctypes.byref(n), TIMEOUT_MS)
        if r != 0 or n.value != len(data):
            raise DimError(f"USB write failed ({self._err(r)}).")

    def _read(self, length):
        buf = (ctypes.c_ubyte * PACKET)()
        n = ctypes.c_int()
        r = self.lib.libusb_bulk_transfer(self.h, EP_IN, buf, PACKET, ctypes.byref(n), TIMEOUT_MS)
        if r != 0 or n.value != length:
            raise DimError(f"USB read failed ({self._err(r)}, got {n.value}/{length}).")
        return bytes(buf[:length])

    def _cs(self, active):
        self._write(bytes([CMD_UIO_STREAM, UIO_STM_OUT | (0x36 if active else 0x37), UIO_STM_END]))

    def xfer(self, out, readlen=0):
        """One SPI transaction under a single CS: send `out`, then clock in `readlen` bytes."""
        stream = bytes(out) + b"\xff" * readlen
        got = bytearray()
        self._cs(True)
        try:
            pos = 0
            while pos < len(stream):
                pkts = []
                for _ in range(BATCH):
                    if pos >= len(stream):
                        break
                    chunk = stream[pos:pos + PACKET - 1]
                    pos += len(chunk)
                    pkts.append(chunk)
                self._write(b"".join(bytes([CMD_SPI_STREAM]) + c.translate(_REV) for c in pkts))
                for c in pkts:
                    got += self._read(len(c)).translate(_REV)
        finally:
            self._cs(False)
        return bytes(got[len(out):])

    def close(self):
        if self.h:
            self.lib.libusb_release_interface(self.h, 0)
            self.lib.libusb_close(self.h)
            self.h = None
            self.lib.libusb_exit(self.ctx)


# --------------------------------------------------------------------------
# DIMcard operations (SPI NOR flash commands)
# --------------------------------------------------------------------------

class DimCard:
    def __init__(self, spi, log=print, progress=None, cancel=None):
        self.spi = spi
        self.log = log
        self.progress = progress or (lambda done, total: None)
        self.cancel = cancel or (lambda: False)

    # -- primitives --
    def jedec_id(self):
        return self.spi.xfer([0x9F], 3)

    def read_sr(self, cmd=0x05):
        return self.spi.xfer([cmd], 1)[0]

    def wren(self):
        self.spi.xfer([0x06])

    def write_sr(self, value):
        self.wren()
        self.spi.xfer([0x01, value])
        self.wait_ready()

    def wait_ready(self, timeout=120.0):
        end = time.monotonic() + timeout
        while self.read_sr() & 0x01:
            if time.monotonic() > end:
                raise DimError("Timed out waiting for the chip.")
            time.sleep(0.002)

    def read(self, addr, length):
        return self.spi.xfer([0x03, (addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF], length)

    def sector_erase(self, addr):
        self.wren()
        self.spi.xfer([0x20, (addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF])
        self.wait_ready()

    def chip_erase(self):
        self.wren()
        self.spi.xfer([0xC7])
        self.wait_ready(timeout=300.0)

    def page_program(self, addr, data):
        self.wren()
        self.spi.xfer(bytes([0x02, (addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF]) + data)
        self.wait_ready()

    def unprotect(self):
        if self.read_sr() & PROTECT_BITS:
            self.log("Removing chip write protect ...")
            self.write_sr(0x00)
            if self.read_sr() & PROTECT_BITS:
                return False
        return True

    def protect(self):
        self.log("Re-enabling chip write protect ...")
        self.write_sr(PROTECT_BITS)

    def _check_cancel(self):
        if self.cancel():
            raise Cancelled()

    # -- high level --
    def validate(self):
        jid = self.jedec_id()
        if not 0x16 <= jid[2] <= 0x18:
            raise DimError("Invalid or no DIMcard found!\nCheck the card is inserted and the wiring.")
        return jid

    def link_id(self):
        raw = self.read(LINK_ADDR, LINK_LEN)
        if all(b == 0xFF for b in raw):
            return None
        return "".join(f"{0xFF - b:02X}" for b in reversed(raw))

    def check(self):
        jid = self.jedec_id()
        s1, s2, s3 = self.read_sr(0x05), self.read_sr(0x35), self.read_sr(0x15)
        self.log(f"DIMcard chip ID: {jid.hex(' ').upper()} status: {s3:02X} {s2:02X} {s1:02X}")
        self.validate()
        if tuple(jid) == STANDARD_ID:
            self.log("Standard DIMcard found.")
        else:
            self.log("Non standard DIMcard found.")
        lid = self.link_id()
        if lid:
            self.log(f"Linked to a Vital Bracelet with ID: {lid}")
        else:
            self.log("Not linked to any Vital Bracelet.")
        return lid

    def unlock(self):
        self.validate()
        self.log("Checking Vital Bracelet link...")
        if self.link_id() is None:
            self.log("There is no Vital Bracelet link to remove.")
            return True
        if not self.unprotect():
            raise DimError("Unable to remove chip write protect.\nVital Bracelet link still active.")
        self.log("Removing Vital Bracelet link ...")
        self.sector_erase(LINK_ADDR)
        self.protect()
        if self.link_id() is None:
            self.log("Vital Bracelet link successfully removed.")
            return True
        raise DimError("Remove unsuccessful. Vital Bracelet link still active.")

    def backup(self, path):
        self.validate()
        t0 = time.monotonic()
        self.log(f"Backup started at {datetime.datetime.now():%H:%M:%S}")
        data = bytearray()
        for addr in range(0, CHIP_SIZE, READ_CHUNK):
            self._check_cancel()
            data += self.read(addr, READ_CHUNK)
            self.progress(addr + READ_CHUNK, CHIP_SIZE)
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        self.log(f"Backup saved to {path}")
        self.log(f"Backup completed in {time.monotonic() - t0:.0f} s")

    def restore(self, path):
        with open(path, "rb") as f:
            image = f.read()
        if len(image) != CHIP_SIZE:
            raise DimError(f"Invalid file ({len(image)} bytes, expected {CHIP_SIZE}). Restore aborted.")
        self.validate()
        t0 = time.monotonic()
        self.log(f"Restoring from file {path}")
        if not self.unprotect():
            raise DimError("Unable to remove chip write protect.\nRestore failed.")
        self.log("Erasing chip ...")
        self.chip_erase()
        self.log("Programming chip ...")
        try:
            for addr in range(0, CHIP_SIZE, PAGE):
                self._check_cancel()
                page = image[addr:addr + PAGE]
                if page != b"\xff" * PAGE:
                    self.page_program(addr, page)
                if addr % READ_CHUNK == 0:
                    self.progress(addr + PAGE, CHIP_SIZE * 2)
        finally:
            self.protect()
        self.log("Verifying ...")
        for addr in range(0, CHIP_SIZE, READ_CHUNK):
            self._check_cancel()
            if self.read(addr, READ_CHUNK) != image[addr:addr + READ_CHUNK]:
                raise DimError(f"Verify failed at 0x{addr:06X}. Restore failed!")
            self.progress(CHIP_SIZE + addr + READ_CHUNK, CHIP_SIZE * 2)
        self.log(f"Restore completed in {time.monotonic() - t0:.0f} s")


def default_backup_name():
    return f"dimcard-backup-{datetime.datetime.now():%Y%m%d%H%M%S}.bin"


def run_op(op, arg=None, log=print, progress=None, cancel=None):
    spi = CH341A()
    try:
        log("CH341A programmer found.")
        card = DimCard(spi, log, progress, cancel)
        if op == "check":
            card.check()
        elif op == "unlock":
            card.unlock()
        elif op == "backup":
            card.backup(arg)
        elif op == "restore":
            card.restore(arg)
    finally:
        spi.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cli(argv):
    p = argparse.ArgumentParser(prog="dimcardtool", description="DIMcard tool for CH341A on Linux")
    sub = p.add_subparsers(dest="op", required=True)
    sub.add_parser("check", help="check programmer, card and bracelet link")
    sub.add_parser("unlock", help="remove the Vital Bracelet link")
    b = sub.add_parser("backup", help="save the card to a 4 MiB file")
    b.add_argument("file", nargs="?", default=None)
    r = sub.add_parser("restore", help="write a 4 MiB file to the card")
    r.add_argument("file")
    r.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    a = p.parse_args(argv)

    arg = getattr(a, "file", None)
    if a.op == "backup" and not arg:
        arg = default_backup_name()
    if a.op == "restore" and not a.yes:
        if input(f"Erase the DIMcard and write {arg}? [y/N] ").strip().lower() != "y":
            return 1

    last = [-1]

    def progress(done, total):
        pct = done * 100 // total
        if pct != last[0]:
            last[0] = pct
            print(f"\r{pct:3d}%", end="", file=sys.stderr, flush=True)
            if pct == 100:
                print(file=sys.stderr)

    try:
        run_op(a.op, arg, progress=progress)
    except Cancelled:
        print("\nCanceled.")
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except DimError as e:
        print(f"\nError: {e}", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

def gui():
    from PySide6.QtCore import QObject, QThread, Signal
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QMessageBox,
                                   QProgressBar, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

    class Worker(QObject):
        logged = Signal(str)
        progressed = Signal(int)
        finished = Signal()

        def __init__(self, op, arg):
            super().__init__()
            self.op, self.arg, self.stop = op, arg, False

        def run(self):
            try:
                run_op(self.op, self.arg, log=self.logged.emit,
                       progress=lambda d, t: self.progressed.emit(d * 1000 // t),
                       cancel=lambda: self.stop)
            except Cancelled:
                self.logged.emit(f"{self.op.capitalize()} canceled.")
            except DimError as e:
                self.logged.emit(str(e))
            except Exception as e:   # surface anything unexpected instead of dying silently
                self.logged.emit(f"Unexpected error: {e!r}")
            self.finished.emit()

    class Window(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("DIMcard Tool (Linux)")
            self.resize(560, 420)
            lay = QVBoxLayout(self)
            row = QHBoxLayout()
            self.buttons = []
            for label, fn in (("Check DIMcard", self.do_check), ("Unlock DIMcard", self.do_unlock),
                              ("Backup DIMcard", self.do_backup), ("Restore DIMcard", self.do_restore)):
                btn = QPushButton(label)
                btn.clicked.connect(fn)
                row.addWidget(btn)
                self.buttons.append(btn)
            lay.addLayout(row)
            self.log = QPlainTextEdit(readOnly=True)
            self.log.setFont(QFont("monospace"))
            lay.addWidget(self.log)
            bottom = QHBoxLayout()
            self.bar = QProgressBar(maximum=1000, textVisible=False)
            bottom.addWidget(self.bar)
            self.cancel_btn = QPushButton("Cancel", enabled=False)
            self.cancel_btn.clicked.connect(self.cancel)
            bottom.addWidget(self.cancel_btn)
            lay.addLayout(bottom)
            self.log.appendPlainText("Always use Check DIMcard first to test the programmer and card.")
            self.thread = self.worker = None

        def start(self, op, arg=None):
            self.log.appendPlainText("")
            self.bar.setValue(0)
            for b in self.buttons:
                b.setEnabled(False)
            self.cancel_btn.setEnabled(op in ("backup", "restore"))
            self.thread = QThread()
            self.worker = Worker(op, arg)
            self.worker.moveToThread(self.thread)
            self.thread.started.connect(self.worker.run)
            self.worker.logged.connect(self.log.appendPlainText)
            self.worker.progressed.connect(self.bar.setValue)
            self.worker.finished.connect(self.done)
            self.thread.start()

        def done(self):
            self.thread.quit()
            self.thread.wait()
            for b in self.buttons:
                b.setEnabled(True)
            self.cancel_btn.setEnabled(False)

        def cancel(self):
            if self.worker:
                self.worker.stop = True

        def do_check(self):
            self.start("check")

        def do_unlock(self):
            self.start("unlock")

        def do_backup(self):
            path, _ = QFileDialog.getSaveFileName(self, "Save backup",
                                                  os.path.expanduser(f"~/{default_backup_name()}"),
                                                  "DIMcard image (*.bin);;All files (*)")
            if path:
                self.start("backup", path)

        def do_restore(self):
            path, _ = QFileDialog.getOpenFileName(self, "Restore image", os.path.expanduser("~"),
                                                  "DIMcard image (*.bin);;All files (*)")
            if not path:
                return
            if QMessageBox.question(self, "Restore DIMcard",
                                    f"Erase the DIMcard and write\n{path}?\n\nThis takes several minutes."
                                    ) == QMessageBox.Yes:
                self.start("restore", path)

        def closeEvent(self, ev):
            if self.thread and self.thread.isRunning():
                ev.ignore()
                return
            ev.accept()

    app = QApplication(sys.argv)
    w = Window()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]) if len(sys.argv) > 1 else gui())
