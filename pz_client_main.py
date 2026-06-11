"""PyInstaller entry point for the Planet Zoo Archipelago hooking client.

A thin launcher so PyInstaller has a top-level script to freeze; all logic lives in
``pz_ap_client.client``. The vendored Archipelago tree is shipped as on-disk data next to the
frozen package (see pz-ap-client.spec), and client.py adds it to sys.path at import time -
``Path(__file__).parent.parent / "vendor" / "Archipelago"`` resolves to the bundle dir when frozen.
"""
import logging
import sys

if __name__ == "__main__":
    # Frozen self-re-invocation: the ovl installer spawns THIS exe as the
    # cobra-tools inject child (see pz_ap_client/ovl.py). Handle the sentinel
    # before importing the client (the child needs neither AP nor pymem).
    if len(sys.argv) > 1 and sys.argv[1] == "--run-ovl-inject":
        from pz_ap_client.ovl import _inject_child_main
        sys.exit(_inject_child_main(sys.argv[2:]))

    from pz_ap_client.client import main

    logging.getLogger().setLevel(logging.INFO)
    main(sys.argv[1:])
