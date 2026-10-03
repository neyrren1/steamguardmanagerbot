"""Backward-compatible launcher for GuardBot."""

import asyncio
import sys

if "--generate-key" in sys.argv:
    from cryptography.fernet import Fernet

    print(Fernet.generate_key().decode())
else:
    from guardbot.compat import *
    from guardbot.__main__ import main

    if __name__ == "__main__":
        asyncio.run(main())
