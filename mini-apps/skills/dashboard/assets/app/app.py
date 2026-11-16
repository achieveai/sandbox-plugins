#!/usr/bin/env python3
# AWB Dash app template (CGI-style Mini Web App entry). Copy the whole assets/app folder into
# mini-web-apps/<id>/, write dashboard.json and data/, then chmod +x app.py.
# Serves: GET /  -> index.html, /dashboard.json, /static/*, /data/*. Add API routes in ROUTES below.
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".json": "application/json; charset=utf-8", ".csv": "text/csv; charset=utf-8", ".svg": "image/svg+xml",
         ".png": "image/png", ".txt": "text/plain; charset=utf-8"}
SERVED_DIRS = ("static", "data")


def reply(status, ctype, body):
    out = sys.stdout.buffer
    out.write(f"Status: {status}\nContent-Type: {ctype}\n\n".encode())
    out.write(body if isinstance(body, bytes) else body.encode("utf-8"))
    out.flush()


def send_file(rel):
    path = os.path.normpath(os.path.join(HERE, rel))
    if not path.startswith(HERE + os.sep) or not os.path.isfile(path):
        return reply("404 Not Found", "text/plain; charset=utf-8", "not found")
    with open(path, "rb") as f:
        reply("200 OK", TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream"), f.read())


# Optional server-side endpoints, e.g. pre-aggregated data for big sources:
#   def usage_by_day(query): return [{"date": "2026-11-01", "cost_usd": 12.5}, ...]
#   ROUTES = {"/api/usage-by-day": usage_by_day}
# The spec then uses {"url": "api/usage-by-day"} (relative, no leading slash).
ROUTES = {}


def main():
    method = os.environ.get("REQUEST_METHOD", "GET")
    path = os.environ.get("PATH_INFO", "/") or "/"
    if method != "GET":
        return reply("405 Method Not Allowed", "text/plain; charset=utf-8", "GET only")
    if path in ROUTES:
        return reply("200 OK", TYPES[".json"], json.dumps(ROUTES[path](os.environ.get("QUERY_STRING", ""))))
    if path in ("/", "/index.html"):
        return send_file("index.html")
    if path == "/dashboard.json":
        return send_file("dashboard.json")
    parts = path.lstrip("/").split("/", 1)
    if len(parts) == 2 and parts[0] in SERVED_DIRS and ".." not in parts[1]:
        return send_file(path.lstrip("/"))
    reply("404 Not Found", "text/plain; charset=utf-8", "not found")


main()
