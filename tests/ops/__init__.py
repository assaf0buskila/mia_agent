"""Tests for the VM and ops tooling under ops/. Nothing here touches Google, docker or a VM."""

import sys
from pathlib import Path

# The backup scripts are installed flat on the VM and import each other by module name.
_BACKUP_DIR = Path(__file__).resolve().parents[2] / "ops" / "gcp" / "backup"
if str(_BACKUP_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKUP_DIR))
