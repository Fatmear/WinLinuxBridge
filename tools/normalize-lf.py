"""Normalises line endings to LF for every file that must stay LF.

Windows checkouts and some editors silently write CRLF. For bash scripts that
breaks execution ("$'\\r': command not found"), and for the patch tooling it
breaks exact-string anchors. Run this before packaging.

    python tools/normalize-lf.py           # check and fix
    python tools/normalize-lf.py --check   # list only, exit 1
"""

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Everything that runs on Linux, plus the JSON-ish text files that are diffed.
TARGETS = [
    "linux/wl",
    "linux/wl_job.py",
    "linux/install-linux.sh",
    "tools/verify.sh",
    "tools/verify_jobs.sh",
    "tools/verify_cancel.sh",
]


def main(argv):
    check_only = "--check" in argv
    dirty = 0
    for rel in TARGETS:
        path = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.isfile(path):
            print("missing   : %s" % rel)
            continue
        with io.open(path, encoding="utf-8", newline="") as fh:
            raw = fh.read()
        fixed = raw.replace("\r\n", "\n").replace("\r", "\n")
        if fixed == raw:
            print("ok (LF)   : %s" % rel)
            continue
        dirty += 1
        if check_only:
            print("CRLF      : %s" % rel)
            continue
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(fixed)
        print("normalised: %s" % rel)
    if dirty and check_only:
        print("\n%d file(s) contain CR; run without --check to fix." % dirty)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
