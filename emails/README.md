# Echo Vault recipient email (MJML)

One branded notification email sent to a recipient when an echo is shared with
them. **A single generic template covers every echo type** (written / voice /
video / mixed) — matching the Figma frame (`Design Master File`, node
`5535:5144`):

| MJML source     | Compiled HTML     | Figma frame |
| --------------- | ----------------- | ----------- |
| `src/echo.mjml` | `dist/echo.html`  | `5535:5144` |

The email is intentionally a **link, not a player**: it shows who shared an echo
plus a short cover note, then sends the recipient into the app via
`open_echo_url`. The app presents the full echo — media, attachments,
everything. There are no inline players, waveforms, attachment rows, or
per-type variants by design, which is why the three old templates
(`echo-voice` / `echo-video` / `echo-written`) and the `partials/` were removed.

## Why MJML + single column

Email HTML is table-based and Outlook uses Word's renderer. MJML compiles clean
markup into the bulletproof table/VML output every client needs. We render a
single ~600px column (the Figma **mobile** layout) for *all* clients.

Robustness choices baked in:
- `background-color` navy on `mj-body` **and** the wrapper, so light text stays
  legible when the starfield `background-url` is blocked/stripped (Outlook). A
  404 on the starfield degrades to the navy color — never a broken-image icon.
- Web fonts (Cormorant Garamond / Inter) with `Georgia` / `Arial` fallbacks —
  the serif degrades by design.
- The CTA is a bulletproof `mj-button` (VML-padded for Outlook). Both the
  "Click here to open your echo" line and the button deep-link to `open_echo_url`.
- `backdrop-blur`, the gradient card fill, and the asymmetric `border-radius`
  from the design are not reproducible in email; approximated with a solid
  midnight card (`#131a2e`) + thin gold border. See **Token gaps**.

## Build

```bash
cd emails
npm install          # installs mjml@4.15.3 locally (pinned)
npm run validate     # lint the template
npm run build        # writes dist/echo.html
npm run watch        # rebuild on change while editing
```

`dist/echo.html` is committed so deploy/runtime need no Node step. Build with the
pinned local mjml (not a global / `npx` version) so the committed output stays
deterministic.

## Template variables (Jinja2)

The compiled HTML is a Jinja2 template; placeholders use `{{ ... }}`. The sender
(`EmailService.send_echo_share_email`) fills them:

| Variable         | Example                       | Notes |
| ---------------- | ----------------------------- | ----- |
| `sender_name`    | `Jane Smith`                  | |
| `echo_date`      | `May 4th, 2025`               | Formatted by the sender, not the template. |
| `quote_text`     | one-paragraph string          | Sender's cover note, or a generic default. **Autoescaped** (sender content). |
| `open_echo_url`  | deep link / web fallback      | The link line **and** the CTA both point here. |
| `app_name`       | `Mirror Collective`           | |
| `asset_base`     | CDN base for static brand assets | logo, star divider, lock icon, starfield bg |

> There is no `{% %}` control flow in this template — it's a flat layout. If you
> ever add a `{% if %}`/`{% for %}` that wraps an `mj-section`/`mj-column`/
> `mj-text`, wrap the tag in `mj-raw` (MJML silently strips bare text nodes
> between components), then `npm run build` and `grep '{%' dist/echo.html` to
> confirm it survived.

## Integration with the Python sender

`EmailService.send_echo_share_email` renders `dist/echo.html` with Jinja2
(autoescape on — `quote_text` is sender-authored) and sends it via
`_send_email` with a plain-text alternative. `send_echo_notification` delegates
to it for registered recipients and accepts (ignores) the spread
`build_email_media_fields()` kwargs for back-compat — only `open_echo_url`
matters now. Bundle `emails/dist/` with the Lambda (`serverless.yml`
`package.patterns`).

## Hosting the brand images (required — or emails render with broken images)

The template loads images from `EMAIL_ASSET_BASE_URL` (set in `serverless.yml`).
A per-stage **public-read** bucket is created by the stack:
`mirror-collective-email-assets-<stage>`.

Upload these to the bucket **root** (filenames must match exactly):

```
logo-mirror-collective.png   divider-star.png   icon-lock.png
email-bg-starfield.png       (optional — degrades to navy if absent)
```

```bash
aws s3 sync ./email-assets \
  s3://mirror-collective-email-assets-staging/ \
  --cache-control "public, max-age=31536000"
```

> The waveform / play / download / hero / attachment-thumb images that the old
> per-type templates needed are no longer referenced — the email embeds no media.

## Token gaps (carry into design review)

- **Starfield background + gradient card fill** are images/gradients in Figma,
  not tokens, and don't survive Outlook — approximated with solid navy
  (`#0b1020`) + a midnight card (`#131a2e`). Provide `email-bg-starfield.png` to
  recover the backdrop where supported.
- **`backdrop-blur(30px)`** and the **asymmetric `border-radius` (12 / 16)** on
  the card and button have no email equivalent — flattened to a thin border +
  uniform 12px radius.
- **Glow text-shadow** on the button degrades silently outside webkit clients.
