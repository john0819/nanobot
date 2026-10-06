"""Image-owned entrypoint checks admitted source hash before fixed pytest invocation."""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

root = Path("/suite/fixed_fixture")
source_hash = hashlib.sha256(root.joinpath("gateway.py").read_bytes() + b"\0"
                            + root.joinpath("test_gateway.py").read_bytes()).hexdigest()
if source_hash != os.environ.get("TESTPILOT_SUITE_HASH"):
    raise SystemExit("Runner source hash does not match admitted target")
result = subprocess.run([
    sys.executable, "-I", "-m", "pytest", "-q", "-p", "no:cacheprovider",
    "--rootdir", "/suite", "--confcutdir", "/suite", "--junitxml", "/artifacts/junit.xml",
    "/suite/fixed_fixture/test_gateway.py",
])
raise SystemExit(result.returncode)
