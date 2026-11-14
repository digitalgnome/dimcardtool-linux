# DIMcard Tool for Linux

[![license](https://img.shields.io/badge/license-PolyForm%20Noncommercial-orange)](LICENSE.md) [![Python 3](https://img.shields.io/badge/python-3-3776AB?logo=python&logoColor=white)](https://www.python.org/) [![Linux](https://img.shields.io/badge/platform-Linux-FCC624?logo=linux&logoColor=white)](#install) [![CH341A](https://img.shields.io/badge/hardware-CH341A-555)](#install) [![last commit](https://img.shields.io/github/last-commit/digitalgnome/dimcardtool-linux)](https://github.com/digitalgnome/dimcardtool-linux/commits/main)

A native Linux version of mrblinky's Windows
[DIMcard Tool v1.02](http://mrblinky.net/digimon/vb/dimcardtool/dimcardtool.html),
using the same hardware: a CH341A programmer wired to a micro-SD breakout. See the
original guide for the wiring.

It needs only Python 3, libusb-1.0 and PySide6 (for the GUI). All three come with
Bazzite, so there's nothing to install.

## Install

```
./install.sh
```

The installer copies the tool to `~/.local/bin/dimcardtool`, adds an app-menu entry and
installs a udev rule (asks for sudo) so the programmer works without root.
Replug the programmer afterwards.

## Use

Open **DIMcard Tool** from the app menu, or use the command line:

```
dimcardtool check                 # programmer, chip ID, bracelet link
dimcardtool unlock                # remove the Vital Bracelet link
dimcardtool backup [file.bin]     # default name: dimcard-backup-YYYYMMDDHHMMSS.bin
dimcardtool restore file.bin      # must be exactly 4 MiB; erases + programs + verifies
```

Always run `check` first. Back up every card before unlocking or restoring it, and
keep the backups somewhere safe (a .zip works).

## What each operation does on the chip

These were taken from the original tool's code, so they match its behavior:

| Operation | SPI actions |
|---|---|
| Check | `9F` JEDEC ID (capacity byte 0x16–0x18 is valid; `C2 20 16` = standard card), status regs `05/35/15`, reads 8 bytes at `0x10000` |
| Bracelet link | 8 bytes at `0x10000`; all `FF` = unlinked; the ID shown is each byte inverted, in reverse order |
| Unlock | if SR1 has BP bits (`0x3C`): WRSR `00`; sector erase `20` at `0x10000`; WRSR `3C`; verify the 8 bytes are `FF` |
| Backup | read `0x000000`–`0x3FFFFF` |
| Restore | clear protection, chip erase `C7`, page program `02` (skipping all-`FF` pages), WRSR `3C`, verify by reading back |

## Speed

By default the tool sends one 32-byte USB packet per round trip, which is the safest
setting. If that's slow on your machine, try `DIMCARD_BATCH=4 dimcardtool backup` and
compare the result against a normal backup.

## Tests

`python3 test_dimcardtool.py` runs every operation against a simulated MX25L3233F
behind a simulated CH341A. No hardware is needed.

## Credits

This project wouldn't exist without **Mr. Blinky**, who created the original
[DIMcard Tool](http://mrblinky.net/digimon/vb/dimcardtool/), worked out how to unlock
and customize DIMcards with a CH341A, and designed the reader hardware and wiring this
port uses. If this tool helps you, consider supporting him on
[Ko-fi](https://ko-fi.com/mrblinky/).

His tool's programmer code builds on [AsProgrammer](https://github.com/nofeletru/UsbAsp-flash/)
by nofeletru. The CH341A USB protocol used here follows [flashrom](https://flashrom.org/)'s
`ch341a_spi` driver.

This is an independent Linux reimplementation, with no code copied from the original tool.
Mr. Blinky does not maintain it, so please report problems with this port here, not to him.

## License

[PolyForm Noncommercial 1.0.0](LICENSE.md): free to use, modify and share for any
noncommercial purpose. This follows the original tool's "free for personal use, not for sale
or commercial use" terms.

Required Notice: Copyright (c) 2026 DigitalGnome (https://github.com/digitalgnome)

Provided as is, without warranty. Back up your cards before writing to them.
