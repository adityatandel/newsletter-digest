"""
setup_lib.py - shared, testable setup logic for the newsletter digest wizard.

This module contains NO user interaction. It is the set of plain functions the
terminal wizard (and, later, the browser wizard) sit on top of:

  - detect_platform()           which OS, and is scheduling supported
  - check_gmail(email, pw)      can we actually log in to read AND send?
  - check_groq(key, model)      is the key valid and the model available?
  - write_env_file(path, data)  save credentials safely (0600 on Mac/Linux)
  - create_venv(dir)            make an isolated Python environment
  - install_requirements(...)   install the digest's dependencies into it

Every check returns a CheckResult(ok, message, hint) so the wizard can show a
clear message and let the user fix it on the spot, instead of discovering a bad
app password or a dead model days later at 7am.
"""

import os
import ssl
import sys
import socket
import imaplib
import smtplib
import subprocess
from dataclasses import dataclass


GMAIL_IMAP_HOST = "imap.gmail.com"
GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 465
NETWORK_TIMEOUT = 20   # seconds

# Minimum Python the digest + wizard are known to work on (network timeout args,
# dataclasses, f-strings). Users should ideally install the latest 3.x.
MIN_PYTHON = (3, 9)

# Single source of truth for the limitations we surface to the user. The wizard
# prints these on its "before you begin" screen and the README lists the same set,
# so the two can never drift apart.
CONSTRAINTS = [
    "This is for a PERSONAL computer. Work or school computers often block "
    "scheduled tasks, software installs, or the permissions this needs.",
    "You need a PERSONAL Gmail account. Some company/Workspace accounts have "
    "app passwords or IMAP turned off, and a few (Advanced Protection) can't use "
    "them at all. A dedicated newsletter inbox is strongly recommended.",
    "Gmail app passwords require 2-Step Verification to be turned on first.",
    "You need permission to install software (an administrator account).",
    "The computer must be awake at the scheduled time. On Mac a run missed while "
    "asleep is caught up on wake; a run missed while fully powered off is skipped.",
    "You pay for your own Groq API usage. It's inexpensive for this, but check "
    "Groq's current pricing — costs can change.",
    "Newsletter text is sent to Groq to be summarized. Don't use this for private "
    "or sensitive email.",
    "Supported: macOS 12+ or Windows 10/11. Linux runs the digest but the wizard "
    "won't install a schedule there yet.",
]


def check_python_version(minimum=MIN_PYTHON):
    """Is the Python running this new enough? Returns a CheckResult."""
    current = sys.version_info[:2]
    if current >= minimum:
        return CheckResult(True, f"Python {current[0]}.{current[1]} is new enough.")
    return CheckResult(
        False,
        f"Python {current[0]}.{current[1]} is too old "
        f"(need {minimum[0]}.{minimum[1]} or newer).",
        "Install the latest Python 3 from python.org, then run setup again.",
    )


@dataclass
class CheckResult:
    ok: bool
    message: str          # short, shown to the user
    hint: str = ""        # optional next-step suggestion when ok is False

    def __bool__(self):
        return self.ok


# ---------------------------------------------------------------------------
# Platform
# ---------------------------------------------------------------------------
def detect_platform():
    """Return a dict describing the OS and whether we can schedule on it."""
    p = sys.platform
    if p == "darwin":
        return {"os": "mac", "scheduler": "launchd", "supported": True}
    if p.startswith("win"):
        return {"os": "windows", "scheduler": "schtasks", "supported": True}
    if p.startswith("linux"):
        # The digest runs fine on Linux; we just don't auto-install a schedule in Phase 1.
        return {"os": "linux", "scheduler": "cron", "supported": False}
    return {"os": "other", "scheduler": None, "supported": False}


# ---------------------------------------------------------------------------
# Gmail: must be able to READ (IMAP) and SEND (SMTP) with the app password
# ---------------------------------------------------------------------------
def _looks_like_app_password(pw):
    # Google app passwords are 16 letters, usually shown in 4 groups of 4.
    stripped = pw.replace(" ", "")
    return len(stripped) == 16 and stripped.isalpha()


def check_gmail(email, app_password, timeout=NETWORK_TIMEOUT):
    """Verify the address + app password can both read and send.
    Returns a CheckResult. Never raises for ordinary failures."""
    email = (email or "").strip()
    raw = (app_password or "").strip()
    pw = raw.replace(" ", "")   # Google shows the password with spaces; IMAP wants it without

    if not email or "@" not in email:
        return CheckResult(False, "That doesn't look like an email address.",
                           "Enter the full Gmail address, e.g. you@gmail.com.")
    if not raw:
        return CheckResult(False, "No app password entered.",
                           "Paste the 16-character app password from Google.")
    if not _looks_like_app_password(raw):
        # Warn, but still try - some Workspace domains differ.
        hint = ("This should be a 16-letter Google *app password*, not your normal "
                "Gmail password. If you entered your normal password, create an app "
                "password instead.")
    else:
        hint = ""

    # 1) IMAP - can we read the inbox?
    try:
        with imaplib.IMAP4_SSL(GMAIL_IMAP_HOST, timeout=timeout) as imap:
            imap.login(email, pw)
    except imaplib.IMAP4.error:
        return CheckResult(False, "Gmail rejected that email + app password (reading).",
                           hint or "Double-check the address and that the app password "
                           "was copied correctly, with no missing characters.")
    except (socket.gaierror, socket.timeout, TimeoutError, ssl.SSLError, OSError) as e:
        return CheckResult(False, f"Could not reach Gmail to test reading ({e}).",
                           "Check your internet connection and try again.")

    # 2) SMTP - can we send?
    try:
        with smtplib.SMTP_SSL(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, timeout=timeout) as smtp:
            smtp.login(email, pw)
    except smtplib.SMTPAuthenticationError:
        return CheckResult(False, "Gmail accepted reading but rejected sending.",
                           "This is unusual. Make sure IMAP is enabled in Gmail settings "
                           "and that the app password is for 'Mail'.")
    except (smtplib.SMTPException, socket.gaierror, socket.timeout, TimeoutError,
            ssl.SSLError, OSError) as e:
        return CheckResult(False, f"Could not reach Gmail to test sending ({e}).",
                           "Check your internet connection and try again.")

    return CheckResult(True, f"Gmail works - can read and send as {email}.")


# ---------------------------------------------------------------------------
# Groq: key valid AND model available (catches the model-deprecation failure)
# ---------------------------------------------------------------------------
def check_groq(api_key, model):
    """Make a tiny real completion to confirm the key works and the model exists."""
    api_key = (api_key or "").strip()
    if not api_key:
        return CheckResult(False, "No Groq API key entered.",
                           "Paste the key from console.groq.com (it starts with gsk_).")
    if not api_key.startswith("gsk_"):
        return CheckResult(False, "That doesn't look like a Groq key.",
                           "Groq keys start with 'gsk_'. Copy it from console.groq.com.")

    try:
        import groq
    except ImportError:
        return CheckResult(False, "The groq library isn't installed yet.",
                           "Run the dependency-install step first.")

    try:
        client = groq.Groq(api_key=api_key)
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
    except groq.AuthenticationError:
        return CheckResult(False, "Groq rejected that API key.",
                           "Make sure you copied the whole key from console.groq.com.")
    except groq.NotFoundError:
        return CheckResult(False, f"The model '{model}' isn't available on your Groq account.",
                           "The model may have been renamed or retired. Check the model list "
                           "at console.groq.com and update the choice.")
    except groq.PermissionDeniedError:
        return CheckResult(False, f"Your key isn't allowed to use '{model}'.",
                           "Pick a different model, or check your Groq account access.")
    except groq.RateLimitError:
        # The key is clearly valid - it got far enough to be rate-limited.
        return CheckResult(True, "Groq key works (hit a temporary rate limit, which is fine).")
    except (groq.APIConnectionError, groq.APITimeoutError) as e:
        return CheckResult(False, f"Could not reach Groq ({e}).",
                           "Check your internet connection and try again.")
    except groq.APIError as e:
        return CheckResult(False, f"Groq returned an error: {e}",
                           "Try again in a moment; if it persists, check console.groq.com.")

    return CheckResult(True, f"Groq works - key is valid and '{model}' responded.")


# ---------------------------------------------------------------------------
# Writing the .env safely
# ---------------------------------------------------------------------------
def env_quote(value):
    """Render a value for a .env file so python-dotenv reads it back unchanged."""
    value = "" if value is None else str(value)
    if value == "" or any(c in value for c in ' "\'\\\n\t#$'):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    return value


def render_env(data):
    lines = []
    for key, value in data.items():
        if value is None:
            continue
        lines.append(f"{key}={env_quote(value)}")
    return "\n".join(lines) + "\n"


def write_env_file(path, data):
    """Write .env with owner-only permissions where the OS supports it.
    Returns a CheckResult."""
    try:
        # Create with 0600 from the start so secrets are never briefly world-readable.
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, render_env(data).encode("utf-8"))
        finally:
            os.close(fd)
        if os.name == "posix":
            os.chmod(path, 0o600)   # ensure, even if the file already existed
        return CheckResult(True, f"Saved settings to {path}.")
    except OSError as e:
        return CheckResult(False, f"Could not write settings file: {e}",
                           "Check you have permission to write to that folder.")


# ---------------------------------------------------------------------------
# Virtual environment + dependencies
# ---------------------------------------------------------------------------
def venv_python(venv_dir):
    """Path to the python executable inside a venv, cross-platform."""
    if os.name == "nt":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def create_venv(venv_dir):
    """Create an isolated environment. Returns a CheckResult."""
    import venv
    try:
        venv.EnvBuilder(with_pip=True).create(venv_dir)
    except Exception as e:
        return CheckResult(False, f"Could not create the Python environment: {e}",
                           "Make sure Python 3 is installed correctly.")
    if not os.path.exists(venv_python(venv_dir)):
        return CheckResult(False, "The Python environment was created but looks incomplete.",
                           "Try deleting the folder and running setup again.")
    return CheckResult(True, f"Created an isolated Python environment in {venv_dir}.")


def install_requirements(venv_dir, packages, timeout=600):
    """pip install the given packages into the venv. Returns a CheckResult."""
    py = venv_python(venv_dir)
    if not os.path.exists(py):
        return CheckResult(False, "No Python environment to install into.",
                           "Create the environment first.")
    cmd = [py, "-m", "pip", "install", "--upgrade", *packages]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CheckResult(False, "Installing dependencies took too long and was stopped.",
                           "Check your internet connection and try again.")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return CheckResult(False, "Could not install the dependencies.",
                           "pip said: " + " / ".join(tail))
    return CheckResult(True, "Installed all dependencies.")


# The packages digest.py needs.
REQUIREMENTS = ["groq", "beautifulsoup4", "python-dotenv", "requests"]
