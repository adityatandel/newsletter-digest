# Newsletter Digest

Turn a noisy newsletter inbox into one short email. This tool reads the unread
newsletters in a dedicated Gmail inbox, groups them by topic, summarizes each one
with AI, and emails you a clean digest on whatever schedule you choose.

It runs entirely on your own computer. Your email and keys never leave your machine
except to talk to Gmail and Groq directly.

> **Status:** Phase 1 — email delivery, on **Mac and Windows**. A one-time setup
> wizard walks you through everything.

---

## What you get

- **A grouped summary by topic** (AI, Finance, Technology, and so on), each with a
  count and a short write-up per newsletter.
- **An individual card per newsletter**: the publisher, the original subject, an
  estimated reading time, a short overview, and a few key points.
- **A link back to each original newsletter** where one can be found.
- **Runs automatically** at the times you pick, using your computer's built-in
  scheduler (launchd on Mac, Task Scheduler on Windows).

*(Add a screenshot of your own digest here — it's the single most persuasive thing
in this README.)*

---

## Before you install — please read

This is a personal-computer tool. It will **not** work for everyone:

- **Personal computer only.** Work or school computers often block scheduled tasks,
  software installs, or the permissions this needs.
- **A personal Gmail account.** Some company/Workspace Google accounts have app
  passwords or IMAP turned off, and a few (Advanced Protection) can't use them at
  all. A **dedicated newsletter inbox** is strongly recommended — the tool marks
  mail as read.
- **Gmail app passwords require 2-Step Verification** to be switched on first.
- **You need an administrator account** (to install software).
- **The computer must be awake** at the scheduled time. On Mac, a run missed while
  the Mac was *asleep* is caught up when it wakes; a run missed while it was fully
  *off* is skipped.
- **You pay for your own Groq usage.** It's inexpensive for this (often only cents
  per run), but prices can change — see [Groq pricing](https://groq.com/pricing/).
- **Newsletter text is sent to Groq** to be summarized. Don't use this for private
  or sensitive mail.

### Supported systems

| System | Supported |
|---|---|
| macOS | 12 (Monterey) or newer — current is macOS 26/27 |
| Windows | 10 or 11 |
| Python | 3.9 or newer (install the latest from [python.org](https://www.python.org/downloads/)) |
| Linux | The digest runs, but the wizard won't install a schedule yet |

---

## Install

1. **Install Python 3** if you don't have it, from
   [python.org/downloads](https://www.python.org/downloads/). On Windows, tick
   **"Add Python to PATH"** in the installer.
2. **Download this project** (green "Code" button → "Download ZIP") and unzip it.
3. **Open a terminal in the unzipped folder:**
   - **Mac:** open Terminal, type `cd ` (with a space), drag the folder onto the
     window, press Enter.
   - **Windows:** open the folder, click the address bar, type `cmd`, press Enter.
4. **Run the wizard:**
   ```
   python3 setup.py
   ```
   (On Windows, use `python setup.py` if `python3` isn't found.)

The wizard will walk you through the rest: it checks your system, helps you create
a Gmail app password and a Groq key (testing each one on the spot), asks how often
you want the digest, saves your settings, schedules it, and offers a test run.

### First-launch security prompts

- **Mac:** the first time you run a downloaded file, macOS may warn you. If so,
  right-click `setup.py` → **Open**, or allow it under **System Settings → Privacy
  & Security**.
- **Windows:** if SmartScreen appears, choose **More info → Run anyway**.

---

## What the wizard asks you for

- Your **Gmail address** and a **16-character app password**
  (create at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords);
  needs [2-Step Verification](https://myaccount.google.com/signinoptions/twosv)).
- A **Groq API key**, starting with `gsk_`
  (create at [console.groq.com/keys](https://console.groq.com/keys)).
- **How often** to run (daily, weekdays, Mon/Wed/Fri, or weekly) and at what time.

Nothing is saved until each key has been tested successfully.

---

## Cost and privacy

- **Cost:** you pay Groq for your own usage. For a handful of newsletters a few
  times a week, this is typically cents per run, but check the current
  [Groq pricing](https://groq.com/pricing/) yourself — don't rely on a number in a
  README that may be out of date.
- **Privacy:** the digest sends the text of your newsletters to Groq to summarize
  them, and logs in to Gmail to read and send. Nothing else is sent anywhere, and
  your keys stay in a local `.env` file readable only by you. Use a dedicated
  newsletter inbox, not your main personal email.

This project is not affiliated with Google, Groq, or Substack.

---

## How it works

- `digest.py` — the digest itself: reads Gmail over IMAP, summarizes with Groq,
  emails the result over SMTP. It fetches everything unread since its last
  successful run, so any schedule works.
- `setup.py` — the one-time setup wizard.
- `setup_lib.py`, `scheduler.py` — the building blocks the wizard uses (checks,
  saving settings, and the Mac/Windows scheduling).

Your settings live in `.env`. The last successful run is tracked in `last_run.json`.
Output from each run is written to `digest.log`.

---

## Changing settings later

- **Change a key, the recipient, or the model:** edit `.env` in your install folder.
  If Groq ever retires the model, just change `DIGEST_MODEL` — no code edit needed.
- **Change the schedule:** run `python3 setup.py` again.
- **Uninstall the schedule:** `python3 setup.py --uninstall`. (Your folder and
  `.env` are left in place; delete them by hand if you want them gone — the `.env`
  holds your keys.)

---

## Troubleshooting

- **The digest didn't arrive.** Open `digest.log` in your install folder. A run that
  started will show a line beginning `=== Digest run started`. If there's no recent
  line at all, the schedule didn't fire — on Mac, check the computer was awake; on
  Windows, open Task Scheduler and look for **NewsletterDigest**.
- **"model ... does not exist".** Groq retired the model. Update `DIGEST_MODEL` in
  `.env` to a current one from [console.groq.com](https://console.groq.com).
- **Gmail login fails.** Re-create the app password and make sure you copied all 16
  characters. The normal Gmail password will not work.
- **An empty digest arrived.** There were no unread newsletters in the window. That's
  normal, not an error.

---

## License

MIT — see [LICENSE](LICENSE). Set your name in that file before publishing.
