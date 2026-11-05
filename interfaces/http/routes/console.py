"""Self-hosted console shell; credentials stay in memory and APIs retain all authority."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

router = APIRouter(tags=["console"])
_ASSETS = Path(__file__).resolve().parents[1] / "console"
_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                               "img-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


@router.get("/", include_in_schema=False)
def console_redirect():
    return RedirectResponse("/console/", headers={"Cache-Control": "no-store"})


@router.get("/console/", include_in_schema=False)
def console_shell():
    return FileResponse(_ASSETS / "index.html", media_type="text/html", headers=_HEADERS)


@router.get("/console/{asset}", include_in_schema=False)
def console_asset(asset: str):
    types = {"app.js": "text/javascript", "app.css": "text/css", "icon.svg": "image/svg+xml"}
    if asset not in types:
        raise HTTPException(status_code=404)
    return FileResponse(_ASSETS / asset, media_type=types[asset], headers=_HEADERS)
