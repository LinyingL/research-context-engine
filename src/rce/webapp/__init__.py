"""Local read-only web view over the graph (DESIGN.md section 7, "Later"),
task V1.

`rce serve [path] [--port N]` starts `rce.webapp.server`'s stdlib-only HTTP
server, bound to 127.0.0.1 only -- never reachable from another machine.
Every endpoint is a read of the graph or the project filesystem; the sole
side effect anywhere in this package is `POST /api/open`, which shells out to
macOS's `open` to reveal a file in Finder or open it with its default
application, never to execute or modify project content. See
rce.webapp.server's module docstring for the full endpoint list.

V1 serves a placeholder page at `/`; V2 replaces it with a real single-page
app (`src/rce/webapp/app.html`) built against the same JSON API.
"""
