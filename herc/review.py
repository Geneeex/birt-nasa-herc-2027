"""Read-only offline evidence viewer. No task-control endpoint exists."""

import argparse
import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


def make_handler(root):
    root = Path(root).resolve()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            name = unquote(urlsplit(self.path).path)
            if name == "/":
                parts = [
                    '<!doctype html><meta charset="utf-8"><title>HERC results</title>',
                    '<meta name="viewport" content="width=device-width,initial-scale=1">',
                    "<style>body{font:18px system-ui;max-width:1000px;margin:30px auto;padding:0 18px}img{max-width:100%}pre{white-space:pre-wrap}article{border-bottom:1px solid #ccc;margin-bottom:35px}</style>",
                    "<h1>HERC task results</h1><p>Saved evidence only. All displayed times are UTC. Refresh to view completed results.</p>",
                ]
                for manifest in sorted(root.glob("*/manifest.json"), reverse=True):
                    try:
                        obj = json.loads(manifest.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    parts += [
                        "<article><h2>" + html.escape(manifest.parent.name) + "</h2>",
                        "<p>"
                        + html.escape(str(obj.get("mode")))
                        + " | "
                        + html.escape(str(obj.get("status")))
                        + "</p>",
                        "<p>Commands: "
                        + html.escape(" ".join(obj.get("commands", [])))
                        + "</p>",
                    ]
                    for item in obj.get("results", []):
                        parts += [
                            "<pre>" + html.escape(json.dumps(item, indent=2)) + "</pre>"
                        ]
                        for key in ("original", "result"):
                            image = item.get(key)
                            if isinstance(image, dict) and image.get("file"):
                                url = (
                                    "/"
                                    + manifest.parent.name
                                    + "/"
                                    + str(image["file"])
                                )
                                parts += [
                                    "<p>"
                                    + key
                                    + '</p><img alt="Saved task image" src="'
                                    + html.escape(url, quote=True)
                                    + '">'
                                ]
                    parts += ["</article>"]
                content = "\n".join(parts).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            else:
                try:
                    target = (root / name.lstrip("/")).resolve()
                except (OSError, ValueError):
                    self.send_error(404)
                    return
                if (
                    root not in target.parents
                    or target.suffix.lower() not in (".png", ".json")
                    or not target.is_file()
                ):
                    self.send_error(404)
                    return
                try:
                    content = target.read_bytes()
                except OSError:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    (
                        "image/png"
                        if target.suffix.lower() == ".png"
                        else "application/json"
                    ),
                )
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="runs")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), make_handler(args.root)).serve_forever()


if __name__ == "__main__":
    main()
