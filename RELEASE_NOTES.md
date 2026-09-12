# VSD Deck v0.1.0 — First public preview

A native Linux controller for the **VSD N4 Pro**, tested on Arch-based
Omarchy / Hyprland with USB variant `5548:1023`.

## Included

- Visual editor, custom button images, saved profiles, and brightness controls.
- Ten display keys, four touch zones, four rotary knobs, and swipe bindings.
- Program launching, websites, keyboard shortcuts, media, volume, and mic mute.
- CPU, memory, disk, and optional weather tiles.
- Editable DaVinci Resolve shortcut profile.
- USB reconnect, permissions diagnostics, and a simulator.

## Install

Download the attached source archive, extract it, and follow the README's Arch
installation steps. Run `./setup.sh`, install the included USB access rule,
reconnect the deck, and launch `./run.sh` as your regular user.

The archive contains source and documentation. Setup downloads the pinned
manufacturer SDK and Python dependencies; the Windows MSI is not required.

## Known limits

- Early preview: one hardware variant and Hyprland have had hands-on testing.
- Resolve support is keyboard shortcuts, not the vendor's complete plugin.
- Weather needs your coordinates; keyboard/media actions need the documented
  desktop tools. The app must remain running to handle the deck.

68 automated tests pass. Actual button labels and live system tiles were
confirmed on the connected N4 Pro after applying the USB access rule.
