#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-$(git -C "$ROOT" rev-parse --short HEAD)}"

python3 - "$ROOT" "$VERSION" <<'PY'
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
version = sys.argv[2].strip()
if not re.fullmatch(r"[0-9a-fA-F]{7,40}", version):
    raise SystemExit(f"Refusing invalid asset version: {version!r}")

index = root / "frontend" / "index.html"
styles = root / "frontend" / "styles.css"
sw = root / "frontend" / "sw.js"

def write_if_changed(path: Path, text: str) -> None:
    if path.read_text() != text:
        path.write_text(text)

html = index.read_text()
html = re.sub(r'href="/manifest\.json(?:\?v=[^"]*)?"', f'href="/manifest.json?v={version}"', html)
html = re.sub(r'href="/styles\.css(?:\?v=[^"]*)?"', f'href="/styles.css?v={version}"', html)
html = re.sub(r'src="/app\.js(?:\?v=[^"]*)?"', f'src="/app.js?v={version}"', html)
write_if_changed(index, html)

css = styles.read_text()
if re.search(r"asset-version:\s*[0-9a-fA-F]{7,40}", css):
    css = re.sub(r"asset-version:\s*[0-9a-fA-F]{7,40}", f"asset-version: {version}", css, count=1)
else:
    css = f"/* asset-version: {version} */\n" + css
write_if_changed(styles, css)

sw_text = sw.read_text()
sw_text = re.sub(
    r"const CACHE = 'jarvis-[^']+';",
    f"const CACHE = 'jarvis-{version}';",
    sw_text,
    count=1,
)
shell = (
    "const SHELL = ['/', '/index.html', "
    f"'/styles.css?v={version}', '/app.js?v={version}', '/manifest.json?v={version}'];"
)
sw_text = re.sub(r"const SHELL = \[[^\n]+\];", shell, sw_text, count=1)
write_if_changed(sw, sw_text)
PY

echo "Stamped frontend assets with version ${VERSION}"
