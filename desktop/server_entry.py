"""Entry point PyInstaller freezes into the `restdesk-server` binary bundled with the desktop app."""
import sys

from restdesk.server import main

if __name__ == "__main__":
    sys.exit(main())
