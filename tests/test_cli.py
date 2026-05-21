import subprocess
import sys

from ha_faker import __version__


def test_cli_version():
    cmd = [sys.executable, "-m", "ha_faker", "--version"]
    assert subprocess.check_output(cmd).decode().strip() == __version__
