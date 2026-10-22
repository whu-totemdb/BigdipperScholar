"""Check the tracked/exported source surface without printing secret values."""
import ast
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = [
    re.compile(r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"),
    re.compile(r"/(?:data[0-9]+|home)/[^\s\"']+"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"display-(?:similar|source)-|demo-similar-|synthetic:\s*true"),
]


def main():
    failures = []
    paths = list((ROOT / "backend/app").rglob("*.py")) + list((ROOT / "backend/prompts").rglob("*.md"))
    paths += [path for path in (ROOT / "frontend/src").rglob("*") if path.is_file()]
    for path in paths:
        content = path.read_text()
        if any(pattern.search(content) for pattern in PATTERNS):
            failures.append(f"private deployment or synthetic display pattern: {path.relative_to(ROOT)}")
        if path.suffix == ".py":
            for node in ast.walk(ast.parse(content)):
                if not isinstance(node, ast.Call) or len(node.args) < 2:
                    continue
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                key, default = node.args[:2]
                if name in {"getenv", "get_env"} and isinstance(key, ast.Constant) and isinstance(key.value, str):
                    if any(part in key.value for part in ("PASSWORD", "API_KEY", "SECRET")) and isinstance(default, ast.Constant) and default.value:
                        failures.append(f"nonempty credential default: {path.relative_to(ROOT)}:{node.lineno}")
    if (ROOT / ".git").exists():
        tracked = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()
        for filename in tracked:
            path = Path(filename)
            if path.name == ".env" or path.suffix in {".db", ".sqlite", ".pyc"} or any(part in path.parts for part in ("node_modules", ".venv", "logs")):
                failures.append(f"local artifact tracked: {filename}")
    for failure in failures:
        print(failure)
    print(f"Release audit: {len(paths)} source files checked; {len(failures)} findings.")
    return bool(failures)


if __name__ == "__main__":
    sys.exit(main())
