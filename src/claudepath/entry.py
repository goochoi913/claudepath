"""
Console entry point.

`claudepath auto` runs before every `claude` launch, so it is dispatched here
before the full CLI (and its imports) is loaded.
"""

import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "auto":
        from claudepath.auto import main as auto_main

        sys.exit(auto_main(sys.argv[2:]))
    from claudepath.cli import main as cli_main

    cli_main()
