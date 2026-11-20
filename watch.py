import html, json, os, re, sys, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

URL = "https://testcisia.it/calendario.php?tolc=cents&l=gb&lingua=inglese"
LINK = "https://testcisia.it/studenti_tolc/login_sso.php"
STATE = "state.json"
ROME = ZoneInfo("Europe/Rome")

# Universite filtresi yok: tum CENT@UNI satirlari icin bildirim gelir.
WATCH_UNIVERSITIES = []
ONLY_UNI_FORMAT = True  # sadece CENT@UNI (yuz yuze) olanlar

# "Koltuk sayisi artti" sadece sinav neredeyse doluyken (bu kadar veya daha az
# koltuk kalmisken) bildirilir; bol koltuk varken artislar gurultu yapar.
NEAR_FULL = 5
# Acik bir sinavda koltuk bu sayiya dusunce bir kez "SON N KOLTUK" uyarisi gider.
# Koltuk LOW_RESET'in ustune cikarsa uyari yeniden kurulur (10<->11 gidip
# gelmesi tekrar tekrar mesaj atmasin diye).
LOW_SEATS = 10
LOW_RESET = 15
# Acik bir sinavin son basvurusuna bu kadar gun kala bir kez hatirlatma gider.
REMIND_DAYS = 2

# Her calisma LOOP_SECONDS boyunca acik kalir ve siteye INTERVAL saniyede bir bakar.
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "0"))
INTERVAL = int(os.environ.get("INTERVAL", "30"))

# Site bu kadar dakika boyunca okunamazsa uyari gonderilir.
ALERT_AFTER = timedelta(minutes=15)
# Sayfa hic anlasilamiyorsa (tablo var ama hicbir satir okunamiyor) daha hizli
# uyar; tek seferlik bir aksaklik yuzunden uyarmamak icin ~1 dk bekle.
SITE_CHANGE_ALERT_AFTER = timedelta(minutes=1)

# Sutunlar basliklarina gore bulunur; boylece CISIA yeni sutun eklese ya da
# sirayi degistirse de takip devam eder. Basliklar bulunamazsa bugunku sutun
# sirasi kullanilir (tarih sutunlari tarih gibi gorunuyorsa).
COLUMNS = {"fmt": "FORMAT", "uni": "UNIVERSITY", "city": "CITY",
           "deadline": "BOOKINGS DEADLINE", "seats": "SEATS", "status": "STATE", "date": "DATE"}
DEFAULT_POSITIONS = {"fmt": 0, "uni": 1, "city": 3, "deadline": 4, "seats": 5, "status": 6, "date": 7}
DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
# Botun tanidigi durum yazilari ve sinav turleri. Bilinmeyen bir sey gorulurse
# sadece o sinav etkilenir, diger sinavlarin takibi surer; bir kez bilgi verilir.
KNOWN_FORMATS = {"CENT@UNI", "CENT@HOME"}
DAILY_HOUR = 9  # gunluk "sistem aktif" ozeti, Roma saatiyle

session = requests.Session()
session.headers["User-Agent"] = "Mozilla/5.0"


class ParseError(Exception):
    pass


class SiteChanged(Exception):
    """Tablo var ama hicbir satir anlasilamiyor; takip yapilamaz."""


class Rows(dict):
    """fetch_rows sonucu: satirlar + kullaniciya bir kez bildirilecek uyarilar.
    partial=True: bazi satirlar okunamadi; gorunmeyen sinavlar silinmemeli."""
    warnings = ()
    partial = False


def now():
    return datetime.now(ROME)


def norm(text):
    return " ".join(str(text).upper().split())


def column_map(soup):
    """Sutun konumlarini basliklardan bulur. (konumlar, uyari) dondurur."""
    for tr in soup.find_all("tr"):
        ths = [norm(th.get_text(" ", strip=True)) for th in tr.find_all("th")]
        if "FORMAT" in ths or "DATE" in ths:
            if all(name in ths for name in COLUMNS.values()):
                return {k: ths.index(name) for k, name in COLUMNS.items()}, None
            missing = [n for n in COLUMNS.values() if n not in ths]
            return DEFAULT_POSITIONS, ("Tablo başlıkları değişmiş (" + ", ".join(missing) +
                                       " bulunamadı); sütun sırasına göre okumaya devam ediyorum.")
    return DEFAULT_POSITIONS, "Tablo başlıkları bulunamadı; sütun sırasına göre okumaya devam ediyorum."


# Bilinen durum yazilari -> (acik_mi, kapanma_nedeni). Site Italyancaya donerse
# diye Italyanca karsiliklari da taniniyor.
STATUS_MAP = {
    "AVAILABLE SEATS": (True, None),
    "NOT LONGER AVAILABLE": (False, "full"),
    "BOOKINGS CLOSED": (False, "closed"),
    "POSTI DISPONIBILI": (True, None),
    "POSTI ESAURITI": (False, "full"),
    "ISCRIZIONI CHIUSE": (False, "closed"),
}


def classify(status):
    """Durum yazisindan (acik_mi, kapanma_nedeni, taninan_mi) cikarir."""
    if status in STATUS_MAP:
        return (*STATUS_MAP[status], True)
    # Bilinmeyen yazi: "acik" gibi gorunuyorsa acik say (kacirmaktansa fazladan
    # haber vermek iyi); degilse karar verme, bu sinavin son durumunu koru.
    negative = any(w in status for w in ("NOT", "NO LONGER", "UNAVAILABLE", "ESAURIT", "CHIUS", "NON "))
    if ("AVAILABLE" in status or "DISPONIBIL" in status) and not negative:
        return True, None, False
    return None, None, False


def fetch_rows():
    resp = session.get(URL, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    cols, header_warning = column_map(soup)
    need = max(cols.values()) + 1

    rows, warnings = Rows(), []
    new_formats, new_statuses = {}, {}
    total = good = bad = 0
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < need:
            continue
        total += 1
        cell = {k: tds[i].get_text(" ", strip=True) for k, i in cols.items()}
        if not (DATE_RE.match(cell["date"]) and DATE_RE.match(cell["deadline"])):
            bad += 1  # bu satir beklenen bicimde degil (tarih sutunu tarih degil)
            continue
        good += 1
        status, fmt = norm(cell["status"]), cell["fmt"]
        available, closed, known = classify(status)
        r = {
            "fmt": fmt, "uni": cell["uni"], "city": cell["city"], "date": cell["date"],
            "deadline": cell["deadline"], "available": bool(available),
            # Kapaliysa neden: koltuklar doldu mu, basvuru suresi mi bitti?
            "closed": None if available else closed,
            # Koltuk sayisi sadece basvuru acikken anlamli. Kapaliyken
            # saklamiyoruz; boylece anlamsiz dalgalanmalar commit uretmiyor.
            "seats": cell["seats"] if available else None,
        }
        where = f"{r['city'].title()} {r['date'][:5]}"
        if norm(fmt) not in KNOWN_FORMATS:
            # Takip edilmese bile bildir: CISIA "CENT@UNI" adini degistirirse fark edelim.
            new_formats.setdefault(fmt, wanted(r))
        if not wanted(r):
            continue
        if not known:
            decision = "son durumunu koruyorum" if available is None else "açık kabul ettim"
            new_statuses.setdefault((status, decision), []).append(where)
            if available is None:
                r["_frozen"] = True  # karar verilemedi: bu sinavin son durumu korunur
        rows[f"{fmt}|{r['uni']}|{r['date']}"] = r

    if total == 0:
        # Sayfada hic sinav satiri yok: bakim/hata sayfasi ya da tablo kaldirilmis.
        raise ParseError("sayfada sinav tablosu bulunamadi")
    if good == 0:
        # Tablo var ama hicbir satir anlasilamiyor: takip yapilamaz.
        raise SiteChanged("Sınav tablosu okunamıyor: hiçbir satırda tarih sütunları beklenen yerde değil.")
    if header_warning:
        warnings.append(header_warning)
    for fmt, tracked in new_formats.items():
        warnings.append(f'Yeni sınav türü "{fmt}"' + (" — takip ediyorum" if tracked else " — takip ETMİYORUM"))
    for (status, decision), wheres in new_statuses.items():
        more = f" +{len(wheres) - 4}" if len(wheres) > 4 else ""
        warnings.append(f'Yeni durum yazısı "{status}" ({", ".join(wheres[:4])}{more}) — {decision}')
    if bad:
        warnings.append(f"{bad} satır okunamadı (beklenmeyen biçim), onları atladım.")
    rows.warnings = tuple(warnings)
    rows.partial = bad > 0
    return rows


def wanted(r):
    if ONLY_UNI_FORMAT and "UNI" not in r["fmt"].upper():
        return False
    if WATCH_UNIVERSITIES:
        name = (r["uni"] + " " + r["city"]).lower()
        return any(w.lower() in name for w in WATCH_UNIVERSITIES)
    return True


def notify(text, button=True):
    payload = {
        "chat_id": os.environ["TELEGRAM_CHAT_ID"],
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if button:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": "🎟 Hemen Başvur", "url": LINK}]]
        }
    resp = session.post(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendMessage",
        json=payload,
        timeout=20,
    )
    resp.raise_for_status()


def try_notify(text, button=True):
    try:
        notify(text, button)
        return True
    except Exception as e:
        print("notify failed:", e)
        return False


def to_int(s):
    try:
        return int(str(s).strip())
    except ValueError:
        return None


AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
         "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
GUNLER = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]


def parse_date(date_str):
    try:
        return datetime.strptime(str(date_str).strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def fmt_date(date_str, year=True, weekday=False):
    """'17/12/2026' -> '17 Aralık 2026, Perşembe' (okunamazsa oldugu gibi)."""
    d = parse_date(date_str)
    if d is None:
        return esc(date_str)
    out = f"{d.day} {AYLAR[d.month - 1]}"
    if year:
        out += f" {d.year}"
    if weekday:
        out += f", {GUNLER[d.weekday()]}"
    return out


def days_until(date_str):
    d = parse_date(date_str)
    return None if d is None else (d - now().date()).days


def days_left(date_str):
    n = days_until(date_str)
    if n is None or n < 0:
        return ""
    if n == 0:
        return " (⚠️ bugün son gün!)"
    return f" ({n} gün kaldı)"


def esc(s):
    return html.escape(str(s), quote=False)


def uni_name(name):
    # CISIA "Università" yerine "Universita'" yaziyor.
    return name.replace("Universita'", "Università")


def city_name(r):
    return r["city"].title()


def card(title, r, extra_lines=(), footer=None, button=True):
    """(mesaj, buton_var_mi) dondurur. Baslikta sehir ve sinav tarihi de yazar,
    boylece kilit ekranindaki bildirimde hangi sinav oldugu hemen gorunur."""
    lines = [
        f"<b>{title} — {esc(city_name(r))} · {fmt_date(r['date'], year=False)}</b>",
        "",
        f"🏛 <b>{esc(uni_name(r['uni']))}</b>",
        f"📍 {esc(city_name(r))} · {esc(r['fmt'])}",
        f"📅 Sınav: <b>{fmt_date(r['date'], weekday=True)}</b>",
        f"⏳ Son başvuru: {fmt_date(r['deadline'], weekday=True)}{days_left(r['deadline'])}",
        *extra_lines,
    ]
    if footer:
        lines += ["", f"<i>{footer}</i>"]
    return "\n".join(lines), button


def message_for(r, old):
    """Satir icin gonderilecek (mesaj, buton) ikilisini dondurur (yoksa None).
    r["low_alerted"] bayragini da gunceller: "son koltuklar" uyarisi bir kez gitsin diye."""
    if old and old.get("deadline_reminded"):
        r["deadline_reminded"] = True
    # Bu sinavda basvuru daha once hic acik goruldu mu? Ilk acilis ile
    # dolduktan sonra iptalle yer acilmasini ayirt etmek icin.
    if r["available"] or (old and (old.get("seen_open") or old["available"])):
        r["seen_open"] = True

    if not r["available"]:
        if old is None:
            return card("🆕 YENİ SINAV TARİHİ", r, ["🔒 Şu an başvuruya kapalı"],
                        "Başvuru açılınca hemen haber vereceğim.", button=False)
        if old["available"]:
            if r.get("closed") == "full":
                return card("❌ DOLDU", r, ["💺 Boş koltuk kalmadı"],
                            "İptal olup yer açılırsa hemen haber vereceğim.", button=False)
            return card("🔒 BAŞVURULAR KAPANDI", r, [],
                        "Bu sınavın başvuru dönemi sona erdi.", button=False)
        return None

    n = to_int(r["seats"])
    low = n is not None and 0 < n <= LOW_SEATS
    if old and old["available"] and old.get("low_alerted") and n is not None and n <= LOW_RESET:
        r["low_alerted"] = True  # daha once uyarildi, koltuk yeterince artmadi
    already_low = r.get("low_alerted", False)
    if low:
        r["low_alerted"] = True

    shown_seats = r["seats"] if to_int(r["seats"]) is not None else "belirtilmemiş"
    seats = f"💺 Boş koltuk: <b>{esc(shown_seats)}</b>"
    hurry = [f"🔥 <b>Son {n} koltuk!</b>"] if low else []

    left = days_until(r["deadline"])
    in_remind_window = left is not None and 0 <= left <= REMIND_DAYS
    if in_remind_window and (old is None or not old["available"]):
        # Acilis mesaji zaten kalan gunu gosteriyor; ayrica hatirlatma gonderme.
        r["deadline_reminded"] = True

    if old is None:
        return card("🚨 YENİ BAŞVURU AÇILDI", r, [seats, *hurry],
                    "Koltuklar hızlı doluyor, acele et!")
    if not old["available"]:
        if not old.get("seen_open"):
            return card("🚨 BAŞVURULAR AÇILDI", r, [seats, *hurry],
                        "Koltuklar hızlı doluyor, acele et!")
        return card("🔔 KOLTUK AÇILDI", r, [seats, *hurry],
                    "Bir iptal olmuş olabilir, hızlı ol!")

    o = to_int(old["seats"])
    if n is not None and o is not None and n > o and o <= NEAR_FULL:
        return card("📈 KOLTUK ARTTI", r,
                    [f"💺 Boş koltuk: {o} → <b>{n}</b>", *hurry],
                    "Dolmak üzereyken bir iptal oldu, hızlı ol!")
    if low and not already_low:
        return card(f"⏰ SON {n} KOLTUK", r, [seats],
                    "Bu sınav dolmak üzere. Başvurmayı düşünüyorsan şimdi tam zamanı!")

    # Son basvuruya REMIND_DAYS gun kala, sabah saatinden sonra bir kez hatirlat.
    if in_remind_window and not r.get("deadline_reminded") and now().hour >= DAILY_HOUR:
        r["deadline_reminded"] = True
        title = "⏳ BUGÜN SON GÜN" if left == 0 else f"⏳ SON {left} GÜN"
        return card(title, r, [seats, *hurry],
                    "Başvurmayı düşünüyorsan erteleme.")
    return None


# ---------- state ----------

def load_state():
    if not os.path.exists(STATE):
        return None
    with open(STATE, encoding="utf-8") as f:
        data = json.load(f)
    if "rows" not in data:  # eski format: sadece satirlar
        data = {"rows": data}
    data.setdefault("health", {})
    return data


def save_state(state):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, ensure_ascii=False, sort_keys=True)
        f.write("\n")


# ---------- one check ----------

def check(state):
    """Siteyi bir kez kontrol eder, state'i gunceller."""
    health = state["health"]
    try:
        rows = fetch_rows()
    except Exception as e:
        print("fetch failed:", e)
        record_failure(health, e)
        return

    record_success(health)
    report_warnings(health, getattr(rows, "warnings", ()))
    prev = state["rows"]
    new_rows = {}
    for key, r in rows.items():
        if r.pop("_frozen", False):
            # Durumu anlasilamayan sinav: son bilinen hali korunur, mesaj gitmez.
            if key in prev:
                new_rows[key] = prev[key]
            else:
                r["available"], r["closed"], r["seats"] = False, None, None
                new_rows[key] = r
            continue
        new_rows[key] = r
        try:
            out = message_for(r, prev.get(key))
        except Exception as e:
            # Beklenmedik bir satir tum takibi durdurmasin; bu satiri sonra tekrar dene.
            print("row error:", key, repr(e))
            out = None
            if key in prev:
                new_rows[key] = prev[key]
            else:
                del new_rows[key]
            continue
        if out and not try_notify(*out):
            # Bildirim gitmediyse eski durumu koru: sonraki kontrolde tekrar denenir.
            if key in prev:
                new_rows[key] = prev[key]
            else:
                del new_rows[key]
    if getattr(rows, "partial", False):
        # Bazi satirlar okunamadi: gorunmeyen sinavlari silme (sonra "yeni" sanilmasin).
        for key, old in prev.items():
            new_rows.setdefault(key, old)
    state["rows"] = new_rows
    print(now().strftime("%H:%M:%S"), "ok,", len(rows), "rows")


def report_warnings(health, warnings):
    """Sayfadaki yeni/tanimadik seyleri bir kez bildirir; ayni uyarilar tekrar gitmez."""
    current = sorted(set(warnings))
    if current == health.get("warnings", []):
        return
    if current:
        lines = ["<b>⚠️ CISIA sayfasında yeni bir şey var</b>", ""]
        lines += [f"• {esc(w)}" for w in current[:8]]
        lines += ["", "<i>Diğer sınavların takibi normal devam ediyor. "
                      "Etkilenen sınavı siteden de kontrol et; botun güncellenmesi gerekebilir.</i>"]
        if not try_notify("\n".join(lines), button=False):
            return
        health["warnings"] = current
    else:
        health.pop("warnings", None)


def record_failure(health, err):
    if not health.get("fail_since"):
        health["fail_since"] = now().isoformat(timespec="seconds")
    since = datetime.fromisoformat(health["fail_since"])
    site_changed = isinstance(err, SiteChanged)
    wait = SITE_CHANGE_ALERT_AFTER if site_changed else ALERT_AFTER
    if health.get("alerted") or now() - since < wait:
        return
    if site_changed:
        text = ("<b>⚠️ CISIA sayfası değişmiş olabilir</b>\n\n"
                f"{esc(str(err)[:300])}\n\n"
                "<i>Sayfadaki hiçbir sınavı okuyamıyorum, takip duruyor. "
                "Siteyi kendin kontrol et; botun güncellenmesi gerekiyor. Düzelince haber vereceğim.</i>")
    else:
        mins = int((now() - since).total_seconds() // 60)
        text = ("<b>⚠️ Takip şu an çalışmıyor</b>\n\n"
                f"CISIA sayfası {mins} dakikadır okunamıyor.\n"
                f"Hata: <code>{esc(str(err)[:200])}</code>\n\n"
                "<i>Bu sürede yeni başvuruları kaçırabilirim, siteyi kendin de kontrol et. "
                "Düzelince haber vereceğim.</i>")
    if try_notify(text, button=False):
        health["alerted"] = True


def record_success(health):
    if health.get("alerted"):
        since = datetime.fromisoformat(health["fail_since"])
        mins = int((now() - since).total_seconds() // 60)
        try_notify(
            f"<b>✅ Takip tekrar çalışıyor</b>\n\n<i>Yaklaşık {mins} dakika kesinti oldu.</i>",
            button=False,
        )
    health.pop("fail_since", None)
    health.pop("alerted", None)


def summary_lines(rows_by_key):
    rows = sorted(rows_by_key.values(),
                  key=lambda r: (not r["available"], parse_date(r["date"]) or now().date()))
    lines = ["<b>☀️ Günaydın — takip aktif ✅</b>", ""]
    if rows:
        lines.append(f"Takip edilen {len(rows)} sınav:")
        for r in rows:
            state_txt = f"{esc(r['seats'])} koltuk" if r["available"] else "kapalı"
            dot = "🟢" if r["available"] else "🔴"
            lines.append(f"• {dot} {esc(city_name(r))} · {fmt_date(r['date'])} — {state_txt}")
    else:
        lines.append("Şu an listede CENT@UNI sınavı yok.")
    lines += ["", "<i>Siteye 30 saniyede bir bakıyorum. Yer açılınca anında haber vereceğim.</i>"]
    return lines


def daily_summary(state):
    """Her sabah bir kez: sistemin calistigini ve takip edilen sinavlari bildirir.
    Bu mesaj gelmezse sistem hic calismiyor demektir."""
    today = now().date().isoformat()
    health = state["health"]
    if now().hour < DAILY_HOUR or health.get("last_daily") == today:
        return
    lines = summary_lines(state["rows"])
    if try_notify("\n".join(lines), button=False):
        health["last_daily"] = today


# ---------- main ----------

def main():
    for var in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
        if not os.environ.get(var):
            sys.exit(f"Missing {var}")

    state = load_state()
    if state is None:
        # Ilk calisma: bildirim gondermeden mevcut durumu kaydet.
        state = {"rows": fetch_rows(), "health": {}}
        save_state(state)
        print("initialized state")
        return

    deadline = time.monotonic() + LOOP_SECONDS
    while True:
        started = time.monotonic()
        try:
            check(state)
        except Exception as e:
            # Hicbir hata dongunun tamamini cokertmesin; kesinti olarak say.
            print("check crashed:", repr(e))
            record_failure(state["health"], e)
        if not state["health"].get("fail_since"):
            try:
                daily_summary(state)
            except Exception as e:
                print("daily summary failed:", repr(e))
        save_state(state)
        if started + INTERVAL > deadline:
            break
        time.sleep(max(0, started + INTERVAL - time.monotonic()))


if __name__ == "__main__":
    main()
