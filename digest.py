import imaplib
import email
import email.header
import email.utils
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import smtplib
import os
import re
import json
import time
from html import escape as esc
from dotenv import load_dotenv
from datetime import datetime, timedelta
from groq import Groq
from bs4 import BeautifulSoup

# Load .env from the folder this script lives in, so the digest works no matter
# which directory it is launched from (cron, launchd and Task Scheduler all differ).
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
# override=True so the project's own .env wins over any leftover shell variables
# (e.g. a stale GROQ_API_KEY exported in the user's profile).
load_dotenv(os.path.join(PROJECT_DIR, ".env"), override=True)

IMAP_EMAIL = os.getenv("IMAP_EMAIL")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD")
# SMTP defaults to the same Gmail account used for reading, so the wizard only
# has to ask for one email and one app password.
SMTP_EMAIL = os.getenv("SMTP_EMAIL") or IMAP_EMAIL
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD") or IMAP_PASSWORD
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL") or IMAP_EMAIL
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

client = Groq(api_key=GROQ_API_KEY)

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
# Model can be overridden in .env so a future deprecation is a one-line fix with
# no code change. Falls back to the current recommended Groq model.
MODEL = os.getenv("DIGEST_MODEL") or "openai/gpt-oss-120b"
WORDS_PER_MINUTE = 230          # used for the reading-time estimates
MAX_INPUT_CHARS = 6000          # how much of each newsletter is sent to the model
RETRY_WAIT_SECONDS = 10         # pause before retrying a failed Groq call

# How far back to look for newsletters.
# The digest records when it last ran successfully (in last_run.json) and, on the
# next run, fetches everything since then. This makes the digest work with ANY
# schedule the user picks, instead of assuming specific weekdays.
FIRST_RUN_LOOKBACK_DAYS = 2    # used only the very first time, when there is no record
MAX_LOOKBACK_DAYS = 14         # safety cap so a long gap can't summarize hundreds of emails
STATE_FILE = os.path.join(PROJECT_DIR, "last_run.json")

# Fixed topic list. Every newsletter is forced into exactly one of these.
TOPICS = [
    "AI",
    "Business",
    "Finance",
    "Technology",
    "Coding",
    "Science",
    "Health",
    "Politics & World",
    "Culture & Media",
    "Career & Productivity",
    "Other",
]

TOPIC_COLORS = {
    "AI": "#7c3aed",
    "Business": "#0891b2",
    "Finance": "#059669",
    "Technology": "#2563eb",
    "Coding": "#db2777",
    "Science": "#ea580c",
    "Health": "#dc2626",
    "Politics & World": "#475569",
    "Culture & Media": "#c026d3",
    "Career & Productivity": "#d97706",
    "Other": "#6b7280",
}

# Common variations the model might return, mapped back to the fixed list.
TOPIC_ALIASES = {
    "tech": "Technology",
    "artificial intelligence": "AI",
    "machine learning": "AI",
    "software": "Coding",
    "programming": "Coding",
    "software development": "Coding",
    "investing": "Finance",
    "markets": "Finance",
    "politics": "Politics & World",
    "world": "Politics & World",
    "world news": "Politics & World",
    "culture": "Culture & Media",
    "media": "Culture & Media",
    "career": "Career & Productivity",
    "productivity": "Career & Productivity",
}


# ---------------------------------------------------------------------------
# Email parsing
# ---------------------------------------------------------------------------
def decode_part(part):
    """Decode one MIME part to a string, or "" if it can't be decoded."""
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="ignore")
        except LookupError:
            return payload.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def html_to_text(html_source):
    soup = BeautifulSoup(html_source, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)


def get_email_parts(msg):
    """Return (readable_text, html_source). HTML is preferred over plain text."""
    html_source = ""
    plain = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition"))
            if "attachment" in disposition:
                continue
            if content_type == "text/html" and not html_source:
                html_source = decode_part(part)
            elif content_type == "text/plain" and not plain:
                plain = decode_part(part)
    else:
        raw = decode_part(msg)
        if msg.get_content_type() == "text/plain":
            plain = raw
        else:
            html_source = raw

    text = html_to_text(html_source) if html_source else plain
    return text, html_source


# Link text that means "this is the web version of the newsletter".
LINK_TEXT_PATTERNS = [
    r"read\s+(this\s+)?(post\s+|email\s+|newsletter\s+)?(in|on)\s+(the\s+|your\s+)?(app|browser|web)",
    r"view\s+(this\s+)?(post\s+|email\s+|newsletter\s+)?(in|on)\s+(the\s+|your\s+)?(app|browser|web)",
    r"(view|read|open)\s+online",
    r"view\s+as\s+(a\s+)?web\s?page",
    r"open\s+in\s+(the\s+)?(browser|app)",
    r"(web|online|browser)\s+version",
]

# URL shapes that are reliably the newsletter post itself.
POST_URL_PATTERNS = [
    r"^https?://([^/]+\.)?substack\.com/(pub/[^/]+/)?p/",
    r"^https?://([^/]+\.)?substack\.com/app-link/post",
    r"^https?://[^/]*beehiiv\.com/p/",
]

# Links that must never be treated as "the newsletter".
REJECT_URL_PATTERN = re.compile(
    r"unsubscribe|subscribe|preferences|manage|opt-?out|/comments?(/|\?|$)|/share|[?&]action=|mail\.google\.com",
    re.I,
)


def _usable_url(url):
    if not url.lower().startswith(("http://", "https://")):
        return False
    return not REJECT_URL_PATTERN.search(url)


def find_newsletter_url(html_source):
    """Find the link to the newsletter itself (not the Gmail message).
    Returns None if no reliable link is found."""
    if not html_source:
        return None

    soup = BeautifulSoup(html_source, "html.parser")
    anchors = [
        (a.get_text(" ", strip=True).lower(), (a.get("href") or "").strip())
        for a in soup.find_all("a")
    ]

    # 1) Links labelled like "Read in the app" / "View in browser" / "Read online"
    for text, url in anchors:
        if not text or len(text) > 60 or not _usable_url(url):
            continue
        if any(re.search(p, text) for p in LINK_TEXT_PATTERNS):
            return url

    # 2) Links that are clearly the post URL on Substack / Beehiiv
    for text, url in anchors:
        if _usable_url(url) and any(re.search(p, url, re.I) for p in POST_URL_PATTERNS):
            return url

    return None


def get_publisher_name(sender_header):
    """'Morning Brew <crew@morningbrew.com>' -> 'Morning Brew'"""
    name, addr = email.utils.parseaddr(sender_header)
    name = name.strip().strip('"').strip()
    if name:
        return name
    if addr:
        return addr.split("@")[0]
    return sender_header or "Unknown Sender"


def reading_minutes(text):
    words = len(text.split())
    return max(1, round(words / WORDS_PER_MINUTE))


# ---------------------------------------------------------------------------
# AI summarizing
# ---------------------------------------------------------------------------
def strip_markdown(text):
    return text.replace("**", "").replace("__", "")


def call_groq(prompt, attempts=2):
    """Call Groq, retrying once after a short pause. Returns the text or raises."""
    last_error = None
    for attempt in range(attempts):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as e:
            last_error = e
            if attempt < attempts - 1:
                time.sleep(RETRY_WAIT_SECONDS)
    raise last_error


def parse_json_object(text):
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        return None
    try:
        data = json.loads(text[start:end])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def normalize_topic(raw):
    if isinstance(raw, str):
        cleaned = raw.strip().lower()
        for topic in TOPICS:
            if topic.lower() == cleaned:
                return topic
        if cleaned in TOPIC_ALIASES:
            return TOPIC_ALIASES[cleaned]
    return "Other"


def clean_points(raw):
    points = []
    if not isinstance(raw, list):
        return points
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = strip_markdown(str(item.get("label", ""))).strip().rstrip(":").strip()
        explanation = strip_markdown(str(item.get("explanation", ""))).strip()
        if label and explanation:
            points.append({"label": label, "explanation": explanation})
    return points[:6]


def analyze_newsletter(subject, publisher, body):
    """One Groq call: topic + prose overview + 3-6 key points."""
    prompt = (
        "You are summarizing a newsletter for a busy reader.\n\n"
        "Return ONLY a valid JSON object. No text before or after it and no markdown code fences. "
        "It must have exactly these fields:\n"
        "- \"topic\": exactly one of: " + ", ".join(TOPICS) + ". Pick the one that best fits the "
        "newsletter's main subject. Use Other only if nothing fits.\n"
        "- \"overview\": one paragraph of roughly 4 to 6 sentences giving a specific, informative "
        "overview of the newsletter.\n"
        "- \"key_points\": an array of 3 to 6 objects, each with \"label\" (a short label of 2 to 5 "
        "words) and \"explanation\" (one to two sentences).\n\n"
        "Rules: plain text only inside the values (no markdown, no asterisks, no bullet characters). "
        "Be specific: include names, numbers, and findings from the newsletter rather than generic "
        "statements.\n\n"
        "Format example: {\"topic\": \"Finance\", \"overview\": \"...\", "
        "\"key_points\": [{\"label\": \"Rate cut odds\", \"explanation\": \"...\"}]}\n\n"
        "Newsletter subject: " + subject + "\n"
        "Newsletter publisher: " + publisher + "\n\n"
        "Newsletter text:\n" + body[:MAX_INPUT_CHARS]
    )

    last_error = "the model returned an unexpected format"
    for _ in range(2):
        try:
            text = call_groq(prompt)
        except Exception as e:
            last_error = str(e)
            break

        data = parse_json_object(text)
        if data:
            overview = strip_markdown(str(data.get("overview", ""))).strip()
            points = clean_points(data.get("key_points"))
            if overview and points:
                return {
                    "ok": True,
                    "topic": normalize_topic(data.get("topic")),
                    "overview": overview,
                    "key_points": points,
                }

    return {
        "ok": False,
        "topic": "Other",
        "overview": f"Could not summarize: {last_error}",
        "key_points": [],
    }


def count_label(count):
    """'1 Newsletter' / '9 Newsletters'"""
    return f"{count} Newsletter" + ("" if count == 1 else "s")


def fallback_bullet(overview):
    """First two sentences of a newsletter's own overview (used if the model skips one)."""
    sentences = re.split(r"(?<=[.!?])\s+", overview.strip())
    return " ".join(sentences[:2]).strip()


def summarize_group(topic, items):
    """One short bullet per newsletter in this topic: [{"text": ..., "name": publisher}, ...].
    The newsletter name is added by the code (not the model), so it is always correct."""
    usable = [n for n in items if n["ok"]]
    if not usable:
        return []

    material = ""
    for i, n in enumerate(usable, 1):
        points = "; ".join(p["label"] + ": " + p["explanation"] for p in n["key_points"])
        material += (
            "[" + str(i) + "] " + n["publisher"] + " - " + n["subject"] + "\n"
            "Overview: " + n["overview"] + "\n"
            "Key points: " + points + "\n\n"
        )

    prompt = (
        "You are writing the \"" + topic + "\" section of a newsletter digest.\n"
        "For EACH numbered newsletter below, write a summary of 1 to 2 sentences covering its most "
        "important takeaway. Do not include the newsletter's name in the summary (it is added "
        "automatically).\n"
        "Return ONLY a valid JSON array with exactly one object per newsletter, in the same order, "
        "each shaped like {\"id\": 1, \"summary\": \"...\"}. No text before or after it and no "
        "markdown code fences. Plain text only inside the values (no markdown, no asterisks).\n\n"
        + material
    )

    summaries = {}
    try:
        text = call_groq(prompt)
        start = text.find("[")
        end = text.rfind("]") + 1
        if start != -1 and end:
            for item in json.loads(text[start:end]):
                if not isinstance(item, dict):
                    continue
                try:
                    idx = int(item.get("id"))
                except (TypeError, ValueError):
                    continue
                summary = strip_markdown(str(item.get("summary", ""))).strip()
                if summary:
                    summaries[idx] = summary
    except Exception as e:
        print(f"Could not write topic summary for {topic}: {e}")

    bullets = []
    for i, n in enumerate(usable, 1):
        bullets.append({
            "text": summaries.get(i) or fallback_bullet(n["overview"]),
            "name": n["publisher"],
        })
    return bullets


# ---------------------------------------------------------------------------
# Run state (so any schedule works: fetch everything since the last run)
# ---------------------------------------------------------------------------
def read_last_run():
    """Return the datetime of the last successful run, or None if unknown."""
    try:
        with open(STATE_FILE) as f:
            value = json.load(f).get("last_success")
        if value:
            return datetime.fromisoformat(value)
    except FileNotFoundError:
        pass
    except (ValueError, json.JSONDecodeError, OSError) as e:
        print(f"Could not read {STATE_FILE} ({e}); treating this as a first run.")
    return None


def write_last_run(when):
    """Record a successful run. Failing to write must not break the digest."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"last_success": when.isoformat()}, f)
    except OSError as e:
        print(f"WARNING: could not write {STATE_FILE}: {e}")


def compute_lookback_days(last_run, now):
    """Whole days to look back, based on when we last ran. IMAP SINCE is date-only,
    so we subtract an extra day to avoid missing mail from the last run's own date."""
    if last_run is None:
        return FIRST_RUN_LOOKBACK_DAYS
    gap_days = (now.date() - last_run.date()).days + 1   # +1 covers the date-only SINCE
    return max(1, min(gap_days, MAX_LOOKBACK_DAYS))


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def fetch_newsletters():
    print("Connecting to Gmail...")
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(IMAP_EMAIL, IMAP_PASSWORD)
    mail.select("inbox")

    # Fetch everything since the last successful run, so ANY schedule works.
    today = datetime.now()
    last_run = read_last_run()
    days_back = compute_lookback_days(last_run, today)
    if last_run is None:
        print(f"No previous run on record; looking back {days_back} day(s).")
    else:
        print(f"Last successful run: {last_run:%Y-%m-%d %H:%M}; looking back {days_back} day(s).")

    date_since = (today - timedelta(days=days_back)).strftime("%d-%b-%Y")
    status, messages = mail.uid("search", None, f'(UNSEEN SINCE "{date_since}")')

    email_ids = messages[0].split()
    print(f"Found {len(email_ids)} unread emails from the last {days_back} day(s).")

    newsletters = []
    handled_uids = []   # marked as read ONLY after the digest email has been sent

    for eid in email_ids:
        # BODY.PEEK[] reads the email WITHOUT marking it as read.
        status, msg_data = mail.uid("fetch", eid, "(BODY.PEEK[])")
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)

        sender = str(email.header.make_header(email.header.decode_header(msg.get("From", "Unknown Sender"))))
        subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject", "No Subject"))))
        message_id = msg.get("Message-ID", "").strip().strip("<>")
        gmail_link = f"https://mail.google.com/mail/u/0/#search/rfc822msgid:{message_id}" if message_id else ""

        if subject.startswith("Newsletter Digest -"):
            print(f"Skipping {subject} - this is a previous digest email.")
            continue

        body, html_source = get_email_parts(msg)

        if len(body) > 100000:
            print(f"Skipping {subject} - body too long.")
            handled_uids.append(eid)
            continue

        if not body.strip():
            print(f"Skipping {subject} - empty body.")
            handled_uids.append(eid)
            continue

        publisher = get_publisher_name(sender)
        newsletter_url = find_newsletter_url(html_source)

        print(f"Summarizing: {subject} from {publisher}")
        analysis = analyze_newsletter(subject, publisher, body)

        summary_text = analysis["overview"] + " " + " ".join(
            p["label"] + " " + p["explanation"] for p in analysis["key_points"]
        )

        handled_uids.append(eid)

        newsletters.append({
            "publisher": publisher,
            "subject": subject,
            "topic": analysis["topic"],
            "overview": analysis["overview"],
            "key_points": analysis["key_points"],
            "ok": analysis["ok"],
            "original_minutes": reading_minutes(body),
            "summary_minutes": reading_minutes(summary_text),
            "newsletter_url": newsletter_url,
            "gmail_link": gmail_link,
        })

    mail.logout()
    return newsletters, handled_uids


def mark_as_read(uids):
    """Mark newsletters as read. Called only after the digest email was sent."""
    if not uids:
        return
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(IMAP_EMAIL, IMAP_PASSWORD)
    mail.select("inbox")
    for uid in uids:
        mail.uid("store", uid, "+FLAGS", "\\Seen")
    mail.logout()
    print(f"Marked {len(uids)} newsletter(s) as read.")


def build_groups(newsletters):
    """Group newsletters by topic and write one combined summary per topic."""
    by_topic = {}
    for n in newsletters:
        by_topic.setdefault(n["topic"], []).append(n)

    groups = []
    for topic, items in by_topic.items():
        print(f"Summarizing topic: {topic} ({len(items)})")
        groups.append({
            "topic": topic,
            "count": len(items),
            "bullets": summarize_group(topic, items),
            "links": [
                {"name": n["publisher"], "url": n["newsletter_url"]}
                for n in items if n["newsletter_url"]
            ],
        })

    # Biggest topics first; ties follow the order of the fixed topic list.
    # "Other" is a catch-all, so it always goes last.
    groups.sort(key=lambda g: (g["topic"] == "Other", -g["count"], TOPICS.index(g["topic"])))
    return groups


def order_newsletters(newsletters, groups):
    """Sort individual summaries to follow the same topic order as the groups."""
    order = [g["topic"] for g in groups]
    return sorted(newsletters, key=lambda n: order.index(n["topic"]))


# ---------------------------------------------------------------------------
# Email (HTML) output
# ---------------------------------------------------------------------------
def text_to_html(text):
    return esc(text).replace("\n", "<br>")


def build_group_html(g):
    color = TOPIC_COLORS.get(g["topic"], "#6b7280")

    if g["bullets"]:
        items = "".join(
            f'<li style="margin-bottom: 8px;">{esc(b["text"])} '
            f'<span style="color: #888;">({esc(b["name"])})</span></li>'
            for b in g["bullets"]
        )
        body_html = f'<ul style="margin: 0; padding-left: 20px; line-height: 1.5;">{items}</ul>'
    else:
        body_html = (
            '<p style="margin: 0; color: #888;">'
            "Could not summarize the newsletters in this topic.</p>"
        )

    links_html = ""
    if g["links"]:
        links = " &nbsp;|&nbsp; ".join(
            f'<a href="{esc(l["url"])}" style="color: #4a90e2;">{esc(l["name"])}</a>'
            for l in g["links"]
        )
        links_html = (
            '<p style="margin: 14px 0 0 0; font-size: 0.85em; color: #666;">'
            f"<strong>Read:</strong> {links}</p>"
        )

    return f"""
    <div style="margin-bottom: 24px; padding: 20px; border-left: 4px solid {color}; background: #f9f9f9;">
        <h2 style="margin: 0 0 12px 0; color: {color};">
            {esc(g['topic'])}
            <span style="background: {color}; color: #ffffff; font-size: 0.55em; padding: 3px 10px; border-radius: 10px; vertical-align: middle; white-space: nowrap;">{count_label(g['count'])}</span>
        </h2>
        {body_html}
        {links_html}
    </div>"""


def build_subject_html(n):
    subject = esc(n["subject"])
    if n.get("gmail_link"):
        return f'<a href="{esc(n["gmail_link"])}" style="color: #333; text-decoration: none;">{subject}</a>'
    return subject


def build_newsletter_html(n):
    color = TOPIC_COLORS.get(n["topic"], "#6b7280")

    points_html = ""
    if n["key_points"]:
        bullets = "".join(
            f'<li style="margin-bottom: 8px;"><strong>{esc(p["label"])}:</strong> {esc(p["explanation"])}</li>'
            for p in n["key_points"]
        )
        points_html = f"""
        <p style="margin: 18px 0 6px 0; font-size: 0.8em; font-weight: bold; letter-spacing: 0.08em; color: #666;">KEY POINTS</p>
        <ul style="margin: 0; padding-left: 20px; line-height: 1.5;">{bullets}</ul>"""

    read_html = ""
    if n["newsletter_url"]:
        read_html = (
            f'<p style="margin: 16px 0 0 0;"><a href="{esc(n["newsletter_url"])}" '
            'style="color: #4a90e2;">Read original newsletter</a></p>'
        )

    return f"""
    <div style="margin-bottom: 30px; padding: 20px; border: 1px solid #e5e5e5; border-radius: 6px;">
        <p style="margin: 0 0 6px 0; font-weight: bold; color: {color};">📧 {esc(n['topic'])}</p>
        <p style="margin: 0; color: #666; font-size: 0.9em;">By <strong>{esc(n['publisher'])}</strong></p>
        <h3 style="margin: 6px 0; color: #333;">{build_subject_html(n)}</h3>
        <p style="margin: 0 0 14px 0; color: #888; font-size: 0.85em;">Original: ~{n['original_minutes']} min | Summary: ~{n['summary_minutes']} min</p>
        <p style="margin: 0; line-height: 1.5;">{text_to_html(n['overview'])}</p>
        {points_html}
        {read_html}
    </div>"""


def build_html(newsletters, groups):
    if not newsletters:
        return "<h2>No new newsletters.</h2>"

    section_label = 'style="margin: 24px 0 12px 0; font-size: 0.8em; font-weight: bold; letter-spacing: 0.1em; color: #888;"'

    html = """
    <html>
    <body style="font-family: Arial, sans-serif; max-width: 700px; margin: auto; padding: 20px;">
    <h1 style="color: #333;">Your Newsletter Digest</h1>
    <hr>
    """

    html += f"<p {section_label}>SUMMARY BY TOPIC</p>"
    for g in groups:
        html += build_group_html(g)

    html += f"<hr><p {section_label}>INDIVIDUAL NEWSLETTERS</p>"
    for n in newsletters:
        html += build_newsletter_html(n)

    html += "</body></html>"
    return html


def send_digest(html):
    print("Sending digest email...")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Newsletter Digest - {datetime.now().strftime('%B %d, %Y')}"
    msg["From"] = SMTP_EMAIL
    msg["To"] = RECIPIENT_EMAIL
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SMTP_EMAIL, SMTP_PASSWORD)
        server.sendmail(SMTP_EMAIL, RECIPIENT_EMAIL, msg.as_string())

    print(f"Digest sent to {RECIPIENT_EMAIL}")


# ---------------------------------------------------------------------------
# Telegram output
# ---------------------------------------------------------------------------
def tg(text):
    """Escape text for Telegram's HTML mode."""
    return esc(text, quote=False)


def build_telegram_messages(newsletters, groups):
    messages = [f"<b>Newsletter Digest - {datetime.now().strftime('%B %d, %Y')}</b>"]

    if not newsletters:
        messages.append("No new newsletters.")
        return messages

    messages.append("<b>SUMMARY BY TOPIC</b>")
    for g in groups:
        msg = f"<b>{tg(g['topic'])} ({count_label(g['count'])})</b>\n"
        if g["bullets"]:
            for b in g["bullets"]:
                msg += f"\n• {tg(b['text'])} ({tg(b['name'])})"
        else:
            msg += "\nCould not summarize the newsletters in this topic."
        if g["links"]:
            links = " | ".join(f'<a href="{esc(l["url"])}">{tg(l["name"])}</a>' for l in g["links"])
            msg += f"\n\nRead: {links}"
        messages.append(msg)

    messages.append("<b>INDIVIDUAL NEWSLETTERS</b>")
    for n in newsletters:
        msg = f"📧 <b>{tg(n['topic'])}</b>\n"
        msg += f"<i>By {tg(n['publisher'])}</i>\n"
        msg += f"<b>{tg(n['subject'])}</b>\n"
        msg += f"Original: ~{n['original_minutes']} min | Summary: ~{n['summary_minutes']} min\n\n"
        msg += tg(n["overview"])

        if n["key_points"]:
            msg += "\n\n<b>KEY POINTS</b>"
            for p in n["key_points"]:
                msg += f"\n• <b>{tg(p['label'])}:</b> {tg(p['explanation'])}"

        links = []
        if n["newsletter_url"]:
            links.append(f'<a href="{esc(n["newsletter_url"])}">Read original newsletter</a>')
        if n.get("gmail_link"):
            links.append(f'<a href="{esc(n["gmail_link"])}">Open original email</a>')
        if links:
            msg += "\n\n" + " | ".join(links)

        messages.append(msg)

    return messages


def split_for_telegram(text, limit=4000):
    """Split a long message on line breaks so HTML tags are never cut in half."""
    if len(text) <= limit:
        return [text]

    chunks = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # extremely long single line: hard cut
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit and current:
            chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)
    return chunks


def send_telegram(newsletters, groups):
    import requests
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    # Phase 1 is email-only. If no Telegram credentials are configured, quietly
    # skip this step instead of failing. Set both values in .env to enable it.
    if not bot_token or not chat_id:
        print("Telegram not configured; sending email only.")
        return

    base_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    for message in build_telegram_messages(newsletters, groups):
        for chunk in split_for_telegram(message):
            response = requests.post(base_url, json={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            if not response.ok:
                print(f"Telegram error {response.status_code}: {response.text[:200]}")
            time.sleep(0.5)

    print("Digest sent to Telegram.")


def run_digest():
    started = datetime.now()
    print(f"=== Digest run started {started:%Y-%m-%d %H:%M:%S} ===")
    newsletters, handled_uids = fetch_newsletters()
    groups = build_groups(newsletters)
    newsletters = order_newsletters(newsletters, groups)
    html = build_html(newsletters, groups)

    # If sending the email fails, this raises and NOTHING below runs: newsletters
    # stay unread AND the last-run record is not advanced, so the next run retries
    # the same window. Losing newsletters is worse than an occasional duplicate.
    send_digest(html)

    # Email is out, so this run counts as successful. Record it before the
    # non-critical steps below, so a later hiccup can't cost us the timestamp.
    write_last_run(started)

    try:
        mark_as_read(handled_uids)
    except Exception as e:
        # Worst case is a duplicate next time, which beats losing newsletters.
        print(f"WARNING: could not mark newsletters as read: {e}")

    try:
        send_telegram(newsletters, groups)
    except Exception as e:
        print(f"WARNING: Telegram failed: {e}")

    print(f"=== Done {datetime.now():%Y-%m-%d %H:%M:%S} ===")


if __name__ == "__main__":
    run_digest()
