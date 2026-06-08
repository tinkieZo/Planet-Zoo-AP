"""PyInstaller entry point for the Planet Zoo Archipelago hooking client.

A thin launcher so PyInstaller has a top-level script to freeze; all logic lives in
``pz_ap_client.client``. The vendored Archipelago tree is shipped as on-disk data next to the
frozen package (see pz-ap-client.spec), and client.py adds it to sys.path at import time -
``Path(__file__).parent.parent / "vendor" / "Archipelago"`` resolves to the bundle dir when frozen.
"""
import logging
import sys

from pz_ap_client.client import main

if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    main(sys.argv[1:])
