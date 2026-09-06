# Daily Research Digest

A small Flask app that pulls tech/AI news from RSS feeds, summarizes it with
an LLM, and emails me a digest every morning. Runs on Render's free tier and
gets triggered daily by an external cron service. No paid tools anywhere in
the stack.

## What it does

1. Checks about 13 topic buckets — AI news, new AI tool launches, system
   design, Java/Spring Boot, DevOps, startups, and a handful of India-focused
   ones (DPI/e-governance, sovereign AI, hardware/manufacturing, policy) —
   each backed by a couple of RSS feeds (Hacker News, Google News search
   feeds, TechCrunch, YourStory, DevOps.com).
2. Pulls anything published in the last ~30 hours, dedupes by title, caps it
   at 4 items per topic so the prompt doesn't get out of hand.
3. Hands the raw headlines to an LLM and asks it to turn them into a short,
   readable digest — a few bullets per topic, not just a wall of links.
4. Emails the result to myself. A Gmail filter auto-labels it under
   "Research" based on the subject line.

## Stack, and why each piece is what it is

- **Feeds** — plain RSS, no API key, no rate limit to worry about.
- **Summarization** — Groq, currently on `openai/gpt-oss-120b`. Free tier,
  no card, limits way beyond what one call a day needs.
- **Email** — Resend's HTTP API, not SMTP. Explained below, this isn't
  optional.
- **Hosting** — Render, free Web Service tier.
- **Scheduling** — cron-job.org pings the app's `/run` endpoint once a day.
  Render's own cron job product looks like the obvious choice but it
  actually isn't free (starts around $1/mo), so a free web service plus a
  free external pinger does the same job for nothing.

## Setting it up from scratch

Roughly 30-40 minutes end to end if nothing goes sideways. Longer if it
does, but the "what actually went wrong" section below covers everything I
personally hit.

### 1. Get a free Groq API key

Sign up at console.groq.com, no card needed. Create an API key from the
dashboard and save it somewhere — it's only shown once.

### 2. Get a free Resend API key

Sign up at resend.com, **using the exact email address you want the digest
sent to**. This matters: without verifying a custom domain (which needs DNS
access), Resend's free tier only lets you send to the address you signed up
with. For a personal single-recipient tool like this, that's not a real
limitation, just something to get right on the first try. Grab an API key
from the dashboard.

### 3. Set up the Gmail label + filter

In Gmail: Settings → Filters and Blocked Addresses → Create a new filter.
Subject: `[Daily Research Digest]`. Apply label → New label → `Research`.
This runs against every future email automatically; it won't retroactively
label anything already in your inbox.

### 4. Put the code in a GitHub repo

Two files: `app.py` and `requirements.txt`. Private repo is fine, doesn't
need to be public for Render to use it.

### 5. Deploy on Render

New → Web Service → connect the repo → **Free** instance type. Build
command `pip install -r requirements.txt`. Start command:

```
gunicorn app:app --timeout 120
```

That `--timeout` flag isn't cosmetic — see the notes below for why.

Environment variables to set in Render's dashboard:

| Variable | What it's for |
|---|---|
| `GROQ_API_KEY` | summarization |
| `RESEND_API_KEY` | sending the email |
| `DIGEST_TO_ADDRESS` | must match the email you signed up to Resend with |
| `RUN_SECRET` | any random string you make up — required as `?key=` on `/run` so random bots can't trigger your pipeline |

Deploy, wait a minute or two, and Render gives you a live URL.

### 6. Test it manually

Open `https://your-app.onrender.com/` in a browser — you get a "Run Digest
Now" button (nicer than typing the secret key into a URL every time,
especially from a phone). Tap it, wait ~20-40 seconds, check your inbox.

### 7. Schedule it

Sign up free at cron-job.org. Create a job pointing at:

```
https://your-app.onrender.com/run?key=your_secret
```

Set it to run daily at whatever time you want, and **explicitly set the
job's timezone** — it defaults to UTC, and a 6:00 AM schedule left on UTC
quietly becomes 11:30 AM in India. Also worth turning off "save responses
in job history" in the job's settings — cron-job.org's free plan has a
surprisingly small cap on stored response size, and even a short JSON reply
can trip "output too large," even though the request itself succeeded.

That's the whole setup. From here it runs on its own every morning.

## What actually went wrong while building this (and why the code looks the way it does)

None of this was smooth on the first try, so keeping it here in case
something similar shows up again.

**Gmail SMTP doesn't work on Render's free tier.** First version used
Gmail + an app password over SMTP. Worked fine locally, immediately broke
on Render with `OSError: [Errno 101] Network is unreachable`. Render's free
tier blocks all outbound SMTP ports (25/465/587) as an anti-abuse measure —
it's a platform-level block, not fixable from the code side. Switched to
Resend's HTTP API, which uses plain HTTPS and isn't affected.

**Sequential feed fetching stalls badly.** Original code fetched all ~26
feed URLs one at a time in a loop. A single slow or redirect-looping feed
(Google News feeds especially) could stall the entire run for minutes,
since everything behind it in the queue just waited. Rewrote it to fetch
everything in parallel with a thread pool, bounded by a hard ~25-second
global budget — if some feeds are still slow after that, they just get
skipped rather than blocking the rest.

**`/run` needs to respond immediately, not after the whole pipeline
finishes.** Originally `/run` ran fetch → summarize → email synchronously
and returned the result. On Render's constrained free CPU this sometimes
took long enough to hit request timeouts. Now `/run` kicks off the real
work in a background thread and responds right away with a small "started"
message; the email still shows up a bit later once the thread finishes.

**gunicorn's own timeout can kill a working process.** Even with the
background thread, gunicorn's default 30-second worker timeout could still
kill the whole worker mid-run if Render's CPU throttling delayed the main
thread's ability to check in with the arbiter. Fixed by starting gunicorn
with `--timeout 120`.

**Logs can appear minutes late or not at all.** Python buffers `print()`
output when it's not attached to a real terminal — which is exactly gunicorn's
situation. This made genuine hangs and instant failures look identical in
Render's log viewer for a while. Fixed with
`sys.stdout.reconfigure(line_buffering=True)` near the top of the file,
which forces logs to appear in real time and made every other issue above
much faster to actually pin down.

**Groq models get retired.** Started on `llama-3.3-70b-versatile`, which
Groq pulled not long after. If summarization starts throwing a 404 with a
"model does not exist" message, that's the cause — check Groq's current
model list and swap `GROQ_MODEL` accordingly.

## Known limitations, left as-is on purpose

- Render's free instance sleeps after 15 minutes idle, so the first request
  each day is a bit slower while it spins back up. Expected, not a bug.
- Resend without a verified domain only sends to the signup address —
  fine here since it's a single-recipient personal tool.
- A handful of RSS feeds (Product Hunt's in particular) occasionally change
  format or go down entirely; the code logs a warning and moves on instead
  of failing the whole run over one bad feed.
