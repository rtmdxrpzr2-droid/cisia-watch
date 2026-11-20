import json, os, sys
import requests
from bs4 import BeautifulSoup

URL = "https://testcisia.it/calendario.php?tolc=cents&l=gb&lingua=inglese"
LINK = "https://testcisia.it/studenti_tolc/login_sso.php"
STATE = "state.json"

# Universite filtresi yok: tum CENT@UNI satirlari icin bildirim gelir.
WATCH_UNIVERSITIES = []
ONLY_UNI_FORMAT = True  # sadece CENT@UNI (yuz yuze) olanlar

session = requests.Session()
session.headers["User-Agent"] = "Mozilla/5.0"


def fetch_rows():
    resp = session.get(URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = {}
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 8:
            continue
        fmt, uni, region, city, deadline, seats, status, date = [
            td.get_text(" ", strip=True) for td in tds[:8]
        ]
        status = status.upper()
        available = "AVAILABLE SEATS" in status and "NOT LONGER" not in status
        r = {
            "fmt": fmt, "uni": uni, "city": city, "date": date,
            "deadline": deadline, "available": available,
            # Koltuk sayisi sadece basvuru acikken anlamli. Kapaliyken
            # saklamiyoruz; boylece anlamsiz dalgalanmalar commit uretmiyor.
            "seats": seats if available else None,
        }
        if wanted(r):
            rows[f"{fmt}|{uni}|{date}"] = r
    return rows


def wanted(r):
    if ONLY_UNI_FORMAT and "UNI" not in r["fmt"].upper():
        return False
    if WATCH_UNIVERSITIES:
        name = (r["uni"] + " " + r["city"]).lower()
        return any(w.lower() in name for w in WATCH_UNIVERSITIES)
    return True


def notify(text):
    resp = session.post(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendMessage",
        data={
            "chat_id": os.environ["TELEGRAM_CHAT_ID"],
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    resp.raise_for_status()


def to_int(s):
    try:
        return int(str(s).strip())
    except ValueError:
        return None


def describe(r):
    return (
        f"{r['uni']} ({r['city']}) - {r['fmt']}\n"
        f"Sinav: {r['date']} | Son basvuru: {r['deadline']}"
    )


def message_for(r, old):
    if old is None:
        if r["available"]:
            return f"YENI BASVURU ACILDI! Koltuk: {r['seats']}\n{describe(r)}"
        return f"Yeni tarih eklendi (henuz koltuk yok)\n{describe(r)}"
    if r["available"] and not old["available"]:
        return f"KOLTUK ACILDI (iptal olmus olabilir)! Koltuk: {r['seats']}\n{describe(r)}"
    if r["available"] and old["available"]:
        n, o = to_int(r["seats"]), to_int(old["seats"])
        if n is not None and o is not None and n > o:
            return f"Koltuk sayisi artti: {o} -> {n} (iptal olmus olabilir)\n{describe(r)}"
    return None


def main():
    for var in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
        if not os.environ.get(var):
            sys.exit(f"Missing {var}")

    try:
        rows = fetch_rows()
    except Exception as e:
        print("fetch failed:", e)
        return  # gecici hata, state'e dokunma

    if not rows:
        print("no rows parsed, skipping")
        return

    first_run = not os.path.exists(STATE)
    prev = {}
    if not first_run:
        with open(STATE, encoding="utf-8") as f:
            prev = json.load(f)

    new_state = dict(rows)
    if not first_run:
        for key, r in rows.items():
            msg = message_for(r, prev.get(key))
            if not msg:
                continue
            try:
                notify(msg + "\n" + LINK)
            except Exception as e:
                # Bildirim gitmediyse eski durumu koru: sonraki calismada tekrar denenir.
                print("notify failed:", key, e)
                if key in prev:
                    new_state[key] = prev[key]
                else:
                    del new_state[key]

    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(new_state, f, indent=1, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    print("ok,", len(rows), "rows")


if __name__ == "__main__":
    main()
