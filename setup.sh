#!/usr/bin/env bash
# NeuroDrive one-command setup (macOS, Linux).
#
#     ./setup.sh
#
# Creates a virtual environment, installs the dependencies, and runs the test
# suite so you know the checkout is healthy before touching hardware.
#
# Should-Have item from plan section 10.2.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv"
REQUIREMENTS="$ROOT/python_bridge/requirements.txt"

echo
echo "  NeuroDrive setup"
echo "  ----------------------------------------"

# --- Python -----------------------------------------------------------------

if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    echo "  Python was not found on PATH."
    echo "  Install Python 3.10 or newer and re-run."
    exit 1
fi

VERSION="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "  Python $VERSION found"

if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "  Python 3.10 or newer is required (found $VERSION)."
    exit 1
fi

# --- Virtual environment ----------------------------------------------------

if [ ! -d "$VENV" ]; then
    echo "  Creating virtual environment in .venv ..."
    "$PYTHON" -m venv "$VENV"
else
    echo "  Reusing the existing .venv"
fi

VENV_PYTHON="$VENV/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
    echo "  The virtual environment looks broken. Delete .venv and re-run."
    exit 1
fi

# --- Dependencies -----------------------------------------------------------

echo "  Installing dependencies ..."
"$VENV_PYTHON" -m pip install --upgrade pip --quiet
"$VENV_PYTHON" -m pip install -r "$REQUIREMENTS" --quiet
echo "  Dependencies installed"

# --- Verify -----------------------------------------------------------------

echo "  Running the test suite (this takes about a minute) ..."
TESTS_PASSED=1
( cd "$ROOT" && "$VENV_PYTHON" -m pytest tests -q ) || TESTS_PASSED=0

echo "  ----------------------------------------"
if [ "$TESTS_PASSED" -eq 1 ]; then
    echo "  Setup complete."
else
    echo "  Setup finished, but some tests failed."
    echo "  Report this to M7 before continuing."
fi

cat <<'EOF'

  Next steps:
    source .venv/bin/activate          activate the environment
    cd python_bridge && python main.py  run the bridge (mock EEG)
    python tests/fake_esp32.py         simulate the vehicle

  On Linux, bind the headset first:
    sudo rfcomm bind 0 <headset-MAC> 1
    # then set eeg.serial.port to /dev/rfcomm0 in python_bridge/config.json

  See README.md for the hardware setup.

EOF

[ "$TESTS_PASSED" -eq 1 ]
