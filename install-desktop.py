#!/usr/bin/env python3
"""Install a launcher for this checkout; no root or autostart changes."""
from pathlib import Path
import os

root = Path(__file__).resolve().parent
applications = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "applications"
applications.mkdir(parents=True, exist_ok=True)
# Desktop Exec quoting needs four backslashes in the file per literal slash.
executable = str(root / "run.sh").replace("\\", "\\\\\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
target = applications / "vsd-deck.desktop"
target.write_text(f'''[Desktop Entry]
Type=Application
Name=VSD Deck
Comment=N4 Pro controls, profiles, and live system tiles
Exec="{executable}"
Icon=input-gaming
Terminal=false
Categories=Utility;
StartupNotify=true
''')
print(f"Installed {target}")
