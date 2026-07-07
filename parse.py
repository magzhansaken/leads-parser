#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TenderView — Парсер протоколов goszakup (ФАЗА 2: с диагностикой).
Считает ПРОПУСКИ — что теряется под параллельной нагрузкой.
Показывает: пустые страницы, ненайденные протоколы, битые PDF.
"""
import re, io, os, sys, csv, time

try:
    import requests, pypdf
except ImportError:
    print("Установи: pip install requests pypdf"); sys.exit(1)

YEAR        = int(os.environ.get("P_YEAR", "2026"))
MONTH       = int(os.environ.get("P_MONTH", "6"))
AMOUNT_FROM = int(os.environ.get("P_AMOUNT_FROM", "150000"))
_at         = os.environ.get("P_AMOUNT_TO", "500000")
AMOUNT_TO   = int(_at) if _at and _at.lower() != "none" else None
GRP         = os.environ.get("P_GRP", "150-500т")
OUT_CSV     = os.environ.get("P_OUT", f"leads_{YEAR}_{MONTH:02d}.csv")
TIME_BUDGET = int(os.environ.get("P_TIME_BUDGET", str(5 * 3600 + 40 * 60)))
START_TS    = time.time()

PAUSE    = float(os.environ.get("P_PAUSE", "1.5"))   # чуть больше для безопасности
PER_PAGE = 50
BASE     = "https://goszakup.gov.kz"

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
})

# --- СЧЁТЧИКИ ПРОПУСКОВ (диагностика) ---
stats = {
    "объявлений_всего": 0,
    "страница_не_загрузилась": 0,
    "протокол_страница_не_загрузилась": 0,
    "ссылка_на_pdf_не_найдена": 0,
    "pdf_не_скачался": 0,
    "pdf_пустой_битый": 0,
    "pdf_разобран_но_0_записей": 0,
    "успешно_с_данными": 0,
    "повторных_попыток": 0,
}

def time_left():
    return TIME_BUDGET - (time.time() - START_TS)

def get(url, timeout=40):
    for attempt in range(3):
        try:
            r = session.get(url, timeout=timeout)
            time.sleep(PAUSE)
            if r.status_code == 200:
                if attempt > 0:
                    stats["повторных_попыток"] += 1
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

def main():
    print("=" * 56)
    print(f" ФАЗА 2 · {GRP} · {MONTH}/{YEAR} · пауза {PAUSE}с")
    print(f" Бюджет: {TIME_BUDGET//60} мин")
    print("=" * 56)

    all_rows, seen = [], set()
    page = 1
    while True:
        if time_left() < 30:
            print("\n⏰ Время вышло — сохраняю.")
            break
        r = get(search_url(page))
        if not r:
            stats["страница_не_загрузилась"] += 1
            print(f"  ⚠️ стр {page}: НЕ загрузилась (пропуск страницы!)")
            break
        ids = []
        for m in re.finditer(r"/ru/announce/index/(\d+)", r.text):
            if m.group(1) not in ids:
                ids.append(m.group(1))
        if not ids:
            print(f"  стр {page}: лотов нет, конец")
            break
        print(f"  стр {page}: {len(ids)} объявл. (осталось ~{int(time_left())//60}м)")
        for aid in ids:
            if time_left() < 20:
                break
            if aid in seen:
                continue
            seen.add(aid)
            stats["объявлений_всего"] += 1

            pr = get(f"{BASE}/ru/announce/index/{aid}?tab=protocols")
            if not pr:
                stats["протокол_страница_не_загрузилась"] += 1
                continue
            pm = re.search(r'(https?://[^"\']*download_file[^"\']*)', pr.text)
            if not pm:
                stats["ссылка_на_pdf_не_найдена"] += 1
                continue
            pdf = get(pm.group(1), timeout=60)
            if not pdf:
                stats["pdf_не_скачался"] += 1
                continue
            if len(pdf.content) < 1000:
                stats["pdf_пустой_битый"] += 1
                continue
            rows = parse_pdf(pdf.content, aid)
            if not rows:
                stats["pdf_разобран_но_0_записей"] += 1
                continue
            stats["успешно_с_данными"] += 1
            all_rows.extend(rows)
        page += 1

    # сохраняем CSV
    cols = ["БИН","поставщик","товар","цена_тендера","ставка","победитель",
            "заказчик","лот","дата","группа","год","месяц","announce_id"]
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader(); w.writerows(all_rows)

    # ═══ ОТЧЁТ ПО ПРОПУСКАМ (главное!) ═══
    dt = int(time.time() - START_TS)
    print("\n" + "=" * 56)
    print(f" ОТЧЁТ ({GRP}, {MONTH}/{YEAR}) за {dt//60}м{dt%60}с")
    print("=" * 56)
    print(f"  Объявлений обработано: {stats['объявлений_всего']}")
    print(f"  ✅ Успешно с данными:  {stats['успешно_с_данными']}")
    print(f"  Записей собрано:       {len(all_rows)}")
    print(f"  Поставщиков (БИН):     {len(set(r['БИН'] for r in all_rows))}")
    print("  --- ПРОПУСКИ (что потеряли) ---")
    print(f"  Страниц не загрузилось:      {stats['страница_не_загрузилась']}")
    print(f"  Стр. протокола не загруз.:   {stats['протокол_страница_не_загрузилась']}")
    print(f"  Ссылка на PDF не найдена:    {stats['ссылка_на_pdf_не_найдена']}")
    print(f"  PDF не скачался:             {stats['pdf_не_скачался']}")
    print(f"  PDF битый/пустой:            {stats['pdf_пустой_битый']}")
    print(f"  PDF без записей:             {stats['pdf_разобран_но_0_записей']}")
    print(f"  Повторных попыток (ретрай):  {stats['повторных_попыток']}")
    # процент потерь
    total = stats['объявлений_всего']
    if total:
        lost = total - stats['успешно_с_данными']
        print(f"  --- ИТОГО потеряно: {lost}/{total} ({lost*100//total}%) ---")
    print("=" * 56)
    print(" Если пропусков МНОГО — goszakup теряет под нагрузкой,")
    print(" надо больше паузу/меньше параллельных. Если МАЛО — всё ок.")

if __name__ == "__main__":
    main()
