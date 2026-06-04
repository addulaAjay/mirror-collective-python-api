"""
Public, tokenized echo viewer for email recipients (no app, no login).

The echo-share email links here with a signed share token. This module renders
a branded HTML page showing the sender's message and every attachment with
inline Play + Download, and a redirect endpoint that streams each attachment via
a freshly-minted presigned S3 URL (so links never expire and access stays
token-gated). See core/share_token.py.
"""

import html
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..core.share_token import verify_share_token
from ..models.echo import Echo
from ..services.echo_service import get_echo_service

router = APIRouter(tags=["share"])
logger = logging.getLogger(__name__)
echo_service = get_echo_service()


def _normalize_attachments(echo: Echo) -> List[Dict[str, Any]]:
    """Flatten attachments to render-friendly dicts; synth a primary for legacy."""
    out: List[Dict[str, Any]] = []
    for a in echo.attachments or []:
        out.append(
            {
                "id": a.attachment_id,
                "kind": a.type.value,
                "name": a.filename or f"{a.type.value.lower()} attachment",
                "duration": a.duration,
            }
        )
    if not out and echo.media_url:
        kind = echo.echo_type.value
        out.append(
            {
                "id": "primary",
                "kind": kind if kind in ("AUDIO", "VIDEO") else "FILE",
                "name": "attachment",
                "duration": None,
            }
        )
    return out


def _render_attachment(att: Dict[str, Any], echo_id: str, token: str) -> str:
    view = f"/share/echo/{echo_id}/attachment/{att['id']}?t={token}&mode=view"
    dl = f"/share/echo/{echo_id}/attachment/{att['id']}?t={token}&mode=download"
    name = html.escape(str(att["name"]))
    kind = att["kind"]
    if kind == "IMAGE":
        media = f'<img class="media" src="{view}" alt="{name}" />'
    elif kind == "VIDEO":
        media = (
            f'<video class="media" controls preload="metadata" src="{view}"></video>'
        )
    elif kind == "AUDIO":
        media = (
            f'<audio class="audio" controls preload="metadata" src="{view}"></audio>'
        )
    else:
        media = f'<div class="file-icon">📄</div>'
    dur = f" · {html.escape(str(att['duration']))}" if att.get("duration") else ""
    return (
        f'<div class="att">{media}'
        f'<div class="att-row"><span class="att-name">{name}{dur}</span>'
        f'<a class="dl" href="{dl}">Download</a></div></div>'
    )


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="color-scheme" content="dark" />
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#0b1020; color:#fdfdf9;
    font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }}
  .wrap {{ max-width:600px; margin:0 auto; padding:40px 20px; }}
  .logo {{ text-align:center; color:#f2e1b0; letter-spacing:3px;
    font-size:14px; margin-bottom:8px; }}
  h1 {{ font-family:Georgia,'Times New Roman',serif; color:#f2e1b0;
    text-align:center; font-weight:400; font-size:32px; margin:8px 0 24px; }}
  .msg {{ background:#131a2e; border:1px solid #2a3450; border-radius:12px;
    padding:20px; line-height:1.5; white-space:pre-wrap; }}
  .section {{ color:#f2e1b0; text-align:center; margin:28px 0 16px;
    font-family:Georgia,serif; }}
  .att {{ background:#131a2e; border:1px solid #2a3450; border-radius:12px;
    overflow:hidden; margin-bottom:16px; }}
  .media {{ width:100%; display:block; background:#0b1020; }}
  .audio {{ width:100%; display:block; padding:12px; }}
  .file-icon {{ font-size:40px; text-align:center; padding:24px; }}
  .att-row {{ display:flex; align-items:center; justify-content:space-between;
    padding:12px 16px; gap:12px; }}
  .att-name {{ color:#a3b3cc; font-size:14px; overflow:hidden;
    text-overflow:ellipsis; white-space:nowrap; }}
  .dl {{ flex:none; color:#0b1020; background:#f2e1b0; text-decoration:none;
    font-weight:600; padding:8px 16px; border-radius:8px; font-size:14px; }}
  .footer {{ color:#a3b3cc; font-size:12px; text-align:center; margin-top:32px; }}
  .empty {{ color:#a3b3cc; text-align:center; }}
</style></head>
<body><div class="wrap">{body}</div></body></html>"""
    return HTMLResponse(doc, status_code=status)


def _error_page(message: str, status: int) -> HTMLResponse:
    body = (
        '<div class="logo">THE MIRROR COLLECTIVE</div>'
        "<h1>Echo</h1>"
        f'<p class="empty">{html.escape(message)}</p>'
    )
    return _page("Echo", body, status)


@router.get("/share/echo/{echo_id}", response_class=HTMLResponse)
async def shared_echo_viewer(echo_id: str, t: str = Query(...)):
    """Render the recipient's echo: message + all attachments (play + download)."""
    payload = verify_share_token(t, echo_id)
    if not payload:
        return _error_page("This link is invalid or has expired.", 403)

    echo = await echo_service.get_shared_echo(echo_id, payload["recipient_id"])
    if not echo:
        return _error_page("This echo is no longer available.", 404)

    atts = _normalize_attachments(echo)
    message = echo.content or echo.letter_to_recipient or ""
    parts = ['<div class="logo">THE MIRROR COLLECTIVE</div>', "<h1>Your Echo</h1>"]
    if message:
        parts.append(f'<div class="msg">{html.escape(message)}</div>')
    if atts:
        parts.append('<div class="section">Attachments</div>')
        parts.extend(_render_attachment(a, echo_id, t) for a in atts)
    elif not message:
        parts.append('<p class="empty">This echo has no content.</p>')
    parts.append('<div class="footer">Shared privately through Echo Vault.</div>')
    return _page("Your Echo", "".join(parts))


@router.get("/share/echo/{echo_id}/attachment/{attachment_id}")
async def shared_attachment_redirect(
    echo_id: str,
    attachment_id: str,
    t: str = Query(...),
    mode: str = Query("view"),
):
    """302 to a fresh presigned URL; mode=download forces Content-Disposition."""
    payload = verify_share_token(t, echo_id)
    if not payload:
        return Response(status_code=403)
    url = await echo_service.presign_shared_attachment(
        echo_id,
        payload["recipient_id"],
        attachment_id,
        download=(mode == "download"),
    )
    if not url:
        return Response(status_code=404)
    return RedirectResponse(url, status_code=302)
