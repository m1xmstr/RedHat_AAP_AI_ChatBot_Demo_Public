"""Bounded text extraction for publisher-provided iCalendar and PDF downloads."""
import datetime
import hashlib
import io
import json
import re
import subprocess
import sys
import threading
from pathlib import Path

PDF_SLOT = threading.Semaphore(1)
WHEEL = "pypdf-7.1.1-py3-none-any.whl"
SHA256 = "04c5680d4b4cfa04ab505b09393fc949860c34a4dad6565284d11a2f5c3ce8b1"


def calendar_text(data):
    data = re.sub(r"\r?\n[ \t]", "", data)
    title = "Calendar"
    events = []
    event = None
    nested = 0
    def readable(value):
        return re.sub(r"\\([nN,;\\])", lambda m: " " if m[1].lower() == "n" else m[1], value).strip()
    for line in data.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        prop, _, params = key.partition(";")
        if prop == "X-WR-CALNAME":
            title = readable(value)
        elif line == "BEGIN:VEVENT":
            event = {}
            nested = 0
        elif event is not None and prop == "BEGIN":
            nested += 1
        elif event is not None and prop == "END" and value != "VEVENT":
            nested = max(0, nested-1)
        elif line == "END:VEVENT":
            if event and event.get("SUMMARY") and event.get("DTSTART") and event.get("STATUS") != "CANCELLED":
                date = event["DTSTART"]
                try:
                    stamp = datetime.datetime.strptime(date[:8], "%Y%m%d").date().isoformat()
                except ValueError:
                    event = None
                    continue
                if "T" in date:
                    clock = date.partition("T")[2]
                    stamp += " " + clock[:2] + ":" + clock[2:4] + (" UTC" if clock.endswith("Z") else " " + event.get("tz", "local time"))
                events.append(f"{stamp} — {event['SUMMARY']}" + (f" — {event['DESCRIPTION']}" if event.get("DESCRIPTION") else ""))
            event = None
        elif event is not None and not nested and prop in ("DTSTART","SUMMARY","DESCRIPTION","STATUS"):
            event[prop] = readable(value)[:1400]
            if prop == "DTSTART" and "TZID=" in params:
                event["tz"] = params.split("TZID=", 1)[-1].split(";")[0].strip('"')
    return title, "\n".join(dict.fromkeys(events))[:120000]


def pdf_text(data, pages):
    with PDF_SLOT:
        reply = subprocess.run([sys.executable, "-I", str(Path(__file__).resolve()), json.dumps(pages or [])],
                           input=data, capture_output=True, timeout=30)
    if reply.returncode:
        raise ValueError("PDF text extraction failed: " + reply.stderr.decode(errors="replace").strip().split("\n")[-1][:180])
    return json.loads(reply.stdout)


def extract_pdf(page_numbers):
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (22, 25))
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (700 * 1024 * 1024,) * 2)
    wheel = Path(__file__).with_name("vendor") / WHEEL
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != SHA256:
        raise ValueError("PDF reader package digest mismatch")
    sys.path.insert(0, str(wheel))
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(sys.stdin.buffer.read(12_000_001)))
    if reader.is_encrypted:
        raise ValueError("encrypted PDF is outside the public crawl")
    numbers = page_numbers or range(1, min(48, len(reader.pages)) + 1)
    blocks = []
    for n in numbers:
        if 1 <= n <= len(reader.pages):
            raw = reader.pages[n-1].extract_text() or ""
            lines = [" ".join(line.split()) for line in raw.splitlines()]
            paragraphs, current = [], ""
            for line in lines:
                if not line or line.isupper() or len(line) < 16:
                    if current:
                        paragraphs.append(current)
                        current = ""
                    if line:
                        paragraphs.append(line)
                else:
                    current = (current + " " + line).strip()
                    if re.search(r"[.?!]$", line):
                        paragraphs.append(current)
                        current = ""
            if current:
                paragraphs.append(current)
            blocks.append(f"Page {n}\n" + "\n".join(paragraphs))
            if sum(map(len, blocks)) > 118000:
                break
    print(json.dumps({"title": str((reader.metadata or {}).get("/Title") or "Public PDF")[:300],
                      "text": "\n".join(blocks)[:120000], "headings": [f"Page {n}" for n in numbers]}))


if __name__ == "__main__":
    extract_pdf(json.loads(sys.argv[1]))
