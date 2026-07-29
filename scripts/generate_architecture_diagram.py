"""One-off script: renders frontend/public/architecture.svg.

Not part of the app build — rerun manually with a throwaway venv:
    python3 -m venv /tmp/architecture-venv
    /tmp/architecture-venv/bin/pip install diagrams
    /tmp/architecture-venv/bin/python3 scripts/generate_architecture_diagram.py
if the diagram needs to change. Requires graphviz's `dot` on PATH (brew install graphviz).

Mirrors the "Architecture" section of README.md — keep the two in sync when the
infra shape changes.

See scripts/generate_how_it_works_diagram.py for why the SVG's <image> refs get
inlined as base64 data URIs (committed static asset can't ship /tmp filesystem paths).
"""

import base64
import re
from pathlib import Path

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import Fargate, Lambda
from diagrams.aws.database import Dynamodb
from diagrams.aws.integration import SQS
from diagrams.aws.network import APIGateway, CloudFront
from diagrams.aws.storage import S3, ElasticFileSystemEFS
from diagrams.onprem.client import User
from diagrams.onprem.vcs import Github

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "frontend" / "public" / "architecture"
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
    "nodesep": "0.6",
    "ranksep": "0.9",
    "fontname": "Helvetica",
    "margin": "0.2",
    "pad": "0.05",
}
node_attr = {
    "fontname": "Helvetica",
    "fontsize": "12",
    "fontcolor": "#333333",
}
edge_attr = {
    "color": "#666666",
    "penwidth": "1.5",
    "fontname": "Helvetica",
    "fontsize": "10",
    "fontcolor": "#555555",
}

with Diagram(
    "",
    filename=str(OUTPUT_PATH),
    outformat="svg",
    show=False,
    direction="TB",
    graph_attr=graph_attr,
    node_attr=node_attr,
    edge_attr=edge_attr,
):
    browser = User("Browser\n(Next.js static export)")

    with Cluster("Static hosting"):
        cloudfront = CloudFront("CloudFront\n(+ SPA routing fn)")
        bucket = S3("S3 (static assets)")
        cloudfront >> Edge(label="origin") >> bucket

    apigw = APIGateway("API Gateway\n(HTTP API)")
    lambda_api = Lambda("Lambda\n(FastAPI + Mangum)")
    sqs = SQS("SQS\nrepomod-tasks")
    lambda_consumer = Lambda("Lambda\n(SQS consumer)")
    dynamo = Dynamodb("DynamoDB\nrepomod-checkpoints")

    with Cluster("Fargate worker (one-shot, no standing service)"):
        fargate = Fargate("Fargate task")
        efs = ElasticFileSystemEFS("EFS\n/mnt/workspace")
        fargate >> Edge(label="workspace") >> efs

    github = Github("GitHub")

    browser >> Edge(label="fetch UI") >> cloudfront
    browser >> Edge(label="fetch/CORS:\nPOST /tasks\nGET /tasks/{id}\nPOST .../approve, /resume") >> apigw
    apigw >> Edge() >> lambda_api
    lambda_api >> Edge(label="enqueue") >> sqs
    lambda_api >> Edge(label="GET: read state\n(no LLM/GitHub calls)", style="dashed") >> dynamo
    sqs >> Edge(label="trigger") >> lambda_consumer
    lambda_consumer >> Edge(label="ecs:RunTask\n(1 task / message)") >> fargate
    fargate >> Edge(label="checkpoint\nevery step") >> dynamo
    fargate >> Edge(label="clone, commit,\npush, open PR") >> github

inline_image_references(OUTPUT_SVG)
