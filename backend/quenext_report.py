"""
quenext_report.py — Connect to the Quenext SFTP, and for a given week classify
each expected revision file per day as ON_TIME / OFF_TIME / MISSING.

Rules (from the tender):
- Intra-day  R-1..R-16 : ON_TIME only if deposit IST time is INSIDE the window
                         [start, end]; before start OR after end = OFF_TIME.
- Day-ahead  RD1..RD4  : ON_TIME if deposit IST time is AT OR BEFORE the deadline;
                         after = OFF_TIME.
- Not deposited at all  : MISSING.
Deposit time = the SFTP server modified time, converted to IST (UTC+5:30).

NOTE: the SFTP only accepts the whitelisted IP, so this runs on the EC2 that
serves the app (same host that runs ingest_quenext.py).
"""
import os
import re
from datetime import datetime, date, timedelta, time
from zoneinfo import ZoneInfo

import paramiko

# ── SFTP config (same as the ingest / report scripts) ────────────────────────
SFTP_HOST = "223.31.122.178"
SFTP_PORT = 22
SFTP_USER = "Indianeye"
SFTP_PASS = "eye_sftp@123"
SFTP_DIR  = "/upload"

IST = ZoneInfo("Asia/Kolkata")

# ── Revision schedule ────────────────────────────────────────────────────────
# Intra-day windows (inclusive start, inclusive end), IST.
_INTRADAY_WINDOWS = {
    "R-1":  (time(23, 30), time(23, 45)),
    "R-2":  (time(1, 0),   time(1, 15)),
    "R-3":  (time(2, 30),  time(2, 45)),
    "R-4":  (time(4, 0),   time(4, 15)),
    "R-5":  (time(5, 30),  time(5, 45)),
    "R-6":  (time(7, 0),   time(7, 15)),
    "R-7":  (time(8, 30),  time(8, 45)),
    "R-8":  (time(10, 0),  time(10, 15)),
    "R-9":  (time(11, 30), time(11, 45)),
    "R-10": (time(13, 0),  time(13, 15)),
    "R-11": (time(14, 30), time(14, 45)),
    "R-12": (time(16, 0),  time(16, 15)),
    "R-13": (time(17, 30), time(17, 45)),
    "R-14": (time(19, 0),  time(19, 15)),
    "R-15": (time(20, 30), time(20, 45)),
    "R-16": (time(22, 0),  time(22, 15)),
}
# Day-ahead deadlines (on or before), IST.
_DAYAHEAD_DEADLINES = {
    "RD1": time(5, 0),
    "RD2": time(12, 0),
    "RD3": time(18, 0),
    "RD4": time(22, 0),
}

# Display order: day-ahead first, then R-1..R-16.
EXPECTED_REVISIONS = ["RD1", "RD2", "RD3", "RD4"] + [f"R-{i}" for i in range(1, 17)]


def map_revision(filename: str):
    """20260615_16_00.csv -> (date, 'R-16'); 20260615_00_25.csv -> (date, 'RD2')."""
    name = os.path.basename(filename)
    m = re.match(r"^(\d{8})_(\d{2})_(\d{2})\.csv$", name, re.IGNORECASE)
    if not m:
        return None, None
    yyyymmdd, hh, mm = m.group(1), m.group(2), m.group(3)
    file_date = datetime.strptime(yyyymmdd, "%Y%m%d").date()
    if hh == "00" and mm == "00":
        rev = "RD1"
    elif hh == "00" and mm == "25":
        rev = "RD2"
    elif hh == "00" and mm == "50":
        rev = "RD3"
    elif hh == "00" and mm == "75":
        rev = "RD4"
    elif mm == "00":
        rev = f"R-{int(hh)}"
    else:
        rev = None
    return file_date, rev


def _classify(rev, deposit_ist):
    """Return ON_TIME / OFF_TIME for a deposited file, given its IST datetime."""
    t = deposit_ist.time()
    if rev in _DAYAHEAD_DEADLINES:
        return "ON_TIME" if t <= _DAYAHEAD_DEADLINES[rev] else "OFF_TIME"
    if rev in _INTRADAY_WINDOWS:
        start, end = _INTRADAY_WINDOWS[rev]
        return "ON_TIME" if (start <= t <= end) else "OFF_TIME"
    return "OFF_TIME"


def _connect():
    transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
    transport.connect(username=SFTP_USER, password=SFTP_PASS)
    return paramiko.SFTPClient.from_transport(transport), transport


def week_report(monday: date):
    """
    Build the ON/OFF/MISSING report for the Mon..Sun week starting at `monday`.
    Returns a dict with per-revision counts and a per-day/revision grid.
    """
    days = [monday + timedelta(days=i) for i in range(7)]
    day_strs = [d.isoformat() for d in days]

    # deposits[(date_iso, rev)] = earliest IST datetime seen
    deposits = {}

    sftp, transport = _connect()
    try:
        for entry in sftp.listdir_attr(SFTP_DIR):
            fdate, rev = map_revision(entry.filename)
            if rev is None or fdate is None:
                continue
            if fdate not in days:
                continue
            # server mtime is UTC epoch -> IST
            deposit_ist = datetime.fromtimestamp(entry.st_mtime, tz=ZoneInfo("UTC")).astimezone(IST)
            key = (fdate.isoformat(), rev)
            # keep the earliest deposit for that revision-day
            if key not in deposits or deposit_ist < deposits[key]:
                deposits[key] = deposit_ist
    finally:
        sftp.close()
        transport.close()

    # Build per-revision summary + grid.
    revisions = []
    grid = {}   # rev -> { date_iso -> status }
    for rev in EXPECTED_REVISIONS:
        on = off = miss = 0
        row = {}
        for d_iso in day_strs:
            dep = deposits.get((d_iso, rev))
            if dep is None:
                status = "MISSING"
                miss += 1
            else:
                status = _classify(rev, dep)
                if status == "ON_TIME":
                    on += 1
                else:
                    off += 1
            row[d_iso] = {
                "status": status,
                "time": dep.strftime("%H:%M:%S") if dep else None,
            }
        grid[rev] = row
        revisions.append({
            "revision": rev,
            "on_time": on,
            "off_time": off,
            "missing": miss,
        })

    return {
        "week_start": monday.isoformat(),
        "week_end": days[-1].isoformat(),
        "days": day_strs,
        "revisions": revisions,
        "grid": grid,
    }
