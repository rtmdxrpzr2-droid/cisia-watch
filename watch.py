import json, os, sys
import requests
from bs4 import BeautifulSoup

URL = "https://testcisia.it/calendario.php?tolc=cents&l=gb&lingua=inglese"
STATE = "state.json"

# Universite filtresi yok: tum CENT@UNI satirlari icin bildirim gelir.
WATCH_UNIVERSITIES = []
ONLY_UNI_FORMAT = True  # sadece CENT@UNI (yuz yuze) olanlar


def fetch_rows():
    html = requests.get(URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"}).text
    soup = BeautifulSoup(html, "html.parser")
    rows = {}
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 8:
            continue
        fmt, uni, region, city, deadline, seats, status, date = [
            td.get_text(" ", strip=True) for td in tds[:8]
        ]
        available = "AVAILABLE SEATS" in status.upper() and "NOT LONGER" not in status.upper()
        key = f"{fmt}|{uni}|{date}"
        rows[key] = {
            "fmt": fmt, "uni": uni, "city": city, "date": date,
            "deadline": deadline, "seats": seats, "available": available,
        }
    return rows


def wanted(r):
    if ONLY_UNI_FORMAT and "UNI" not in r["fmt"].upper():
        return False
    if WATCH_UNIVERSITIES:
        name = (r["uni"] + " " + r["city"]).lower()
        return any(w in name for w in WATCH_UNIVERSITIES)
    return True


def notify(text):
    token = os.environ["TELEGRAM_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat, "text": text, "disable_web_page_preview": True},
        timeout=30,
    )


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


LINK = "https://testcisia.it/studenti_tolc/login_sso.php"


def main():
    try:
        rows = fetch_rows()
    except Exception as e:
        print("fetch failed:", e)
        sys.exit(0)  # gecici hata, state'e dokunma

    if not rows:
        print("no rows parsed, skipping")
        sys.exit(0)

    first_run = not os.path.exists(STATE)
    prev = {} if first_run else json.load(open(STATE))

    if not first_run:
        for key, r in rows.items():
            if not wanted(r):
                continue
            old = prev.get(key)
            msg = None

            if old is None:
                if r["available"]:
                    msg = f"YENI BASVURU ACILDI! Koltuk: {r['seats']}\n{describe(r)}"
                else:
                    msg = f"Yeni tarih eklendi (henuz koltuk yok)\n{describe(r)}"
            elif r["available"] and not old["available"]:
                msg = f"KOLTUK ACILDI (iptal olmus olabilir)! Koltuk: {r['seats']}\n{describe(r)}"
            elif r["available"] and old["available"]:
                n, o = to_int(r["seats"]), to_int(old["seats"])
                if n is not None and o is not None and n > o:
                    msg = f"Koltuk sayisi artti: {o} -> {n} (iptal olmus olabilir)\n{describe(r)}"

            if msg:
                notify(msg + "\n" + LINK)

    json.dump(rows, open(STATE, "w"), indent=1, ensure_ascii=False)
    print("ok,", len(rows), "rows")


if __name__ == "__main__":
    main()
