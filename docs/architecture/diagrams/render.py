"""Render the Graphviz architecture figures as self-contained SVG files."""
import base64
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def embed_icon(match):
    data = (ROOT / match[1]).read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    return f'xlink:href="data:image/png;base64,{encoded}"'


if __name__ == "__main__":
    for source in sorted((ROOT / "sources").glob("*.dot")):
        svg = subprocess.check_output(["dot", "-Tsvg", source], cwd=ROOT, text=True)
        svg = re.sub(r'xlink:href="(icons/[^"]+)"', embed_icon, svg)
        (ROOT / "exports" / source.with_suffix(".svg").name).write_text(svg, encoding="utf-8")
