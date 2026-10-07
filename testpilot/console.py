"""Static same-origin console. Bearer authentication is enforced at every data API."""

from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).parent / "console"


def register(app: web.Application) -> None:
    async def asset(request: web.Request) -> web.Response:
        name = request.match_info.get("asset", "index.html")
        if name not in {"index.html", "app.js", "style.css"}:
            raise web.HTTPNotFound()
        content_type = {"index.html": "text/html", "app.js": "application/javascript", "style.css": "text/css"}[name]
        return web.Response(body=(ROOT/name).read_bytes(), content_type=content_type, headers={
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
        })

    app.router.add_get("/console", asset)
    app.router.add_get("/console/{asset}", asset)
