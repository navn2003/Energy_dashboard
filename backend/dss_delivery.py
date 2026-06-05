#!/usr/bin/env python3
"""
dss_delivery.py
===============
In-process helpers for the "Daily Data Delivery" web page. Reuses the SFTP +
column-counting logic from parse_mail.py, minus the email/CLI parts, so the
FastAPI backend can serve the same report as JSON and stream the CSVs.

Date offsets relative to the run day (same as parse_mail.py):
    DA2 (_00_25)  ->  run day + 1
    R16 (_16_00)  ->  run day - 1
"""

import os
import csv
import io
import logging
from datetime import datetime, timedelta, date as _date

log = logging.getLogger("dss_delivery")

# Daylight window during which a solar DSS forecast must NOT be null.
DAYLIGHT_START_MIN = int(os.environ.get("DSS_DAYLIGHT_START", str(5 * 60 + 30)))   # 05:30 -> 330
DAYLIGHT_END_MIN   = int(os.environ.get("DSS_DAYLIGHT_END",   str(19 * 60)))       # 19:00 -> 1140

# Strings that count as "null" in a cell (besides truly empty / NaN).
_NULL_TOKENS = {"", "null", "na", "nan", "none", "n/a", "-", "--"}

# ── Config (env-overridable, mirrors parse_mail.py) ──────────────────────────
EXPECTED_DSS = int(os.environ.get("DSS_EXPECTED", "212"))

SSH_HOST   = os.environ.get("SSH_HOST", "223.31.122.178")
SSH_PORT   = int(os.environ.get("SSH_PORT", "22"))
SSH_USER   = os.environ.get("SSH_USER", "ubuntu")
SSH_PASS   = os.environ.get("SSH_PASS", "eye_sftp@123")
SSH_KEY_PATH = os.environ.get("SSH_KEY_PATH", "")
SSH_KEY_PASS = os.environ.get("SSH_KEY_PASS", "")
REMOTE_DIR = os.environ.get("DSS_REMOTE_DIR", "/home/ubuntu")

# Optional local mode (handy for testing without the EC2 box).
MODE    = os.environ.get("DSS_MODE", "remote")        # "remote" or "local"
CSV_DIR = os.environ.get("DSS_CSV_DIR", "/home/ubuntu")

REVISION_MAP = {"_00_25": "DA2", "_16_00": "R16"}
DATE_OFFSET  = {"_00_25": +1,    "_16_00": -1}
DAILY_SUFFIXES = list(REVISION_MAP.keys())


def _filename_for(suffix, base_day, apply_offset=True):
    off = DATE_OFFSET.get(suffix, 0) if apply_offset else 0
    d = base_day + timedelta(days=off)
    return f"{d.strftime('%Y%m%d')}{suffix}.csv"


def expected_filenames(base_day, apply_offset=True):
    """All filenames we expect for a given day, with their revision label."""
    out = []
    for suffix in DAILY_SUFFIXES:
        out.append({
            "file": _filename_for(suffix, base_day, apply_offset),
            "revision": REVISION_MAP[suffix],
            "suffix": suffix,
        })
    return out


def is_valid_dss_filename(name):
    """Whitelist check: YYYYMMDD + known suffix + .csv (prevents path traversal)."""
    base = os.path.basename(name or "")
    if not base.lower().endswith(".csv"):
        return False
    stem = base[:-4]
    if len(stem) < 9:
        return False
    date_part, suffix = stem[:8], stem[8:]
    if suffix not in REVISION_MAP:
        return False
    try:
        datetime.strptime(date_part, "%Y%m%d")
    except ValueError:
        return False
    return True


def _count_dss_in_header(header):
    return sum(1 for name in header if name.strip().upper().startswith("DSS"))


def _header_from_first_line(line):
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    return next(csv.reader([line]))


def _parse_filename(name):
    base = os.path.basename(name)
    stem = base[:-4] if base.lower().endswith(".csv") else base
    date_part, suffix = stem[:8], stem[8:]
    try:
        date_iso = datetime.strptime(date_part, "%Y%m%d").date().isoformat()
    except ValueError:
        date_iso = date_part or "unknown"
    return date_iso, REVISION_MAP.get(suffix, "unknown")


def _minute_of_day(text):
    """
    Parse a time/timestamp cell into minutes-of-day (0-1439), or None if unparseable.
    Handles: full datetimes, 'HH:MM[:SS]', and integer block numbers (1-96 -> 15-min slots).
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None

    # Integer block number (1..96 or 0..95) -> 15-minute slots.
    if s.isdigit():
        n = int(s)
        if 0 <= n <= 96:
            base = (n - 1) if n >= 1 else n  # treat block 1 as 00:00
            return (base * 15) % 1440

    # Full datetime, several common layouts.
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M",
                "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
                "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.hour * 60 + dt.minute
        except ValueError:
            pass

    # Bare time HH:MM or HH:MM:SS (also pulls the time out of an ISO 'T' stamp).
    t = s.split("T")[-1] if "T" in s else s
    t = t.split()[-1] if " " in t and ":" in t.split()[-1] else t
    parts = t.split(":")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        h, m = int(parts[0]), int(parts[1])
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    return None


def _is_null_cell(v):
    return str(v).strip().lower() in _NULL_TOKENS


def analyze_daylight_nulls(content, filename=""):
    """
    Returns DSS columns that contain null values during the daylight window
    (05:30-19:00). Pure-stdlib CSV parse so it works without pandas.

    Returns: {
        time_col, window_rows, flagged: [{dss, null_count, example_times:[...]}],
        error
    }
    """
    out = {"time_col": None, "window_rows": 0, "flagged": [], "error": None}
    try:
        text = content.decode("utf-8", errors="replace")
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if len(rows) < 2:
            out["error"] = "File has no data rows."
            return out

        header = rows[0]
        dss_idx = [i for i, name in enumerate(header) if name.strip().upper().startswith("DSS")]

        # Pick the time column: a header that looks time-like, else the first column.
        time_i = 0
        for i, name in enumerate(header):
            low = name.strip().lower()
            if any(k in low for k in ("time", "timestamp", "datetime", "date_time",
                                      "block", "period", "slot", "interval")):
                time_i = i
                break
        out["time_col"] = header[time_i] if time_i < len(header) else f"col{time_i}"

        null_counts = {}     # dss name -> count
        examples = {}        # dss name -> [times]
        window_rows = 0

        for r in rows[1:]:
            if time_i >= len(r):
                continue
            mod = _minute_of_day(r[time_i])
            if mod is None or not (DAYLIGHT_START_MIN <= mod <= DAYLIGHT_END_MIN):
                continue
            window_rows += 1
            hhmm = f"{mod // 60:02d}:{mod % 60:02d}"
            for i in dss_idx:
                if i >= len(r):
                    continue
                if _is_null_cell(r[i]):
                    name = header[i].strip()
                    null_counts[name] = null_counts.get(name, 0) + 1
                    if len(examples.setdefault(name, [])) < 6:
                        examples[name].append(hhmm)

        out["window_rows"] = window_rows
        out["flagged"] = [
            {"dss": name, "null_count": null_counts[name], "example_times": examples.get(name, [])}
            for name in sorted(null_counts)
        ]
    except Exception as e:
        out["error"] = f"Null analysis failed: {e}"
    return out


def _row_from_content(name, content):
    date_iso, revision = _parse_filename(name)
    header = _header_from_first_line(content.split(b"\n", 1)[0])
    present = _count_dss_in_header(header)
    null_info = analyze_daylight_nulls(content, name)
    return {
        "file": os.path.basename(name),
        "date": date_iso,
        "revision": revision,
        "present": present,
        "expected": EXPECTED_DSS,
        "ok": present == EXPECTED_DSS,
        "size_kb": round(len(content) / 1024, 1),
        "null_analysis": null_info,
    }


# ── Private key loading (key-auth fallback) ──────────────────────────────────
def _load_private_key(paramiko, key_path, passphrase=None):
    path = os.path.expanduser(key_path)
    last_err = None
    for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey, paramiko.DSSKey):
        try:
            return cls.from_private_key_file(path, password=passphrase)
        except paramiko.PasswordRequiredException:
            raise RuntimeError("SSH key is passphrase-protected; set SSH_KEY_PASS.")
        except Exception as e:
            last_err = e
    raise last_err


def _open_sftp():
    import paramiko
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = dict(hostname=SSH_HOST, port=SSH_PORT, username=SSH_USER, timeout=30)
    if SSH_PASS:
        kwargs.update(password=SSH_PASS, look_for_keys=False, allow_agent=False)
    elif SSH_KEY_PATH:
        kwargs["pkey"] = _load_private_key(paramiko, SSH_KEY_PATH, SSH_KEY_PASS or None)
    else:
        raise RuntimeError("Set SSH_PASS or SSH_KEY_PATH for remote mode.")
    client.connect(**kwargs)
    return client, client.open_sftp()


# ── Public API used by main.py ───────────────────────────────────────────────
def fetch_one(filename, base_day=None):
    """Return the raw bytes of one CSV (remote or local). None if missing."""
    if not is_valid_dss_filename(filename):
        raise ValueError("Invalid DSS filename.")
    fname = os.path.basename(filename)

    if MODE == "local":
        path = os.path.join(CSV_DIR, fname)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            return f.read()

    client, sftp = _open_sftp()
    try:
        remote_path = f"{REMOTE_DIR.rstrip('/')}/{fname}"
        try:
            with sftp.open(remote_path) as f:
                f.prefetch()
                return f.read()
        except IOError:
            return None
    finally:
        client.close()


def build_report(base_day, apply_offset=True):
    """
    Returns a dict:
      {run_date, expected, mode, rows:[...], error}
    Each row: file, date, revision, present, expected, ok, size_kb, found(bool)

    apply_offset=True  -> parse_mail.py behaviour (DA2 = day+1, R16 = day-1)
    apply_offset=False -> filenames use base_day directly (web page behaviour)
    """
    result = {
        "run_date": base_day.isoformat(),
        "expected": EXPECTED_DSS,
        "mode": MODE,
        "rows": [],
        "error": None,
    }
    targets = expected_filenames(base_day, apply_offset)

    if MODE == "local":
        for t in targets:
            path = os.path.join(CSV_DIR, t["file"])
            if not os.path.isfile(path):
                result["rows"].append(_missing_row(t))
                continue
            with open(path, "rb") as f:
                content = f.read()
            row = _row_from_content(t["file"], content)
            row["found"] = True
            result["rows"].append(row)
        return result

    # remote
    try:
        import paramiko  # noqa: F401
    except ImportError:
        result["error"] = "Remote mode needs paramiko on the API server (pip install paramiko)."
        for t in targets:
            result["rows"].append(_missing_row(t))
        return result

    try:
        client, sftp = _open_sftp()
    except Exception as e:
        result["error"] = f"SSH/SFTP connection failed: {e}"
        for t in targets:
            result["rows"].append(_missing_row(t))
        return result

    try:
        try:
            existing = set(sftp.listdir(REMOTE_DIR))
        except IOError:
            existing = set()
            result["error"] = f"Remote dir not found: {REMOTE_DIR}"

        for t in targets:
            if t["file"] not in existing:
                log.warning("Remote file missing: %s", t["file"])
                result["rows"].append(_missing_row(t))
                continue
            remote_path = f"{REMOTE_DIR.rstrip('/')}/{t['file']}"
            with sftp.open(remote_path) as f:
                f.prefetch()
                content = f.read()
            row = _row_from_content(t["file"], content)
            row["found"] = True
            result["rows"].append(row)
    finally:
        client.close()

    return result


def _missing_row(t):
    return {
        "file": t["file"],
        "date": _parse_filename(t["file"])[0],
        "revision": t["revision"],
        "present": 0,
        "expected": EXPECTED_DSS,
        "ok": False,
        "size_kb": 0,
        "found": False,
        "null_analysis": {"time_col": None, "window_rows": 0, "flagged": [], "error": None},
    }