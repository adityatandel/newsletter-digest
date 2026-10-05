"""
scheduler.py - install/remove/inspect the digest's schedule, on Mac and Windows.

One common schedule description (resolve_schedule) is rendered two ways:
  - macOS  -> a launchd user agent (plist in ~/Library/LaunchAgents), loaded with
              launchctl. launchd catches up a run missed while the Mac slept.
  - Windows -> a Scheduled Task created with schtasks, pointed at a small .bat
               launcher so we avoid schtasks' quoting pitfalls and still get a log.

The pure "build" functions (plist bytes, bat text, command arg lists, output
parsing) contain all the fiddly OS-specific detail and are fully unit-tested.
The thin wrappers that actually call launchctl/schtasks can only be verified on a
real Mac/Windows machine.
"""

import os
import re
import sys
import plistlib
import subprocess
from dataclasses import dataclass, field

TASK_NAME = "NewsletterDigest"
LAUNCHD_LABEL = "com.newsletterdigest.daily"

# Day handling uses Python's convention everywhere: Monday=0 ... Sunday=6.
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SCHTASKS_DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

FREQUENCIES = {
    "daily":    None,            # every day
    "weekdays": [0, 1, 2, 3, 4],
    "mwf":      [0, 2, 4],
    "weekly":   "weekly_day",    # resolved using the chosen day
}


@dataclass
class Schedule:
    hour: int
    minute: int
    days: list = field(default_factory=list)  # [] / None means every day
    label: str = LAUNCHD_LABEL

    def human(self):
        when = f"{self.hour:02d}:{self.minute:02d}"
        if not self.days:
            return f"every day at {when}"
        names = ", ".join(WEEKDAY_NAMES[d] for d in self.days)
        return f"{names} at {when}"


def resolve_schedule(frequency, hour, minute, weekly_day=0):
    """Turn a frequency choice + time into a Schedule. Raises ValueError on bad input."""
    if frequency not in FREQUENCIES:
        raise ValueError(f"Unknown frequency: {frequency!r}. "
                         f"Choose one of: {', '.join(FREQUENCIES)}.")
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        raise ValueError("Time must be between 00:00 and 23:59.")

    spec = FREQUENCIES[frequency]
    if spec is None:
        days = None
    elif spec == "weekly_day":
        if not (0 <= weekly_day <= 6):
            raise ValueError("Day of week must be 0 (Mon) to 6 (Sun).")
        days = [weekly_day]
    else:
        days = list(spec)
    return Schedule(hour=hour, minute=minute, days=days)


# ---------------------------------------------------------------------------
# macOS (launchd)
# ---------------------------------------------------------------------------
def _launchd_weekday(d):
    # Python Mon=0..Sun=6  ->  launchd Sun=0, Mon=1 .. Sat=6
    return (d + 1) % 7


def build_launchd_plist(python_exe, script_path, schedule, log_path):
    """Return the plist as bytes (valid XML, correctly escaped by plistlib)."""
    if not schedule.days:
        calendar = {"Hour": schedule.hour, "Minute": schedule.minute}
    else:
        calendar = [
            {"Weekday": _launchd_weekday(d), "Hour": schedule.hour, "Minute": schedule.minute}
            for d in schedule.days
        ]
    plist = {
        "Label": schedule.label,
        "ProgramArguments": [python_exe, script_path],
        "StartCalendarInterval": calendar,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
        "RunAtLoad": False,
    }
    return plistlib.dumps(plist)


def launchd_plist_path(label=LAUNCHD_LABEL):
    return os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")


def launchd_install_commands(plist_path):
    """Command sequence to (re)load the agent. unload may fail harmlessly if absent."""
    return [
        (["launchctl", "unload", plist_path], True),    # True = ignore failure
        (["launchctl", "load", "-w", plist_path], False),
    ]


def launchd_uninstall_commands(plist_path):
    return [(["launchctl", "unload", "-w", plist_path], True)]


# ---------------------------------------------------------------------------
# Windows (schtasks + .bat launcher)
# ---------------------------------------------------------------------------
def build_bat_launcher(python_exe, script_path, log_path):
    """A tiny batch file so Task Scheduler runs one clean token and we still log."""
    return (
        "@echo off\r\n"
        f'"{python_exe}" "{script_path}" >> "{log_path}" 2>&1\r\n'
    )


def build_schtasks_create_args(bat_path, schedule, task_name=TASK_NAME):
    """schtasks /Create argument list. /TR is quoted to tolerate spaces in the path."""
    args = ["schtasks", "/Create", "/TN", task_name,
            "/TR", f'"{bat_path}"', "/F",
            "/ST", f"{schedule.hour:02d}:{schedule.minute:02d}"]
    if not schedule.days:
        args += ["/SC", "DAILY"]
    else:
        days = ",".join(SCHTASKS_DAYS[d] for d in schedule.days)
        args += ["/SC", "WEEKLY", "/D", days]
    return args


def build_schtasks_delete_args(task_name=TASK_NAME):
    return ["schtasks", "/Delete", "/TN", task_name, "/F"]


def build_schtasks_query_args(task_name=TASK_NAME):
    return ["schtasks", "/Query", "/TN", task_name, "/V", "/FO", "LIST"]


def parse_schtasks_query(output):
    """Pull the useful fields out of `schtasks /Query /V /FO LIST` text."""
    wanted = {
        "Last Run Time": "last_run",
        "Last Result": "last_result",
        "Next Run Time": "next_run",
        "Status": "status",
    }
    found = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key in wanted:
            found[wanted[key]] = value.strip()
    return found


# ---------------------------------------------------------------------------
# Cross-platform dispatch (thin wrappers; real calls need a real Mac/Windows)
# ---------------------------------------------------------------------------
def _run(cmd, ignore_fail=False):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        if ignore_fail:
            return True, ""
        return False, str(e)
    if proc.returncode != 0 and not ignore_fail:
        return False, (proc.stderr or proc.stdout or "").strip()
    return True, (proc.stdout or "").strip()


def install_schedule(platform_os, python_exe, script_path, log_path, schedule, project_dir):
    """Install the schedule for the given OS. Returns (ok, message)."""
    if platform_os == "mac":
        path = launchd_plist_path(schedule.label)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(build_launchd_plist(python_exe, script_path, schedule, log_path))
        for cmd, ignore in launchd_install_commands(path):
            ok, msg = _run(cmd, ignore)
            if not ok:
                return False, f"launchctl failed: {msg}"
        return True, f"Scheduled with launchd: {schedule.human()}."

    if platform_os == "windows":
        bat_path = os.path.join(project_dir, "run_digest.bat")
        with open(bat_path, "w", newline="") as f:
            f.write(build_bat_launcher(python_exe, script_path, log_path))
        ok, msg = _run(build_schtasks_create_args(bat_path, schedule))
        if not ok:
            return False, f"schtasks failed: {msg}"
        return True, f"Scheduled with Task Scheduler: {schedule.human()}."

    return False, (f"Automatic scheduling isn't supported on this system ({platform_os}). "
                   f"You can still run digest.py yourself.")


def uninstall_schedule(platform_os, label=LAUNCHD_LABEL, task_name=TASK_NAME):
    if platform_os == "mac":
        path = launchd_plist_path(label)
        for cmd, ignore in launchd_uninstall_commands(path):
            _run(cmd, ignore)
        if os.path.exists(path):
            os.remove(path)
        return True, "Removed the launchd schedule."
    if platform_os == "windows":
        ok, msg = _run(build_schtasks_delete_args(task_name))
        return (ok, "Removed the scheduled task." if ok else f"Could not remove task: {msg}")
    return False, "Nothing to remove on this system."
