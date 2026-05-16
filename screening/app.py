"""
Screening WebApp — Flask mini-service for the health screening quiz.

Serves two HTML pages with dual-mode support:
- Telegram WebApp (when opened inside Telegram mini-app)
- Standalone browser (when opened from WhatsApp deep-link)

In standalone mode, the page shows results directly and offers a
"Write to bot on WhatsApp" deep-link with pre-filled result text.
No server-side state — pure static HTML + JS.

Designed for zero-config deployment on PythonAnywhere free tier.
"""
from __future__ import annotations

import os

from flask import Flask, render_template

app = Flask(__name__)


# Default WhatsApp phone number — overridable via query param ?wa=77XXX
# This is embedded in the pre-filled message users send back after the test
WA_PHONE_DEFAULT = os.environ.get("WA_PHONE", "77773997793")


# ── Routes ───────────────────────────────────────────────────────────────────


@app.route("/")
def screening_page():
    """Serve the 19-symptom screening quiz."""
    return render_template("screening.html")


@app.route("/capillary")
def capillary_page():
    """Serve the 15-sign capillary test."""
    return render_template("capillary.html")


@app.route("/health")
def health():
    """Health check endpoint for uptime monitoring."""
    return {"status": "ok"}, 200


# ── Entry point (local dev only; PythonAnywhere uses WSGI) ───────────────────


if __name__ == "__main__":
    port = int(os.environ.get("SCREENING_PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
