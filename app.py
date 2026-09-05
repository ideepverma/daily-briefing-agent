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
  - Gmail SMTP             -> free (App Password)
  - Render Web Service     -> free tier (750 instance-hrs/month)
  - cron-job.org           -> free external scheduler that "wakes" this
                              service once a day (Render's own Cron Job
                              product is NOT free, so we avoid it)

Endpoints:
  GET /                -> health check, confirms the service is alive
  GET /run?key=SECRET  -> runs the full pipeline once (fetch -> summarize -> email)

Environment variables required (set these in Render's dashboard, never in code):
  GROQ_API_KEY        - from https://console.groq.com
  GMAIL_ADDRESS       - your Gmail address
  GMAIL_APP_PASSWORD  - 16-char App Password (not your real password)
  DIGEST_TO_ADDRESS   - where to send the digest (can equal GMAIL_ADDRESS)
  RUN_SECRET          - any random string you invent; required as ?key= to
                        trigger /run, so random internet bots can't spam it
"""

import os
import re
import ssl
import smtplib
import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import feedparser
import requests
from flask import Flask, request, jsonify, render_template_string

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

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
TO_ADDRESS = os.environ.get("DIGEST_TO_ADDRESS", GMAIL_ADDRESS)
RUN_SECRET = os.environ.get("RUN_SECRET")

SUBJECT_PREFIX = "[Daily Research Digest]"

# ---------------------------------------------------------------------------
# 2. FETCH
# ---------------------------------------------------------------------------

FEED_FETCH_TIMEOUT = 8  # seconds, per feed

def fetch_topic_items(feed_urls):
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=LOOKBACK_HOURS)
    items = []
    for url in feed_urls:
        try:
            # feedparser.parse(url) has no timeout of its own and can hang
            # forever on a slow/dead server. Fetch the raw bytes ourselves
            # with an explicit timeout, then hand them to feedparser.
            resp = requests.get(
                url,
                timeout=FEED_FETCH_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0 (daily-briefing-agent)"},
            )
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception as e:
            print(f"  [warn] failed to parse {url}: {e}")
            continue
        for entry in parsed.entries:
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
    seen = set()
    deduped = []
    for it in items:
        key = it["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)
    return deduped[:MAX_ITEMS_PER_TOPIC]


def gather_all():
    digest_source = {}
    for topic, feeds in FEEDS.items():
        print(f"Fetching: {topic}")
        digest_source[topic] = fetch_topic_items(feeds)
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
        timeout=60,
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
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        raise RuntimeError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD env vars not set.")

    today = datetime.date.today().strftime("%d %b %Y")
    subject = f"{SUBJECT_PREFIX} {today}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = TO_ADDRESS
    msg.attach(MIMEText(markdown_body, "plain"))
    msg.attach(MIMEText(markdown_to_basic_html(markdown_body), "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls(context=context)
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, TO_ADDRESS, msg.as_string())

    print(f"Email sent to {TO_ADDRESS} with subject: {subject}")

# ---------------------------------------------------------------------------
# 5. PIPELINE
# ---------------------------------------------------------------------------

def run_pipeline():
    digest_source = gather_all()
    total_items = sum(len(v) for v in digest_source.values())
    print(f"Collected {total_items} raw items across {len(digest_source)} topics.")
    digest_markdown = summarize_with_groq(digest_source)
    send_email(digest_markdown)
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
          status.innerText = 'Done! ' + (data.items_collected || 0) + ' items collected. Check your email.';
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
    try:
        result = run_pipeline()
        return jsonify(result)
    except Exception as e:
        print(f"[error] pipeline failed: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
