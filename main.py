"""iFlow Core entry point.

    python main.py                     desktop window (flet build / flet pack use this module too)
    python main.py --selftest [--json] deployment self-test, then exit (works inside packaged binaries)
"""

import sys

if __name__ == "__main__":
    import os

    from core.config import promote_legacy_env

    promote_legacy_env(os.environ)          # pre-rebrand RUBYSOHO_* settings keep working
    if "--selftest" in sys.argv:
        from core.selftest import main as selftest

        sys.exit(selftest(sys.argv[1:]))
    from ui.app import run

    run()
