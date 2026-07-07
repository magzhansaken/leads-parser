#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TenderView — Парсер протоколов goszakup для GitHub Actions.
Параметры берёт из переменных окружения (месяц, год, группа).
Защита от обрыва: сам остановится и сохранит за 20 мин до лимита 6ч.

Env-переменные (задаёт workflow):
  P_YEAR, P_MONTH, P_AMOUNT_FROM, P_AMOUNT_TO, P_GRP, P_OUT
"""
import re, io, os, sys, csv, time

try:
    import requests, pypdf
except ImportError:
    print("Установи: pip install requests pypdf"); sys.exit(1)

# --- параметры из окружения (с запасными значениями для локального теста) ---
YEAR        = int(os.environ.get("P_YEAR", "2026"))
MONTH       = int(os.environ.get("P_MONTH", "6"))
AMOUNT_FROM = int(os.environ.get("P_AMOUNT_FROM", "150000"))
_at         = os.environ.get("P_AMOUNT_TO", "500000")
AMOUNT_TO   = int(_at) if _at and _at.lower() != "none" else None
GRP         = os.environ.get("P_GRP", "150-500т")
OUT_CSV     = os.environ.get("P_OUT", f"leads_{YEAR}_{MONTH:02d}.csv")

# --- защита по времени ---
TIME_BUDGET = int(os.environ.get("P_TIME_BUDGET", str(5 * 3600 + 40 * 60)))  # 5ч40м
START_TS    = time.time()

PAUSE    = 1.0
PER_PAGE = 50
BASE     = "https://goszakup.gov.kz"

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
})

def time_left():
    return TIME_BUDGET - (time.time() - START_TS)

def get(url, timeout=40):
    for _ in range(3):
        try:
            r = session.get(url, timeout=timeout)
            time.sleep(PAUSE)
            if r.status_code == 200:
                return r
        except Exception:
            time.sleep(PAUSE * 2)
    return None

def search_url(page):
    amt_to = str(AMOUNT_TO) if AMOUNT_TO else ""
    return (f"{BASE}/ru/search/lots?filter%5Bmethod%5D%5B%5D=3"
            f"&filter%5Bstatus%5D%5B%5D=360&filter%5Bamount_from%5D={AMOUNT_FROM}"
            f"&filter%5Bamount_to%5D={amt_to}&filter%5Btrade_type%5D=g"
            f"&filter%5Bmonth%5D={MONTH}&filter%5Byear%5D={YEAR}"
            f"&count_record={PER_PAGE}&page={page}")

def parse_pdf(pdf_bytes, aid):
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        full = "\n".join(p.extract_text() for p in reader.pages)
    except Exception:
        return []
    ru = full[full.find("Протокол об итогах"):] if "Протокол об итогах" in full else full
    rows = []
    for block in re.split(r"Лот № ", ru)[1:]:
        lot_no = block.split("\n")[0].strip()
        nm = re.search(r"Наименование лота\s+(.+)", block)
        tovar = nm.group(1).strip() if nm else ""
        pm = re.search(r"Запланированная сумма, тенге\s+(\d+)", block)
        plan = pm.group(1) if pm else ""
        cm = re.search(r"Наименование заказчика\s+(.+)", block)
        cust = cm.group(1).strip() if cm else ""
        wm = re.search(r"Определить победителем по лоту[^:]*:\s*(\d{12})", block)
        wbin = wm.group(1) if wm else ""
        for p in re.finditer(r"\n\s*(\d+)\s+(.+?)\s+(\d{12})\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d{4}-\d{2}-\d{2})", block):
            _, pn, pb, pu, _s, tot, pd = p.groups()
            rows.append({
                "БИН": pb, "поставщик": pn.strip(), "товар": tovar,
                "цена_тендера": plan, "ставка": tot,
                "победитель": "да" if pb == wbin else "",
                "заказчик": cust, "лот": lot_no, "дата": pd[:10],
                "группа": GRP, "год": YEAR, "месяц": MONTH, "announce_id": aid,
            })
    return rows

def count_totals():
    r = get(search_url(1))
    if not r:
        return None, None
    m = re.search(r"из\s+(\d+)\s+запис", r.text)
    total = int(m.group(1)) if m else None
    if total is not None:
        pages = (total + PER_PAGE - 1) // PER_PAGE
    else:
        pgs = [int(x) for x in re.findall(r"[?&]page=(\d+)", r.text)]
        pages = max(pgs) if pgs else None
    return total, pages

def save(rows, reason):
    cols = ["БИН","поставщик","товар","цена_тендера","ставка","победитель",
            "заказчик","лот","дата","группа","год","месяц","announce_id"]
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader(); w.writerows(rows)
    dt = int(time.time() - START_TS)
    print("\n" + "=" * 54)
    print(f" СОХРАНЕНО ({reason})")
    print(f" Время: {dt//60} мин | Записей: {len(rows)} | "
          f"Поставщиков: {len(set(r['БИН'] for r in rows))}")
    print(f" Файл: {OUT_CSV}")
    print("=" * 54)

def main():
    print("=" * 54)
    print(f" {GRP} · {MONTH}/{YEAR}")
    print(f" Бюджет времени: {TIME_BUDGET//3600}ч{(TIME_BUDGET%3600)//60}м")
    print("=" * 54)
    total, pages = count_totals()
    if total is not None:
        print(f" Всего лотов: {total} | Страниц: {pages}")
    print("-" * 54)

    all_rows, seen, done = [], set(), 0
    page = 1
    stopped_early = False
    while True:
        # проверка времени перед каждой страницей
        if time_left() < 60:
            print(f"\n⏰ Время на исходе — останавливаюсь безопасно.")
            stopped_early = True
            break
        r = get(search_url(page))
        if not r:
            print(f"  стр {page}: не загрузилась, стоп"); break
        ids = []
        for m in re.finditer(r"/ru/announce/index/(\d+)", r.text):
            if m.group(1) not in ids:
                ids.append(m.group(1))
        if not ids:
            print(f"  стр {page}: лотов нет, конец месяца"); break
        print(f"  стр {page}/{pages or '?'}: {len(ids)} объявл. "
              f"(осталось ~{int(time_left())//60} мин)")
        for aid in ids:
            if time_left() < 45:      # запас на сохранение
                print(f"\n⏰ Время вышло на объявлениях — сохраняю.")
                stopped_early = True
                break
            if aid in seen:
                continue
            seen.add(aid)
            pr = get(f"{BASE}/ru/announce/index/{aid}?tab=protocols")
            if not pr:
                continue
            pm = re.search(r'(https?://[^"\']*download_file[^"\']*)', pr.text)
            if not pm:
                continue
            pdf = get(pm.group(1), timeout=60)
            if not pdf or len(pdf.content) < 1000:
                continue
            rows = parse_pdf(pdf.content, aid)
            all_rows.extend(rows)
            done += 1
            if rows and done % 10 == 0:
                print(f"    [{done}] +{len(rows)} (всего {len(all_rows)})")
        if stopped_early:
            break
        page += 1

    reason = "лимит времени — сохранено что успел" if stopped_early else "месяц собран полностью"
    save(all_rows, reason)

if __name__ == "__main__":
    main()
