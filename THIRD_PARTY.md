# Third-party components

This project uses MiraboxSpace's StreamDock Device SDK, MIT licensed:
https://github.com/MiraboxSpace/StreamDock-Device-SDK

Tested/pinned revision: `a3c31aa4c330c30fb6d0815c00747ac52374f189`.
The unmodified checkout and its license notices live in `vendor/streamdock-sdk`.
The adapter uses its Python ctypes transport definitions and bundled Linux native
transport. N4 Pro packet/image mappings are checked against its C++ implementation.
The SDK checkout contains C++ wrappers and a prebuilt native transport library;
this project does not build that library from source. SDK binaries are downloaded
by `setup.sh` and are not included in VSD Deck's source release.
The upstream MIT notice is also retained in `docs/licenses/StreamDock-LICENSE`.

PySide6 / Qt are distributed under their respective LGPL/GPL/commercial terms.
Pillow uses the HPND license. Their notices are included with the installed packages.

Optional weather data: [Open-Meteo](https://open-meteo.com/),
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
The app displays attribution and fetches only after coordinates are configured.

VSD Deck is an independent project and is not affiliated with or endorsed by
VSDinside, MiraboxSpace, or Blackmagic Design. Product names identify compatible
hardware and applications.
