"""
Daily Briefing & Research Agent — SINGLE FILE, WEB SERVICE VERSION
====================================================================
Everything (feed fetching, summarization, emailing) lives in this one file,
wrapped as a tiny Flask app so it can run as a free Web Service on Render
and be triggered by an HTTP request (from a free scheduler, your browser,
your phone, curl, anything) — "run my agent from anywhere."

FREE STACK USED:
  - RSS feeds              -> free, no API key
  - Groq API               -> free tier, no credit card, no expiry
  - Resend Email API       -> free tier, 100 emails/day, no credit card
                              (used instead of Gmail SMTP because Render's
                              free web services block outbound SMTP ports
                              25/465/587 — this uses plain HTTPS instead)
  - Render Web Service     -> free tier (750 instance-hrs/month)
  - cron-job.org           -> free external scheduler that "wakes" this
                              service once a day (Render's own Cron Job
                              product is NOT free, so we avoid it)

Endpoints:
  GET /                -> health check (JSON) or a "Run Digest Now" button page (browser)
  GET /run?key=SECRET  -> starts the pipeline in the background (fetch -> summarize -> email)

Environment variables required (set these in Render's dashboard, never in code):
  GROQ_API_KEY        - from https://console.groq.com
  RESEND_API_KEY      - from https://resend.com/api-keys
  DIGEST_TO_ADDRESS   - MUST be the exact email address you signed up to
                        Resend with (their free/no-domain tier only allows
                        sending to your own account email)
  RUN_SECRET          - any random string you invent; required as ?key= to
                        trigger /run, so random internet bots can't spam it
"""

import os
import re
import sys
import datetime
import threading
import concurrent.futures as cf

import feedparser
import requests
from flask import Flask, request, jsonify, render_template_string

# Force real-time logging. Without this, Python buffers print() output when
# not attached to a terminal (as under gunicorn), so log lines can sit
# invisible for minutes even though the code has already run/failed —
# making a fast failure look like an indefinite hang.
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# 1. CONFIG — topic -> list of RSS feed URLs (all free, no API key required)
# ---------------------------------------------------------------------------

def google_news_rss(query, region="IN", lang="en"):
    q = query.replace(" ", "+")
    return f"https://news.google.com/rss/search?q={q}&hl={lang}-{region}&gl={region}&ceid={region}:{lang}"

FEEDS = {
    "AI News": [
        "https://hnrss.org/newest?q=artificial+intelligence",
        google_news_rss("artificial intelligence"),
    ],
    "New AI Tools & Launches": [
        "https://www.producthunt.com/feed?category=artificial-intelligence",
        google_news_rss("new AI tool launch"),
    ],
    "Software Engineering & Tech": [
        "https://hnrss.org/newest?q=software+engineering",
        "https://techcrunch.com/feed/",
    ],
    "System Design": [
        "https://hnrss.org/newest?q=system+design",
        google_news_rss("system design architecture"),
    ],
    "Backend / Java / Spring Boot": [
        "https://hnrss.org/newest?q=spring+boot",
        google_news_rss("Java Spring Boot"),
    ],
    "DevOps": [
        "https://devops.com/feed/",
        "https://hnrss.org/newest?q=devops",
    ],
    "Startups": [
        "https://hnrss.org/newest?q=startup+funding",
        google_news_rss("startup funding"),
    ],
    "India Tech & Innovation": [
        "https://yourstory.com/feed",
        google_news_rss("India tech events innovation"),
    ],
    "Digital Public Infrastructure & E-Governance": [
        google_news_rss("Digital Public Infrastructure India"),
        google_news_rss("e-governance India"),
    ],
    "Emerging Tech & Sovereign AI": [
        google_news_rss("sovereign AI"),
        google_news_rss("emerging technology India"),
    ],
    "Hardware, Manufacturing & Deep-Tech": [
        google_news_rss("semiconductor manufacturing India"),
        google_news_rss("deep tech hardware India"),
    ],
    "Startup Ecosystem, Innovation & R&D": [
        google_news_rss("startup ecosystem R&D India"),
        google_news_rss("innovation research India"),
    ],
    "Tech Regulation, Policy & Cyber Governance": [
        google_news_rss("tech regulation policy India"),
        google_news_rss("cyber governance India"),
    ],
}

MAX_ITEMS_PER_TOPIC = 4
LOOKBACK_HOURS = 30
PER_FEED_TIMEOUT = (5, 8)     # (connect timeout, read timeout) in seconds — bounds a single slow feed
GLOBAL_FETCH_BUDGET_SECONDS = 25  # hard ceiling on the whole fetching phase, no matter how many feeds hang

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")   # kept only for reference; not used as sender anymore
TO_ADDRESS = os.environ.get("DIGEST_TO_ADDRESS")
RUN_SECRET = os.environ.get("RUN_SECRET")

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
RESEND_URL = "https://api.resend.com/emails"
# Resend's shared sandbox sender — works with no domain setup, but only
# delivers to the email address you signed up to Resend with.
RESEND_FROM_ADDRESS = "Daily Briefing Agent <onboarding@resend.dev>"

SUBJECT_PREFIX = "[Daily Research Digest]"

# ---------------------------------------------------------------------------
# 2. FETCH
# ---------------------------------------------------------------------------

def _fetch_one_feed(topic, url):
    """Fetch and parse a single feed. Any failure (timeout, bad response,
    redirect loop, DNS issue) is caught here and just means fewer items —
    it never blocks or crashes the whole run."""
    try:
        resp = requests.get(
            url,
            timeout=PER_FEED_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
            allow_redirects=True,
        )
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        return topic, parsed.entries
    except Exception as e:
        print(f"  [warn] failed to fetch {url}: {e}")
        return topic, []


def _entries_to_items(entries):
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=LOOKBACK_HOURS)
    items = []
    for entry in entries:
        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()
        if not title or not link:
            continue
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        if published:
            pub_dt = datetime.datetime(*published[:6])
            if pub_dt < cutoff:
                continue
        items.append({"title": title, "link": link})
    return items


def gather_all():
    """Fetch every feed across every topic IN PARALLEL, bounded by a hard
    global time budget. Previously this fetched one feed at a time in a
    simple loop — a single slow or redirect-looping feed (Google News feeds
    are a known culprit) could stall the entire run for minutes. Now, even
    in the worst case, the whole fetch phase can't exceed
    GLOBAL_FETCH_BUDGET_SECONDS: slow feeds are simply skipped rather than
    blocking everything else."""
    digest_source = {topic: [] for topic in FEEDS}
    tasks = [(topic, url) for topic, urls in FEEDS.items() for url in urls]

    print(f"Fetching {len(tasks)} feeds in parallel (budget: {GLOBAL_FETCH_BUDGET_SECONDS}s)...")
    with cf.ThreadPoolExecutor(max_workers=6) as executor:
        future_to_task = {executor.submit(_fetch_one_feed, topic, url): (topic, url) for topic, url in tasks}
        done, not_done = cf.wait(future_to_task.keys(), timeout=GLOBAL_FETCH_BUDGET_SECONDS)

        for future in done:
            topic, url = future_to_task[future]
            try:
                _, entries = future.result()
            except Exception as e:
                print(f"  [warn] {url} raised: {e}")
                entries = []
            digest_source[topic].extend(_entries_to_items(entries))

        for future in not_done:
            topic, url = future_to_task[future]
            print(f"  [warn] {url} did not finish within the global budget, skipping")
            # Not cancelling: the underlying request has its own timeout and
            # will die on its own; we just stop waiting for it here.

    # De-dupe by title and cap per topic
    for topic, items in digest_source.items():
        seen = set()
        deduped = []
        for it in items:
            key = it["title"].lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(it)
        digest_source[topic] = deduped[:MAX_ITEMS_PER_TOPIC]

    return digest_source

# ---------------------------------------------------------------------------
# 3. SUMMARIZE with Groq (free tier: https://console.groq.com)
# ---------------------------------------------------------------------------

def build_prompt(digest_source):
    lines = []
    for topic, items in digest_source.items():
        if not items:
            continue
        lines.append(f"### {topic}")
        for it in items:
            lines.append(f"- {it['title']} ({it['link']})")
    raw_text = "\n".join(lines)

    return f"""You are writing a concise daily research digest for a software engineer
in India who follows AI, software engineering, system design, backend/Java/Spring Boot,
DevOps, startups, new AI tool launches, and India tech events.

Below are raw headlines grouped by topic, gathered from RSS feeds in the last day.
Some may be noise or duplicates — use judgment and skip anything irrelevant or low quality.

For each topic that has real content, write:
- A short "## Topic Name" heading
- 3-6 bullet points, each ONE crisp sentence summarizing a distinct story/development
  (not just restating the headline — add the "so what")
- Keep the source link at the end of each bullet in markdown format: [source](link)

Skip topics with no meaningful content. Keep the whole digest scannable in under 3 minutes.
Start directly with the first "## " heading, no preamble.

RAW HEADLINES:
{raw_text}
"""


def summarize_with_groq(digest_source):
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY environment variable not set.")

    prompt = build_prompt(digest_source)
    resp = requests.post(
        GROQ_URL,
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 6000,
        },
        timeout=45,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]

# ---------------------------------------------------------------------------
# 4. EMAIL via Gmail SMTP (free, App Password)
# ---------------------------------------------------------------------------

def markdown_to_basic_html(md_text):
    html_lines = ["<html><body style='font-family:Arial,sans-serif;line-height:1.5'>"]
    for line in md_text.splitlines():
        line = line.rstrip()
        if line.startswith("## "):
            html_lines.append(f"<h3>{line[3:]}</h3>")
        elif line.startswith("- "):
            content = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', line[2:])
            html_lines.append(f"<p style='margin:4px 0'>&bull; {content}</p>")
        elif line.strip() == "":
            continue
        else:
            html_lines.append(f"<p>{line}</p>")
    html_lines.append("</body></html>")
    return "\n".join(html_lines)


def send_email(markdown_body):
    """Send via Resend's HTTP email API (not SMTP) — Render's free tier blocks
    outbound SMTP ports (25/465/587), so a plain HTTPS API call is required."""
    if not RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY environment variable not set.")
    if not TO_ADDRESS:
        raise RuntimeError("DIGEST_TO_ADDRESS environment variable not set.")

    today = datetime.date.today().strftime("%d %b %Y")
    subject = f"{SUBJECT_PREFIX} {today}"
    html_body = markdown_to_basic_html(markdown_body)

    resp = requests.post(
        RESEND_URL,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": RESEND_FROM_ADDRESS,
            "to": [TO_ADDRESS],
            "subject": subject,
            "text": markdown_body,
            "html": html_body,
        },
        timeout=30,
    )
    resp.raise_for_status()
    print(f"Email sent to {TO_ADDRESS} with subject: {subject}")

# ---------------------------------------------------------------------------
# 5. PIPELINE
# ---------------------------------------------------------------------------

def run_pipeline():
    t0 = datetime.datetime.now()
    print(f"[{t0.strftime('%H:%M:%S')}] Pipeline started.")
    digest_source = gather_all()
    t1 = datetime.datetime.now()
    total_items = sum(len(v) for v in digest_source.values())
    print(f"[{t1.strftime('%H:%M:%S')}] Collected {total_items} raw items across {len(digest_source)} topics. (+{(t1-t0).total_seconds():.1f}s)")
    digest_markdown = summarize_with_groq(digest_source)
    t2 = datetime.datetime.now()
    print(f"[{t2.strftime('%H:%M:%S')}] Groq summary done. (+{(t2-t1).total_seconds():.1f}s)")
    send_email(digest_markdown)
    t3 = datetime.datetime.now()
    print(f"[{t3.strftime('%H:%M:%S')}] Email sent. (+{(t3-t2).total_seconds():.1f}s, total {(t3-t0).total_seconds():.1f}s)")
    return {"status": "ok", "items_collected": total_items}

# ---------------------------------------------------------------------------
# 6. FLASK ROUTES
# ---------------------------------------------------------------------------

HOME_PAGE_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Daily Briefing Agent</title>
  <style>
    body {
      font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      background: #111; color: #eee; margin: 0;
      display: flex; flex-direction: column; align-items: center;
      justify-content: center; min-height: 100vh; padding: 24px; box-sizing: border-box;
    }
    h1 { font-size: 1.3rem; margin-bottom: 4px; text-align: center; }
    p.sub { color: #999; margin-top: 0; margin-bottom: 28px; text-align: center; font-size: 0.9rem; }
    button {
      background: #2563eb; color: white; border: none; border-radius: 12px;
      padding: 18px 32px; font-size: 1.1rem; font-weight: 600;
      width: 100%; max-width: 320px; cursor: pointer;
    }
    button:disabled { background: #444; }
    #status {
      margin-top: 24px; font-size: 0.95rem; text-align: center;
      max-width: 320px; white-space: pre-wrap;
    }
    .ok { color: #4ade80; }
    .err { color: #f87171; }
  </style>
</head>
<body>
  <h1>Daily Briefing Agent</h1>
  <p class="sub">Tap below to fetch, summarize, and email today's digest</p>
  <button id="runBtn" onclick="runNow()">Run Digest Now</button>
  <div id="status"></div>

  <script>
    async function runNow() {
      const btn = document.getElementById('runBtn');
      const status = document.getElementById('status');
      btn.disabled = true;
      btn.innerText = 'Running... (10-20s)';
      status.className = '';
      status.innerText = '';
      try {
        const res = await fetch('/run?key={{run_secret}}');
        const data = await res.json();
        if (res.ok) {
          status.className = 'ok';
          status.innerText = (data.message || 'Started!') + ' Check your email shortly.';
        } else {
          status.className = 'err';
          status.innerText = 'Error: ' + (data.error || 'unknown error');
        }
      } catch (e) {
        status.className = 'err';
        status.innerText = 'Network error: ' + e.message;
      } finally {
        btn.disabled = false;
        btn.innerText = 'Run Digest Now';
      }
    }
  </script>
</body>
</html>
"""


@app.route("/")
def health():
    # Browsers get the button page; anything asking for JSON (like an uptime
    # monitor) gets a plain health check instead.
    if "text/html" in request.headers.get("Accept", ""):
        return render_template_string(HOME_PAGE_HTML, run_secret=RUN_SECRET or "")
    return jsonify({"status": "alive", "service": "daily-briefing-agent"})


@app.route("/run")
def run_endpoint():
    key = request.args.get("key")
    if not RUN_SECRET or key != RUN_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    def background_job():
        try:
            run_pipeline()
        except Exception as e:
            import traceback
            print(f"[error] pipeline failed: {e}")
            traceback.print_exc()

    threading.Thread(target=background_job, daemon=True).start()
    return jsonify({"status": "started", "message": "Pipeline running in background. Check your email in ~20-30 seconds."})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
