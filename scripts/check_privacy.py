"""Reject real phone-number examples in tracked text files."""
from pathlib import Path
import re
import subprocess
import sys

allowed = {"000000001", "000000002"}
pattern = re.compile(r"(?<![A-Za-z0-9])(?:00420|420)?([0-9]{9})(?![A-Za-z0-9])")
failures = []
for name in subprocess.check_output(["git", "ls-files"], text=True).splitlines():
    path = Path(name)
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeError, OSError):
        continue
    if any(match.group(1) not in allowed for match in pattern.finditer(content)):
        failures.append(name)
if failures:
    print("Non-fictional phone-number examples found in: " + ", ".join(failures))
    sys.exit(1)
print("Privacy scan passed")
