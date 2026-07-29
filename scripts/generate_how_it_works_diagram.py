"""One-off script: renders frontend/public/how-it-works.svg.

Not part of the app build — rerun manually with a throwaway venv:
    python3 -m venv /tmp/how-it-works-venv
    /tmp/how-it-works-venv/bin/pip install diagrams
    /tmp/how-it-works-venv/bin/python3 scripts/generate_how_it_works_diagram.py
if the diagram needs to change. Requires graphviz's `dot` on PATH (brew install graphviz).

Icons come straight from the `diagrams` package's bundled resource PNGs (installed as a
sibling `resources/` dir next to the `diagrams` module in site-packages) — no manual icon
prep needed, unlike terraform-agent's version of this script.

Graphviz's node `image` attribute does not reliably rasterize SVG node icons (it silently
drops them with no error), which is why we use the pre-rendered PNGs directly instead of SVGs.

Graphviz also writes the image reference as a literal filesystem path (e.g. `/tmp/.../user.png`)
rather than embedding the image data. A committed static asset can't ship with a `/tmp`
reference — browsers block loading `file://` paths from an `http://` page, and the path
wouldn't exist on any other machine anyway. This script inlines every `<image>` reference as a
base64 `data:` URI after graphviz generates the SVG, so the final `how-it-works.svg` is a single
self-contained file.
"""

import base64
import re
from pathlib import Path

import diagrams
from diagrams import Diagram, Edge
from diagrams.custom import Custom

RESOURCES_DIR = Path(diagrams.__file__).resolve().parent.parent / "resources"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "frontend" / "public" / "how-it-works"
OUTPUT_SVG = OUTPUT_PATH.with_suffix(".svg")


def inline_image_references(svg_path: Path) -> None:
    svg_text = svg_path.read_text()

    def replace(match: re.Match) -> str:
        image_path = Path(match.group(1))
        png_bytes = image_path.read_bytes()
        encoded = base64.b64encode(png_bytes).decode("ascii")
        return f'xlink:href="data:image/png;base64,{encoded}"'

    inlined = re.sub(r'xlink:href="([^"]+\.png)"', replace, svg_text)
    svg_path.write_text(inlined)


graph_attr = {
    "bgcolor": "transparent",
    "splines": "ortho",
    "nodesep": "1.0",
    "ranksep": "1.2",
    "fontname": "Helvetica",
    "margin": "0.15",
    "pad": "0.05",
}
node_attr = {
    "fontname": "Helvetica",
    "fontsize": "14",
    "fontcolor": "#333333",
}
edge_attr = {
    "color": "#666666",
    "penwidth": "2",
}

with Diagram(
    "",
    filename=str(OUTPUT_PATH),
    outformat="svg",
    show=False,
    direction="LR",
    graph_attr=graph_attr,
    node_attr=node_attr,
    edge_attr=edge_attr,
):
    describe = Custom("You give it a repo\n& a goal", str(RESOURCES_DIR / "onprem" / "client" / "user.png"))
    plan = Custom("Agent plans the\nmigration, file by file", str(RESOURCES_DIR / "programming" / "flowchart" / "document.png"))
    migrate = Custom("Agent rewrites & tests\neach file, pauses on risk", str(RESOURCES_DIR / "programming" / "language" / "python.png"))
    deliver = Custom("You get an opened\npull request", str(RESOURCES_DIR / "onprem" / "vcs" / "github.png"))

    describe >> Edge() >> plan >> Edge() >> migrate >> Edge() >> deliver

inline_image_references(OUTPUT_SVG)
