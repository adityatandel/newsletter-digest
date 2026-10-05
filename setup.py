#!/usr/bin/env python3
"""
setup.py - guided setup wizard for the Newsletter Digest (Phase 1: email only).

Run this once:   python3 setup.py
Remove later:    python3 setup.py --uninstall

It walks you through the whole thing: checks your system, helps you create the
Gmail app password and Groq key (testing each on the spot), asks how often you
want the digest, saves your settings, schedules it, and does a test run.

The real work lives in setup_lib.py (checks, .env, venv) and scheduler.py
(launchd / Task Scheduler). This file is just the conversation on top of them.
"""

import os
import sys
import json
import shutil
import argparse
import subprocess

import setup_lib as _lib
import scheduler as _sched

APP_PASSWORD_URL = "https://myaccount.google.com/apppasswords"
TWO_STEP_URL     = "https://myaccount.google.com/signinoptions/twosv"
GROQ_KEYS_URL    = "https://console.groq.com/keys"
GROQ_PRICING_URL = "https://groq.com/pricing/"

DEFAULT_MODEL = "openai/gpt-oss-120b"
FILES_TO_INSTALL = ["digest.py", "setup_lib.py", "scheduler.py"]
LOG_NAME = "digest.log"
MAX_TRIES = 3


# ---------------------------------------------------------------------------
# Console input/output (swapped out in tests)
# ---------------------------------------------------------------------------
class ConsoleIO:
    def say(self, text=""):
        print(text)

    def rule(self, title=""):
        print("\n" + "=" * 62)
        if title:
            print(title)
            print("=" * 62)

    def ask(self, prompt, default=None):
        suffix = f" [{default}]" if default else ""
        while True:
            val = input(f"{prompt}{suffix}: ").strip()
            if val:
                return val
            if default is not None:
                return default

    def ask_secret(self, prompt):
        import getpass
        return getpass.getpass(f"{prompt}: ").strip()

    def confirm(self, prompt, default=True):
        hint = "Y/n" if default else "y/N"
        while True:
            val = input(f"{prompt} [{hint}]: ").strip().lower()
            if not val:
                return default
            if val in ("y", "yes"):
                return True
            if val in ("n", "no"):
                return False

    def choose(self, prompt, options):
        """options: list of (value, label). Returns the chosen value."""
        self.say(prompt)
        for i, (_, label) in enumerate(options, 1):
            self.say(f"  {i}. {label}")
        while True:
            val = input("Enter a number: ").strip()
            if val.isdigit() and 1 <= int(val) <= len(options):
                return options[int(val) - 1][0]


# ---------------------------------------------------------------------------
# Thin wrappers around the heavy operations (patched in tests)
# ---------------------------------------------------------------------------
def _check_gmail(email, pw):
    return _lib.check_gmail(email, pw)


def _check_groq(venv_py, install_dir, key, model):
    """Validate the Groq key INSIDE the venv (where the groq library lives)."""
    code = ("import json, os, setup_lib;"
            "r = setup_lib.check_groq(os.environ['GK'], os.environ['GM']);"
            "print(json.dumps({'ok': r.ok, 'message': r.message, 'hint': r.hint}))")
    env = dict(os.environ, GK=key, GM=model)
    try:
        p = subprocess.run([venv_py, "-c", code], cwd=install_dir, env=env,
                           capture_output=True, text=True, timeout=90)
        line = (p.stdout or "").strip().splitlines()[-1]
        d = json.loads(line)
        return _lib.CheckResult(d["ok"], d["message"], d.get("hint", ""))
    except Exception as e:
        return _lib.CheckResult(False, f"Couldn't run the Groq check ({e}).",
                                "Make sure the dependencies installed correctly.")


def _create_venv(venv_dir):
    return _lib.create_venv(venv_dir)


def _install_requirements(venv_dir):
    return _lib.install_requirements(venv_dir, _lib.REQUIREMENTS)


def _write_env(path, data):
    return _lib.write_env_file(path, data)


def _install_schedule(platform_os, venv_py, script, log, schedule, install_dir):
    return _sched.install_schedule(platform_os, venv_py, script, log, schedule, install_dir)


def _run_once(venv_py, script, install_dir):
    """Run the digest a single time to prove the whole chain works."""
    try:
        p = subprocess.run([venv_py, script], cwd=install_dir,
                           capture_output=True, text=True, timeout=600)
        return p.returncode == 0, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return False, str(e)


def _open_url(url):
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Individual steps (each testable)
# ---------------------------------------------------------------------------
def step_constraints(io):
    io.rule("Before you begin")
    io.say("A few things to know first:\n")
    for c in _lib.CONSTRAINTS:
        io.say("  - " + c)
    io.say("")
    return io.confirm("Happy to continue?", default=True)


def step_platform(io):
    plat = _lib.detect_platform()
    pyres = _lib.check_python_version()
    if not pyres.ok:
        io.say("\n" + pyres.message)
        io.say(pyres.hint)
        return None
    if not plat["supported"]:
        io.say(f"\nThis looks like {plat['os']}. Automatic scheduling isn't supported "
               "here yet, so the digest can be installed but you'd run it yourself.")
        if not io.confirm("Continue without automatic scheduling?", default=False):
            return None
    return plat


def step_install_dir(io, source_dir, default_dir):
    io.rule("Where to install")
    install_dir = io.ask("Folder to install into", default=default_dir)
    install_dir = os.path.abspath(os.path.expanduser(install_dir))
    os.makedirs(install_dir, exist_ok=True)
    if os.path.abspath(source_dir) != install_dir:
        for name in FILES_TO_INSTALL:
            src = os.path.join(source_dir, name)
            if os.path.exists(src):
                shutil.copy(src, os.path.join(install_dir, name))
    return install_dir


def step_dependencies(io, venv_dir):
    io.rule("Installing dependencies")
    io.say("Creating an isolated Python environment (this can take a minute)...")
    r = _create_venv(venv_dir)
    io.say(r.message)
    if not r.ok:
        io.say(r.hint)
        return False
    r = _install_requirements(venv_dir)
    io.say(r.message)
    if not r.ok:
        io.say(r.hint)
        return False
    return True


def step_gmail(io):
    io.rule("Step 1 of 2: Gmail")
    io.say("The digest reads newsletters from a Gmail inbox and emails you the summary.")
    io.say("You need a 16-character Google 'app password' (not your normal password).")
    io.say("This requires 2-Step Verification to be on.\n")
    if io.confirm("Open the Google app-password page now?", default=True):
        _open_url(APP_PASSWORD_URL)
        io.say(f"If it didn't open: {APP_PASSWORD_URL}")
        io.say(f"(Need 2-Step Verification first? {TWO_STEP_URL})")

    for attempt in range(MAX_TRIES):
        email = io.ask("\nYour Gmail address")
        pw = io.ask_secret("Your 16-character app password (input hidden)")
        io.say("Testing Gmail (reading and sending)...")
        r = _check_gmail(email, pw)
        io.say(r.message)
        if r.ok:
            return {"email": email, "password": pw.replace(" ", "")}
        io.say(r.hint)
        if attempt < MAX_TRIES - 1 and not io.confirm("Try again?", default=True):
            break
    return None


def step_groq(io, venv_py, install_dir, model):
    io.rule("Step 2 of 2: Groq (the AI summarizer)")
    io.say("The digest uses Groq to summarize newsletters. You need a free API key.")
    io.say("Cost: usually only cents per run for a handful of newsletters, but you pay")
    io.say(f"for your own usage and prices change - see {GROQ_PRICING_URL}\n")
    if io.confirm("Open the Groq keys page now?", default=True):
        _open_url(GROQ_KEYS_URL)
        io.say(f"If it didn't open: {GROQ_KEYS_URL}")

    for attempt in range(MAX_TRIES):
        key = io.ask_secret("\nYour Groq API key (starts with gsk_, input hidden)")
        io.say("Testing the key and the model...")
        r = _check_groq(venv_py, install_dir, key, model)
        io.say(r.message)
        if r.ok:
            return key
        io.say(r.hint)
        if attempt < MAX_TRIES - 1 and not io.confirm("Try again?", default=True):
            break
    return None


def step_schedule(io):
    io.rule("How often?")
    freq = io.choose("How often should the digest run?", [
        ("daily", "Every day"),
        ("weekdays", "Weekdays (Mon-Fri)"),
        ("mwf", "Mon / Wed / Fri"),
        ("weekly", "Once a week"),
    ])
    weekly_day = 0
    if freq == "weekly":
        weekly_day = io.choose("Which day?", [
            (0, "Monday"), (1, "Tuesday"), (2, "Wednesday"), (3, "Thursday"),
            (4, "Friday"), (5, "Saturday"), (6, "Sunday"),
        ])
    while True:
        t = io.ask("What time? (24-hour HH:MM)", default="07:00")
        try:
            hh, mm = t.split(":")
            schedule = _sched.resolve_schedule(freq, int(hh), int(mm), weekly_day=weekly_day)
            return schedule
        except (ValueError, IndexError):
            io.say("Please enter a time like 07:00 or 18:30.")


def run_wizard(io, source_dir=None, default_dir=None):
    source_dir = source_dir or os.path.dirname(os.path.abspath(__file__))
    default_dir = default_dir or os.path.join(os.path.expanduser("~"), "newsletter-digest")

    io.rule("Newsletter Digest - Setup")
    if not step_constraints(io):
        io.say("No problem - setup cancelled."); return False

    plat = step_platform(io)
    if plat is None:
        io.say("Setup stopped."); return False

    install_dir = step_install_dir(io, source_dir, default_dir)
    venv_dir = os.path.join(install_dir, "venv")
    venv_py = _lib.venv_python(venv_dir)
    script = os.path.join(install_dir, "digest.py")
    log_path = os.path.join(install_dir, LOG_NAME)

    if not step_dependencies(io, venv_dir):
        io.say("Setup stopped - dependencies didn't install."); return False

    gmail = step_gmail(io)
    if not gmail:
        io.say("Setup stopped - Gmail wasn't verified."); return False

    groq_key = step_groq(io, venv_py, install_dir, DEFAULT_MODEL)
    if not groq_key:
        io.say("Setup stopped - Groq key wasn't verified."); return False

    schedule = step_schedule(io)

    # Save settings (email-only: no Telegram keys written).
    env_path = os.path.join(install_dir, ".env")
    r = _write_env(env_path, {
        "IMAP_EMAIL": gmail["email"],
        "IMAP_PASSWORD": gmail["password"],
        "RECIPIENT_EMAIL": gmail["email"],
        "GROQ_API_KEY": groq_key,
        "DIGEST_MODEL": DEFAULT_MODEL,
    })
    io.say(r.message)
    if not r.ok:
        io.say(r.hint); return False

    io.rule("Scheduling")
    ok, msg = _install_schedule(plat["os"], venv_py, script, log_path, schedule, install_dir)
    io.say(msg)
    if not ok:
        io.say("You can still run the digest yourself; scheduling just isn't set up.")

    if io.confirm("\nDo a test run now to make sure it all works?", default=True):
        io.say("Running once (this may take a minute)...")
        ran_ok, output = _run_once(venv_py, script, install_dir)
        io.say("Test run finished." if ran_ok else "Test run reported a problem:")
        tail = "\n".join(output.strip().splitlines()[-8:])
        if tail:
            io.say(tail)

    io.rule("All done")
    io.say(f"Installed in: {install_dir}")
    io.say(f"Schedule:     {schedule.human()}")
    io.say(f"Log file:     {log_path}")
    io.say(f"To remove:    python3 setup.py --uninstall")
    return True


def run_uninstall(io, default_dir=None):
    plat = _lib.detect_platform()
    io.rule("Uninstall")
    ok, msg = _sched.uninstall_schedule(plat["os"])
    io.say(msg)
    io.say("Your installed folder and .env were left in place; delete them manually "
           "if you want them gone (the .env holds your keys).")
    return ok


def main(argv=None):
    parser = argparse.ArgumentParser(description="Newsletter Digest setup")
    parser.add_argument("--uninstall", action="store_true", help="remove the schedule")
    args = parser.parse_args(argv)
    io = ConsoleIO()
    try:
        if args.uninstall:
            return 0 if run_uninstall(io) else 1
        return 0 if run_wizard(io) else 1
    except KeyboardInterrupt:
        io.say("\nCancelled."); return 1


if __name__ == "__main__":
    sys.exit(main())
