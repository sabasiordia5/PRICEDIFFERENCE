"""
Runs on GitHub (GitHub Actions) every time a new price file is uploaded to
data/snapshots/. It:
  1. rebuilds data/history.json  - every product with its price history
  2. compares the newest file with the previous one and records changes
     in data/changelog.json
  3. e-mails the changes (if the e-mail secrets are set in the repository)
"""

import json
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAP_DIR = ROOT / "data" / "snapshots"
HISTORY = ROOT / "data" / "history.json"
CHANGELOG = ROOT / "data" / "changelog.json"
STATE = ROOT / "data" / "state.json"


def snapshot_time(path):
    # tegeta_prices_2026-11-16_10-01-23.json -> 2026-11-16T10:01:23
    stamp = path.stem.replace("tegeta_prices_", "")
    d, t = stamp.split("_")
    return f"{d}T{t.replace('-', ':')}"


def load_snapshots():
    snaps = []
    for f in sorted(SNAP_DIR.glob("tegeta_prices_*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"skip {f.name}: {e}")
            continue
        products = {}
        for p in data.get("products", []):
            if not p.get("price"):
                continue
            key = p.get("url") or p["name"]
            products[key] = {"name": p["name"], "url": p.get("url", ""),
                             "price": float(p["price"]),
                             "old_price": float(p["old_price"]) if p.get("old_price") else None}
        if products:
            snaps.append({"file": f.name, "at": snapshot_time(f), "products": products})
    return snaps


def build_history(snaps):
    items = {}
    for s in snaps:
        for key, p in s["products"].items():
            it = items.setdefault(key, {"name": p["name"], "url": p["url"], "history": []})
            it["name"] = p["name"]
            h = it["history"]
            point = [s["at"], p["price"], p["old_price"]]
            # keep one point per day (the latest)
            if h and h[-1][0][:10] == s["at"][:10]:
                h[-1] = point
            else:
                h.append(point)
    return {"updated": snaps[-1]["at"] if snaps else None,
            "products": list(items.values())}


def diff(prev, cur):
    changes = []
    for key, p in cur["products"].items():
        old = prev["products"].get(key)
        if old is None:
            changes.append({"type": "new", "name": p["name"], "url": p["url"], "price": p["price"]})
        elif abs(old["price"] - p["price"]) > 0.001:
            changes.append({"type": "price", "name": p["name"], "url": p["url"],
                            "from": old["price"], "to": p["price"]})
    # if the new file has far fewer products, the save was probably incomplete:
    # don't report the missing ones as removed
    if len(cur["products"]) < 0.8 * len(prev["products"]):
        return changes
    for key, p in prev["products"].items():
        if key not in cur["products"]:
            changes.append({"type": "removed", "name": p["name"], "url": p["url"], "price": p["price"]})
    return changes


def describe(c):
    if c["type"] == "price":
        delta = c["to"] - c["from"]
        arrow = "▼ გაიაფდა" if delta < 0 else "▲ გაძვირდა"
        return f'{c["name"]}: {c["from"]:.2f} → {c["to"]:.2f} ₾ ({delta:+.2f}) {arrow}'
    if c["type"] == "new":
        return f'{c["name"]}: ახალი პროდუქტი, {c["price"]:.2f} ₾'
    return f'{c["name"]}: აღარ ჩანს საიტზე (იყო {c["price"]:.2f} ₾)'


def send_email(entry):
    user, pwd, to = (os.environ.get(k, "").strip() for k in ("SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"))
    if not (user and pwd and to):
        print("E-mail secrets not set - skipping e-mail.")
        return
    host = os.environ.get("SMTP_HOST", "").strip() or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT", "").strip() or 465)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    site = ""
    if "/" in repo:
        owner, name = repo.split("/", 1)
        site = f"https://{owner.lower()}.github.io/{name}/"

    lines = [describe(c) for c in entry["changes"]]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f'Tegeta: {len(lines)} ფასის ცვლილება ({entry["at"][:10]})'
    msg["From"] = user
    msg["To"] = to
    text = "\n".join(lines) + (f"\n\nგვერდი: {site}" if site else "")
    rows = "".join(f"<li style='margin:4px 0'>{l}</li>" for l in lines)
    html = (f"<div style='font-family:Arial,sans-serif;font-size:15px'>"
            f"<p>Tegeta-ზე ფასები შეიცვალა ({entry['at'].replace('T', ' ')[:16]}):</p><ul>{rows}</ul>"
            + (f"<p><a href='{site}'>გახსენი ფასების გვერდი</a></p>" if site else "") + "</div>")
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as s:
        s.login(user, pwd)
        s.sendmail(user, [a.strip() for a in to.split(",") if a.strip()], msg.as_string())
    print(f"E-mail sent to {to}")


def main():
    snaps = load_snapshots()
    HISTORY.write_text(json.dumps(build_history(snaps), ensure_ascii=False), encoding="utf-8")
    print(f"history.json: {len(snaps)} snapshots")

    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    changelog = json.loads(CHANGELOG.read_text(encoding="utf-8")) if CHANGELOG.exists() else []

    if len(snaps) >= 2 and state.get("last_compared") != snaps[-1]["file"]:
        changes = diff(snaps[-2], snaps[-1])
        if changes:
            entry = {"at": snaps[-1]["at"], "changes": changes}
            changelog.insert(0, entry)
            changelog = changelog[:200]
            print(f"{len(changes)} changes found")
            try:
                send_email(entry)
            except Exception as e:
                print(f"E-mail failed: {e}")
        else:
            print("No changes since the previous snapshot")
    state["last_compared"] = snaps[-1]["file"] if snaps else None

    CHANGELOG.write_text(json.dumps(changelog, ensure_ascii=False, indent=1), encoding="utf-8")
    STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
