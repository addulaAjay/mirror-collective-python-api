"""
Public, tokenized echo viewer for email recipients (no app, no login).

The echo-share email links here with a signed share token. This module renders
a branded HTML page — matching Figma "Dev Master File" node 7539:4157 (logo,
"Your Echo" title, subtitle, translucent gold-bordered media cards, quote card
with star divider, outlined GET THE APP button, lock footer) — showing the
sender's message and every attachment with inline playback + Download. A
redirect endpoint streams each attachment via a freshly-minted presigned S3 URL
(so links never expire and access stays token-gated). See core/share_token.py.

Unlike the email (which degrades in Outlook), the web page renders the real
design: backdrop-blur, gradient card fills, web fonts, and inline <video>/
<audio> players that play within the page.
"""

import html
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..core.share_token import verify_share_token
from ..models.echo import Echo
from ..services.echo_service import get_echo_service

router = APIRouter(tags=["share"])
logger = logging.getLogger(__name__)
echo_service = get_echo_service()

# Brand assets + app link, shared with the echo emails so the viewer matches.
_APP_URL = os.getenv("APP_URL", "https://mirrorcollective.com")
_ASSET_BASE = (os.getenv("EMAIL_ASSET_BASE_URL") or f"{_APP_URL}/email-assets").rstrip(
    "/"
)
_GET_APP_URL = os.getenv("APP_STORE_URL", _APP_URL)

# Never cache share responses: the viewer is per-recipient and the redirect
# resolves to a SHORT-LIVED presigned URL — a cached 302 would hand out an
# expired/dead link. Defends against a misconfigured CDN/browser cache.
_NO_STORE = {"Cache-Control": "no-store"}

# Viewer-scoped Content-Security-Policy. The global security-headers middleware
# (app/handler.py) sets a strict `default-src 'self'` CSP via `setdefault`,
# which would block EVERYTHING this page needs — brand images + inline
# <video>/<audio> served from S3, and the Google web fonts — so the browser
# refuses to load the media (it 206s fine over the wire; CSP blocks it client
# side). Because the middleware uses setdefault, setting our own CSP here wins.
# Scope: images + media from any AWS S3 host (asset bucket + vault/accelerate
# media), styles/fonts from Google, and no scripts at all.
_VIEWER_CSP = (
    "default-src 'self'; "
    "img-src 'self' https://*.amazonaws.com data:; "
    "media-src 'self' https://*.amazonaws.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "script-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
)
_PAGE_HEADERS = {"Cache-Control": "no-store", "Content-Security-Policy": _VIEWER_CSP}

_DAY_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}


def _format_date(value: Optional[str]) -> str:
    """Format an ISO date/datetime string as 'May 4th, 2025'; '' on failure."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ""
    day = dt.day
    suffix = "th" if 11 <= (day % 100) <= 13 else _DAY_SUFFIXES.get(day % 10, "th")
    return f"{dt.strftime('%B')} {day}{suffix}, {dt.year}"


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
    # Inline media plays from a DIRECT presigned S3 URL (set by the viewer) so
    # the browser's byte-range requests work — a 302 redirect breaks <video>
    # playback in Safari/Chrome. Falls back to the redirect endpoint if presign
    # failed. Download always uses the redirect (forces Content-Disposition).
    redirect_view = f"/share/echo/{echo_id}/attachment/{att['id']}?t={token}&mode=view"
    src = att.get("media_src") or redirect_view
    dl = f"/share/echo/{echo_id}/attachment/{att['id']}?t={token}&mode=download"
    name = html.escape(str(att["name"]))
    kind = att["kind"]
    lname = str(att["name"]).lower()
    if kind == "IMAGE" and (lname.endswith(".heic") or lname.endswith(".heif")):
        # Browsers can't render HEIC/HEIF — show a hint instead of a broken box.
        media = (
            '<div class="file-icon">🖼️'
            '<div class="hint">Preview not supported — download to view</div></div>'
        )
    elif kind == "IMAGE":
        media = f'<img class="media" src="{src}" alt="{name}" />'
    elif kind == "VIDEO":
        media = (
            '<video class="media" controls playsinline preload="metadata" '
            f'src="{src}"></video>'
        )
    elif kind == "AUDIO":
        media = f'<audio class="audio" controls preload="metadata" src="{src}"></audio>'
    else:
        media = '<div class="file-icon">📄</div>'
    dur = f" · {html.escape(str(att['duration']))}" if att.get("duration") else ""
    # Download icon (inline SVG so it needs no hosted asset) + label, matching
    # the design's gold download affordance.
    dl_icon = (
        '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        'stroke-linejoin="round" aria-hidden="true">'
        '<path d="M12 3v12m0 0l-4-4m4 4l4-4M5 21h14"/></svg>'
    )
    return (
        f'<div class="att card">{media}'
        f'<div class="att-row"><span class="att-name">{name}{dur}</span>'
        f'<a class="dl" href="{dl}" download>{dl_icon}<span>Download</span></a>'
        f"</div></div>"
    )


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    """Branded shell matching Figma 7539:4157: starfield navy, centered logo,
    Cormorant gold headings, translucent gold-bordered cards, outlined GET THE
    APP button with glow, lock footer."""
    doc = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="color-scheme" content="dark" />
<title>{html.escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,500;1,400&family=Inter:ital,wght@0,300;0,400;1,400&display=swap" rel="stylesheet" />
<style>
  :root {{
    --navy:#0b1020; --gold:#f2e1b0; --white:#fdfdf9; --subtle:#a3b3cc;
    --card-border:rgba(240,212,168,0.45);
    color-scheme: dark;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin:0; }}
  body {{
    background:#0b1020 url("{_ASSET_BASE}/email-bg-starfield.png") center top / cover no-repeat fixed;
    color:var(--white);
    font-family:'Inter',-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
    font-weight:300;
    -webkit-font-smoothing:antialiased;
  }}
  .wrap {{ max-width:480px; margin:0 auto; padding:40px 24px 48px; }}
  .logo-img {{ display:block; width:180px; max-width:60%; height:auto;
    margin:0 auto 28px; }}
  h1 {{ font-family:'Cormorant Garamond',Georgia,serif; color:var(--gold);
    text-align:center; font-weight:400; font-size:32px; line-height:40px;
    margin:0 0 12px; text-shadow:0 0 12px rgba(242,226,177,0.25); }}
  .subtitle {{ text-align:center; font-size:14px; line-height:20px;
    color:var(--white); margin:0 0 28px; }}
  .subtitle .it {{ font-style:italic; }}
  /* Translucent gold-bordered card — the design's backdrop-blur gradient.
     navy shows through the near-transparent fill over the starfield. */
  .card {{
    background:linear-gradient(180deg, rgba(253,253,249,0.04), rgba(253,253,249,0));
    -webkit-backdrop-filter:blur(30px); backdrop-filter:blur(30px);
    border:1px solid var(--card-border); border-radius:12px;
    margin:0 0 16px; overflow:hidden;
  }}
  .att .media {{ width:100%; display:block; background:#070b16; }}
  .att .audio {{ width:100%; display:block; padding:14px 14px 4px; }}
  .file-icon {{ font-size:40px; text-align:center; padding:26px 16px; }}
  .hint {{ font-size:13px; color:var(--subtle); margin-top:8px; font-style:italic; }}
  .att-row {{ display:flex; align-items:center; justify-content:space-between;
    padding:12px 16px; gap:12px; }}
  .att-name {{ color:var(--white); font-size:14px; overflow:hidden;
    text-overflow:ellipsis; white-space:nowrap; }}
  .dl {{ flex:none; display:inline-flex; align-items:center; gap:6px;
    color:var(--gold); text-decoration:none; font-size:14px; font-weight:400;
    border:1px solid var(--card-border); border-radius:8px; padding:7px 14px; }}
  .dl:hover {{ background:rgba(242,226,177,0.08); }}
  /* Message / quote card: centered, with the gold star divider. */
  .quote-card {{ padding:24px 22px 18px; text-align:center; }}
  .quote {{ font-size:16px; line-height:24px; color:var(--white);
    white-space:pre-wrap; }}
  .quote::before {{ content:"\\201C"; }}
  .quote::after {{ content:"\\201D"; }}
  .divider {{ display:block; width:180px; max-width:70%; height:auto;
    margin:16px auto 0; }}
  .cta {{ display:block; width:max-content; max-width:90%;
    margin:28px auto 0; font-family:'Cormorant Garamond',Georgia,serif;
    font-size:24px; line-height:28px; color:var(--gold); text-decoration:none;
    text-align:center; padding:12px 36px; border:1px solid var(--subtle);
    border-radius:12px 16px 12px 12px;
    background:linear-gradient(180deg, rgba(253,253,249,0.04), rgba(253,253,249,0));
    -webkit-backdrop-filter:blur(30px); backdrop-filter:blur(30px);
    text-shadow:0 0 15px rgba(242,226,177,0.25); }}
  .footer {{ display:flex; align-items:center; justify-content:center; gap:8px;
    color:var(--subtle); font-size:14px; line-height:20px; margin-top:28px; }}
  .footer img {{ width:13px; height:auto; opacity:0.9; }}
  .empty {{ color:var(--subtle); text-align:center; }}
</style></head>
<body><div class="wrap">
<img class="logo-img" src="{_ASSET_BASE}/logo-mirror-collective.png" alt="The Mirror Collective" />
{body}
<a class="cta" href="{html.escape(_GET_APP_URL)}">GET THE APP</a>
<div class="footer"><img src="{_ASSET_BASE}/icon-lock.png" alt="" />Shared privately through Echo Vault</div>
</div></body></html>"""
    return HTMLResponse(doc, status_code=status, headers=_PAGE_HEADERS)


def _error_page(message: str, status: int) -> HTMLResponse:
    body = "<h1>Your Echo</h1>" f'<p class="empty">{html.escape(message)}</p>'
    return _page("Your Echo", body, status)


@router.get("/share/echo/{echo_id}", response_class=HTMLResponse)
async def shared_echo_viewer(echo_id: str, t: str = Query(...)):
    """Render the recipient's echo: subtitle + all attachments (play in-page +
    download) + the sender's message, in the branded shell."""
    payload = verify_share_token(t, echo_id)
    if not payload:
        return _error_page("This link is invalid or has expired.", 403)

    echo = await echo_service.get_shared_echo(echo_id, payload["recipient_id"])
    if not echo:
        return _error_page("This echo is no longer available.", 404)

    atts = _normalize_attachments(echo)
    # Presign a direct S3 URL per attachment for inline playback (range-request
    # friendly, unlike the 302 redirect). Download links still use the redirect.
    for a in atts:
        try:
            a["media_src"] = await echo_service.presign_shared_attachment(
                echo_id, payload["recipient_id"], a["id"], download=False
            )
        except Exception as e:  # noqa: BLE001 - inline preview is best-effort
            logger.warning(f"Presign for inline view failed ({a['id']}): {e}")
            a["media_src"] = None

    message = echo.content or echo.letter_to_recipient or ""
    date_str = _format_date(echo.release_date or echo.created_at)

    # Logo, GET THE APP and footer come from _page (the branded shell).
    parts = ["<h1>Your Echo</h1>"]
    subtitle = "A private message has been shared with you"
    if date_str:
        subtitle += f' <span class="it">on {html.escape(date_str)}</span>'
    parts.append(f'<p class="subtitle">{subtitle}</p>')
    # Media first (play + download), then the sender's message as a quote card.
    parts.extend(_render_attachment(a, echo_id, t) for a in atts)
    if message:
        parts.append(
            '<div class="card quote-card">'
            f'<div class="quote">{html.escape(message)}</div>'
            f'<img class="divider" src="{_ASSET_BASE}/divider-star.png" alt="" />'
            "</div>"
        )
    elif not atts:
        parts.append('<p class="empty">This echo has no content.</p>')
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
        return Response(status_code=403, headers=_NO_STORE)
    url = await echo_service.presign_shared_attachment(
        echo_id,
        payload["recipient_id"],
        attachment_id,
        download=(mode == "download"),
    )
    if not url:
        return Response(status_code=404, headers=_NO_STORE)
    return RedirectResponse(url, status_code=302, headers=_NO_STORE)
