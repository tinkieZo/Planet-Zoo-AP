"""Build a release zip with SPEC-COMPLIANT (forward-slash) entry paths.

Used by build-exe.ps1's -Version step. Kept as a real repo file rather than an inline
`python -c` string because Windows PowerShell 5.1 mangles the double-quotes when passing
a multi-line script via -c (strips them -> the Python source arrives as a syntax error).

Why not Compress-Archive: PS 5.1's Compress-Archive writes BACKSLASH entry names (a .NET
Framework defect); Windows unpackers tolerate them but Linux/mac take them literally and
produce a flat pile of "pz-ap-client\\_internal\\..." files instead of folders. os.walk +
an explicit "/" join avoids that, and we emit an entry for empty dirs (e.g. custom_worlds)
that a files-only walk would otherwise drop.

Verifying the output is a trap: Python's zipfile NORMALIZES \\ to / on read, so namelist()
reports a broken zip as clean. Check the raw central-directory bytes instead (see
docs/PACKAGING.md §Release zip).

Usage: python tools/make_release_zip.py <src_dir> <out.zip>
"""
import os
import sys
import zipfile


def make_zip(src: str, out: str) -> int:
    base = os.path.dirname(os.path.abspath(src))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirs, files in os.walk(src):
            if not dirs and not files:
                # empty dir: explicit entry so unpackers still create it
                zf.writestr(os.path.relpath(dirpath, base).replace(os.sep, "/") + "/", "")
            for f in files:
                p = os.path.join(dirpath, f)
                zf.write(p, os.path.relpath(p, base).replace(os.sep, "/"))
        n = len(zf.namelist())
    print("wrote %s (%d entries)" % (out, n))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: make_release_zip.py <src_dir> <out.zip>")
    sys.exit(make_zip(sys.argv[1], sys.argv[2]))
