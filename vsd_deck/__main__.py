import argparse
import json
import logging
import os
import sys

from .config import config_path


def main():
    parser = argparse.ArgumentParser(description="Native VSD N4 Pro controller for Linux")
    parser.add_argument("--simulate", action="store_true", help="No USB access; log actions instead of executing")
    parser.add_argument("--config", type=str, default=str(config_path()), help="Profile JSON path")
    parser.add_argument("--diagnostics", action="store_true", help="Print desktop tool availability and exit")
    parser.add_argument("--list-devices", action="store_true", help="Enumerate N4 Pro interfaces and exit")
    parser.add_argument("--screenshot", metavar="PNG", help="Save simulator screenshot and exit (offscreen compatible)")
    args = parser.parse_args()
    if args.list_devices:
        from .device import enumerate_devices
        try:
            print(json.dumps(enumerate_devices(), indent=2, default=str))
        except (OSError, RuntimeError) as exc:
            print(f"Device enumeration failed: {exc}", file=sys.stderr)
            return 1
        return
    if args.diagnostics:
        from .actions import ActionRunner
        from .device import enumerate_devices
        diagnostics = ActionRunner().diagnostics()
        try:
            diagnostics["usb_devices"] = [
                {"path": device["path"], "read_write_access": os.access(device["path"], os.R_OK | os.W_OK)}
                for device in enumerate_devices()
            ]
        except Exception as exc:
            diagnostics["usb_error"] = str(exc)
        print(json.dumps(diagnostics, indent=2))
        return
    from PySide6.QtCore import QTimer, QLockFile, QStandardPaths
    from PySide6.QtWidgets import QApplication, QMessageBox
    from .app import MainWindow
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    app = QApplication(sys.argv)
    app.setApplicationName("VSD Deck")
    app.setOrganizationName("VSD Deck")
    lock = None
    if not args.simulate and not args.screenshot:
        lock = QLockFile(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.RuntimeLocation) + "/vsd-deck.lock")
        if not lock.tryLock(0):
            QMessageBox.warning(None, "VSD Deck is already running", "Open the existing VSD Deck window from the system tray.")
            return 1
    try:
        window = MainWindow(args.config, simulate=args.simulate or bool(args.screenshot))
    except (OSError, ValueError) as exc:
        if args.screenshot:
            print(f"Could not load profiles: {exc}", file=sys.stderr)
        else:
            QMessageBox.critical(None, "Could not load profiles", str(exc))
        return 1
    window.show()
    if args.screenshot:
        def capture():
            saved = window.grab().save(args.screenshot)
            window.dirty = False
            window.close()
            if not saved:
                print(f"Could not save screenshot: {args.screenshot}", file=sys.stderr)
                app.exit(1)
        QTimer.singleShot(700, capture)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
