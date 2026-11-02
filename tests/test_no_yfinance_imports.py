"""Yahoo/yfinance must not be reachable from any pipeline code — not as a
source and not as a fallback. AST scan, so comments and legacy-format notes
don't trip it; only real imports do."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE_DIRS = ("mcp", "scripts", "eval", "normalize", "valuation", "templates",
             "web", "memory", "ledger")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_no_code_imports_yfinance():
    offenders = []
    for d in CODE_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if "yfinance" in _imports(path):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_setup_does_not_install_yfinance():
    assert "yfinance" not in (ROOT / "setup.sh").read_text(encoding="utf-8")
