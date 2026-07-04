# Markdown and PDF Preview Design

## Goal

Allow users on the public download page to preview PDF and Markdown files without losing the existing forced-download behavior.

## Current Context

The public page is a single Vue CDN page in `frontend/public/index.html`. Files are downloaded through `/files/<stored_filename>`.

Nginx currently forces all `/files` responses to download by setting:

```nginx
Content-Disposition: attachment
Content-Type: application/octet-stream
```

That makes browser-native PDF preview impossible and also prevents Markdown from being fetched as normal text for preview.

## Approach

Keep `/files` as the download-only path and add a separate inline preview path:

```text
/preview/<stored_filename>
```

The frontend will derive the preview URL from the existing `f.url` by replacing the `/files/` prefix with `/preview/`.

Supported preview types:

- `.pdf`: open a modal with an `iframe` pointing at `/preview/...`
- `.md` and `.markdown`: fetch text from `/preview/...`, sanitize rendered HTML, and show it in a modal

Download buttons remain unchanged.

## Frontend Behavior

Rows for supported files show a secondary `预览` button beside `下载`.

The history modal also shows `预览` for supported historical versions.

Preview modal behavior:

- Opens over the current page.
- Has file name, close button, and download button.
- PDF uses browser-native rendering through an `iframe`.
- Markdown uses the `marked` CDN library and a small sanitizer that removes scriptable tags and event attributes from the generated HTML.
- Fetch failures show a short error state and keep the download option available.

## Nginx Behavior

`/files` remains forced download.

`/preview` reads from the same mounted uploads directory through `root /usr/share/nginx/html`, but does not set `Content-Disposition: attachment`.

Nginx sets common preview content types:

- `.pdf` as `application/pdf`
- `.md` and `.markdown` as `text/markdown; charset=utf-8`

## Testing

Manual verification is required because this is a CDN-driven static Vue page:

1. Start or use the existing Nginx-served frontend.
2. Upload or place a PDF and Markdown file in the app.
3. Confirm `预览` appears only for PDF/Markdown.
4. Confirm PDF opens in the modal.
5. Confirm Markdown renders in the modal.
6. Confirm `下载` still downloads files.

## Out of Scope

- Admin page previews.
- Converting Markdown on the backend.
- Full PDF.js integration.
- Editing Markdown files.
