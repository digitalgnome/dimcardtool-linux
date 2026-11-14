#!/usr/bin/env python3
"""Tests against a simulated MX25L3233F behind a simulated CH341A (no hardware needed)."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dimcardtool as dt  # noqa: E402


class FakeFlash:
    """SPI NOR flash: 4 MiB, BP bits protect writes, WEL latch, page wrap."""

    def __init__(self, data=None, sr=0x3C, jid=dt.STANDARD_ID):
        self.mem = bytearray(data or b"\xff" * dt.CHIP_SIZE)
        self.sr, self.jid, self.wel = sr, bytes(jid), False

    def _protected(self):
        return bool(self.sr & dt.PROTECT_BITS)

    def miso(self, mosi, start):
        """MISO bytes for positions [start, len(mosi)) of the current CS-low period (no side effects)."""
        n = len(mosi)
        out = bytearray(b"\xff" * n)
        cmd = mosi[0]
        if cmd == 0x9F:
            for i in range(max(1, start), n):
                out[i] = self.jid[(i - 1) % 3]
        elif cmd in (0x05, 0x35, 0x15):
            val = (self.sr | (2 if self.wel else 0)) if cmd == 0x05 else 0
            for i in range(max(1, start), n):
                out[i] = val
        elif cmd == 0x03 and n > 4:
            a = (mosi[1] << 16) | (mosi[2] << 8) | mosi[3]
            lo = max(4, start)
            out[lo:n] = self.mem[a + lo - 4:a + n - 4]
        return bytes(out[start:])

    def commit(self, mosi):
        """Apply write/erase side effects when CS goes high."""
        cmd = mosi[0]
        addr = lambda: (mosi[1] << 16) | (mosi[2] << 8) | mosi[3]
        if cmd == 0x06:
            self.wel = True
            return
        if not self.wel:
            return
        if cmd == 0x01:
            self.sr = mosi[1] & 0xFC
        elif cmd == 0x20 and not self._protected():
            a = addr() & ~0xFFF
            self.mem[a:a + 0x1000] = b"\xff" * 0x1000
        elif cmd in (0xC7, 0x60) and not self._protected():
            self.mem[:] = b"\xff" * dt.CHIP_SIZE
        elif cmd == 0x02 and not self._protected():
            a = addr()
            base = a & ~0xFF
            for i, b in enumerate(mosi[4:]):
                self.mem[base + ((a + i) & 0xFF)] &= b   # NOR: can only clear bits
        else:
            return
        self.wel = False


class FakeCH341(dt.CH341A):
    """Real CH341A.xfer code path; USB endpoints replaced by the flash model."""

    def __init__(self, flash):
        self.flash = flash
        self.cs = False
        self.mosi = bytearray()
        self.pending = []

    def _write(self, data):
        if data[0] == dt.CMD_UIO_STREAM:
            active = data[1] == (dt.UIO_STM_OUT | 0x36)
            if self.cs and not active:
                self.flash.commit(bytes(self.mosi))   # side effects on CS rising edge
            if active and not self.cs:
                self.mosi = bytearray()
            self.cs = active
            return
        assert self.cs, "SPI data sent with CS high"
        assert len(data) <= dt.PACKET * dt.BATCH
        i = 0
        while i < len(data):
            assert data[i] == dt.CMD_SPI_STREAM
            end = min(len(data), i + dt.PACKET)   # 1 cmd byte + up to 31 payload bytes
            start = len(self.mosi)
            self.mosi += data[i + 1:end].translate(dt._REV)
            self.pending.append(self.flash.miso(self.mosi, start).translate(dt._REV))
            i = end

    def _read(self, length):
        r = self.pending.pop(0)
        assert len(r) == length
        return r

    def close(self):
        pass


def card(flash, logs=None):
    return dt.DimCard(FakeCH341(flash), log=(logs.append if logs is not None else lambda m: None))


def linked_image():
    img = bytearray(os.urandom(dt.CHIP_SIZE))
    img[dt.LINK_ADDR:dt.LINK_ADDR + 8] = bytes(0xFF - b for b in [1, 2, 3, 4, 5, 6, 7, 8])
    return img


class Tests(unittest.TestCase):
    def test_check_standard_linked(self):
        logs = []
        lid = card(FakeFlash(linked_image()), logs).check()
        self.assertEqual(lid, "0807060504030201")
        self.assertIn("Standard DIMcard found.", logs)

    def test_check_invalid(self):
        with self.assertRaises(dt.DimError):
            card(FakeFlash(jid=(0xFF, 0xFF, 0xFF))).check()

    def test_unlock_only_touches_link_sector(self):
        img = linked_image()
        f = FakeFlash(img)
        self.assertTrue(card(f).unlock())
        expect = bytearray(img)
        expect[0x10000:0x11000] = b"\xff" * 0x1000
        self.assertEqual(f.mem, expect)
        self.assertEqual(f.sr, 0x3C)           # protection re-enabled

    def test_unlock_not_linked(self):
        logs = []
        f = FakeFlash()
        card(f, logs).unlock()
        self.assertIn("There is no Vital Bracelet link to remove.", logs)

    def test_backup_restore_roundtrip(self):
        src = FakeFlash(linked_image())
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "b.bin")
            card(src).backup(p)
            self.assertEqual(open(p, "rb").read(), bytes(src.mem))
            dst = FakeFlash(os.urandom(dt.CHIP_SIZE))
            card(dst).restore(p)
            self.assertEqual(dst.mem, src.mem)
            self.assertEqual(dst.sr, 0x3C)

    def test_restore_rejects_wrong_size(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.bin")
            open(p, "wb").write(b"\0" * 100)
            f = FakeFlash(linked_image())
            before = bytes(f.mem)
            with self.assertRaises(dt.DimError):
                card(f).restore(p)
            self.assertEqual(bytes(f.mem), before)


if __name__ == "__main__":
    for batch in (1, 4):
        dt.BATCH = batch
        print(f"--- BATCH={batch}")
        r = unittest.main(exit=False, argv=[sys.argv[0]]).result
        if not r.wasSuccessful():
            sys.exit(1)
