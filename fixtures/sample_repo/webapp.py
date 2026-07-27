from flask import Flask, jsonify

app = Flask(__name__)


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/items/<int:item_id>")
def get_item(item_id: int):
    return jsonify(id=item_id, name=f"item-{item_id}")


if __name__ == "__main__":
    app.run()
