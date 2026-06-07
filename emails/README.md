# Echo Vault recipient emails (MJML)

Rich, branded notification emails sent to a recipient when an echo is shared
with them. One template per echo type, matching the Figma "Email Templates"
frame (`Dev Master File`, node `7539:2963`):

| Echo type | MJML source              | Compiled HTML              | Figma frame        |
| --------- | ------------------------ | -------------------------- | ------------------ |
| Voice     | `src/echo-voice.mjml`    | `dist/echo-voice.html`     | `7539:3358` / `…4157` |
| Video     | `src/echo-video.mjml`    | `dist/echo-video.html`     | `7539:2975` / `…4279` |
| Written   | `src/echo-written.mjml`  | `dist/echo-written.html`   | `7539:3775` / `…4387` |

Shared chrome lives in `src/partials/` (`_head`, `_header`, `_footer`, `_cta`)
and is pulled into each variant with `<mj-include>`.

> **Status: scaffold / draft.** Structure, tokens, and email-robustness patterns
> are in place. Real asset URLs, final copy, and the Python wiring are still
> open — see **Open questions** below.

## Why MJML + single column

Email HTML is table-based and Outlook uses Word's renderer. MJML compiles clean
markup into the bulletproof table/VML output every client needs. We render a
single ~600px column (the Figma **mobile** layout) for *all* clients — the 1440px
"desktop" frame is a canvas preview, and side-by-side image+text is the main
source of Outlook bugs. See `../docs` if a cross-client decision log is added.

Robustness choices baked in:
- `bgcolor` navy on every section **and** `mj-style`, so light text stays legible
  when the starfield image is blocked/stripped (Outlook).
- Web fonts with `Georgia` / `Arial` fallbacks — the serif degrades by design.
- **No inline audio/video.** Players are static images linked to `open_echo_url`.
  The video play button overlays the thumbnail via `mj-hero` (emits Outlook VML);
  duration badges render as a row, not an absolute-positioned overlay.
- Attachment shown as its own row (the mobile pattern), never an overlay caption.
- CTA is a bulletproof `mj-button`, rendered on every client (not mobile-only).

## Build

```bash
cd emails
npm install          # installs mjml locally
npm run validate     # lint all three templates
npm run build        # writes dist/*.html
npm run watch        # rebuild on change while editing
```

`dist/*.html` is committed so deploy/runtime need no Node step. If you move
compilation into CI, ignore `dist/` and build there instead.

## Template variables (Jinja2)

Placeholders use `{{ ... }}` / `{% ... %}` and the compiled HTML is a Jinja2
template. Names match `tokens.json` (`links`, `assets`) so MJML and the sender
never drift.

> **Editing rule — block-level control flow needs `mj-raw`.** Inline `{{ var }}`
> and conditionals *inside* an `mj-text` (e.g. pluralization, `loop.first`)
> survive as-is. But a `{% if %}`/`{% for %}` that wraps an `mj-section`,
> `mj-column`, or `mj-text` is a bare text node between MJML components and MJML
> **silently strips it** — breaking the loop/conditional with no error. Wrap
> those tags in `mj-raw`: `<mj-raw>{% if attachment_count %}</mj-raw>` … then the
> section … `<mj-raw>{% endif %}</mj-raw>`. After any edit, run `npm run build`
> and `grep '{%' dist/*.html` to confirm the tags survived.

| Variable                | Example                              | Notes |
| ----------------------- | ------------------------------------ | ----- |
| `sender_name`           | `Jane Smith`                         | |
| `echo_date`             | `May 4th, 2025`                      | Formatted by the sender, not the template. |
| `quote_text`            | one-paragraph string                 | voice / video |
| `quote_paragraphs`      | `list[str]`                          | written (multi-paragraph) |
| `audio_duration`        | `2:32`                               | voice |
| `video_duration`        | `1:25`                               | video |
| `attachment_count`      | `0`, `1`, `3`                        | drives the row + pluralization |
| `attachment_url`        | signed download URL                  | soft-delete safe; resolves for returning users |
| `attachment_thumb_url`  | CDN URL                              | |
| `hero_image_url`        | CDN URL                              | |
| `open_echo_url`         | deep link / web fallback             | every "play"/thumbnail/CTA points here |
| `app_name`              | `Mirror Collective`                  | |
| `asset_base`            | CDN base for static brand assets     | logo, icons, waveform, divider |

## Integration with the Python sender

`src/app/services/email_service.py` currently builds HTML with inline f-strings.
To adopt these templates, render the compiled HTML with Jinja2 and pass it to the
existing `_send_email`. Sketch (new method on `EmailService`):

```python
from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATES = {"AUDIO": "echo-voice.html", "VIDEO": "echo-video.html", "TEXT": "echo-written.html"}
_env = Environment(
    loader=FileSystemLoader("emails/dist"),
    autoescape=select_autoescape(["html"]),  # quote_text is user content — must escape
)

async def send_echo_share_email(self, *, to_email, echo_type, ctx: dict) -> bool:
    template = _env.get_template(_TEMPLATES[echo_type])
    html = template.render(app_name=self.app_name, **ctx)
    text = self._echo_share_text(echo_type, ctx)   # plain-text alternative (required)
    subject = f"Your {echo_type.title()} Echo from {ctx['sender_name']}"
    return await self._send_email(to_email, subject, html, text)
```

Notes:
- `jinja2` is not yet a dependency — add to `requirements.txt`.
- **Autoescape on:** `quote_text`/`quote_paragraphs` are sender-authored — escape
  to prevent HTML injection into the email.
- A **plain-text part is mandatory** (deliverability + accessibility); `_send_email`
  already takes `text_body`. Author one per type.
- Bundle `emails/dist/` with the Lambda (check `serverless.yml` `package.include`).

## Hosting the brand images (required — or emails render with broken images)

The templates load images from `EMAIL_ASSET_BASE_URL` (set in `serverless.yml`).
A per-stage **public-read** bucket is created by the stack:
`mirror-collective-email-assets-<stage>` →
`https://mirror-collective-email-assets-<stage>.s3.us-east-1.amazonaws.com`.

After the first `serverless deploy` (which creates the bucket), upload the 8
brand images to the bucket **root** (filenames must match exactly):

```
logo-mirror-collective.png   divider-star.png      waveform-gold.png
icon-play-gold.png           icon-download.png     icon-lock.png
hero-default.jpg             attachment-thumb.png
```

```bash
# from a folder containing the 8 files, for the stage you deployed:
aws s3 sync ./email-assets \
  s3://mirror-collective-email-assets-staging/ \
  --cache-control "public, max-age=31536000"

# verify one is publicly fetchable:
curl -I https://mirror-collective-email-assets-staging.s3.us-east-1.amazonaws.com/logo-mirror-collective.png
# expect: HTTP/1.1 200 OK, Content-Type: image/png
```

Public read is granted by `EmailAssetsBucketPolicy` (GetObject for `*`); no
listing or writes are public. To use one shared bucket / CloudFront across all
stages instead, override `EMAIL_ASSET_BASE_URL` per stage.

## Token gaps (carry into design review)

- **Background navy** (`color.bg.*` in `tokens.json`) are not Figma variables —
  the design uses an image+gradient. Approximated from the screenshot. Replace
  with real `Bg/*` tokens when design adds them.
- **Title size** (~44px) has no variable.
- **"GET THE APP"** appears only on the mobile frame; we render it everywhere.
- **Gradient gold border / `border-radius`** degrade to solid border / square in
  Outlook — confirm acceptable.

## Open questions (blocking a real send)

1. Final asset CDN + the actual brand PNGs (logo, gold play, lock, download,
   waveform, star divider).
2. `open_echo_url` shape — deep link vs web viewer vs app-store fallback for
   non-registered recipients (the sender already branches on `is_registered`).
3. Quote copy: author-written per echo, or templated boilerplate?
4. Final subject lines + preview text per type.
5. Litmus / Email-on-Acid pass (Outlook Win, Gmail, Apple Mail, iOS; dark mode
   on/off; images on/off) before launch.
