#!/bin/bash
# MarketMind-AlphaEngine Environment Setup
# Usage: source setup.sh
#
# Creates a .venv at the project root with all required dependencies.
# All skills use .venv/bin/python3 (relative path, never absolute).

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Find a suitable Python (3.10+)
PY=""
for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" &> /dev/null; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    echo "Error: No Python 3.10+ found. Please install Python first."
    exit 1
fi

echo "Using: $($PY --version) at $(which $PY)"

# Create .venv if not exists
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    $PY -m venv .venv
fi

# Activate and install
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q pyyaml yfinance pandas ta fpdf2 markdown flask mcp pydantic

echo ""
echo "MarketMind environment ready."
echo "Python: .venv/bin/python3"
echo "Version: $(.venv/bin/python3 --version)"
echo ""
echo "All skills use .venv/bin/python3 — no absolute paths needed."
