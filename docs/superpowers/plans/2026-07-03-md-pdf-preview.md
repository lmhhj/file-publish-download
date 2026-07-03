# Markdown and PDF Preview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add public-page preview support for PDF and Markdown while preserving forced downloads.

**Architecture:** Add a separate Nginx `/preview` path for inline-safe reads from the same uploads directory. In `frontend/public/index.html`, add preview detection, a modal state, PDF iframe preview, Markdown fetch/render preview using `marked`, and secondary preview buttons for current and historical file rows.

**Tech Stack:** Static HTML, Vue 3 CDN, Tailwind CDN classes, marked CDN, Nginx.

---

## Task 1: Add Inline Preview Route

**Files:**
- Modify: `frontend/nginx.conf`

- [ ] Add a `location /preview` block before `location /files`.
- [ ] Keep `/files` unchanged as forced download.
- [ ] Verify `nginx -t` through Docker or by starting the frontend container.

## Task 2: Add Preview Modal and Markdown Renderer

**Files:**
- Modify: `frontend/public/index.html`

- [ ] Add marked CDN script in the page head.
- [ ] Add preview buttons next to download buttons for supported files.
- [ ] Add preview buttons in the history modal.
- [ ] Add a preview modal with PDF iframe, Markdown rendered content, loading state, and error state.
- [ ] Add Vue helpers: `canPreview`, `previewUrl`, `openPreview`, `closePreview`, `sanitizeHtml`, `renderMarkdown`.

## Task 3: Manual Browser Verification

**Files:**
- Read-only verification

- [ ] Start the app with Docker Compose or a local static server when possible.
- [ ] Open the public page in a browser.
- [ ] Confirm PDF and Markdown rows show preview buttons.
- [ ] Confirm preview modal works for PDF and Markdown.
- [ ] Confirm download still works.
- [ ] Run backend tests to ensure unrelated backend behavior remains green.
