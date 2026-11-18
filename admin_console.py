"""iFlow Core Admin Console: the operator app (users, licenses, Parser Studio, crash reports, health).

    iFlow Core Admin Console (app menu)      after installing iflowcore-admin_<version>_amd64.deb
    python admin_console.py                  from source
    python admin_console.py --selftest       check the build (temporary folder, nothing real touched)

Separate from the user app (main.py): it never opens an iFlow Core database. "This computer" runs the
admin service inside the console; the first launch offers to create the owner account.
"""

import sys
from pathlib import Path

if __name__ == "__main__":
    service_src = Path(__file__).resolve().parent / "services" / "ai"
    if service_src.is_dir():                     # from source; the packaged app bundles iflow_ai itself
        sys.path.insert(0, str(service_src))
    if "--selftest" in sys.argv:
        from ui.admin_console.selftest import main as selftest

        sys.exit(selftest(sys.argv[1:]))
    from ui.admin_console.app import run

    run()
