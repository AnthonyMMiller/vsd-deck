# Contributing

Bug reports and fixes are welcome. This first release targets the VSD N4 Pro on
Arch Linux, with hands-on testing on Omarchy / Hyprland.

For a bug report, include:

- Linux distribution, desktop/compositor, and whether you use Wayland or X11.
- Device model and USB vendor/product IDs (omit the serial number).
- What you did, what happened, and what you expected.
- Relevant output from `./run.sh --diagnostics` and the app's activity log.

Review logs before sharing them: custom command paths or URLs may be personal.
Use the GitHub issue tracker for the repository, not the manufacturer's tracker
unless the problem has been reproduced with their SDK alone.

## Development

Run `./setup.sh` to create a virtual environment and download the tested SDK.
Then run:

```bash
./run.sh --simulate
.venv/bin/python -m unittest discover -s tests -v
```

The simulator never accesses USB or executes configured desktop commands.
Tests use mocked hardware and providers; no deck, API key, or weather location
is required. Keep USB work on the device worker thread and Qt updates on the
GUI thread. Hardware claims need real-device verification: a successful return
from the SDK's queued-write API alone does not prove delivery.

Do not commit the Windows installer, virtual environment, downloaded SDK,
personal profile files, or credentials. Preserve third-party license notices.
