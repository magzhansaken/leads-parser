#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TenderView — Сбор протоколов ПО НОМЕРАМ ЛОТОВ (для GitHub Actions).
Читает all_numbers.txt, берёт свою часть (по TASK_INDEX из TASK_TOTAL),
идёт по номерам → протокол → участники (все форматы) → CSV.

Защита от обрыва: сам сохранит за 20 мин до лимита 6ч.
Дедуп: один announce не качаем дважды.

Env (задаёт workflow):
  TASK_INDEX  — номер этой задачи (0..TASK_TOTAL-1)
  TASK_TOTAL  — всего задач (напр. 150)
  P_TIME_BUDGET — бюджет секунд (по умолч. 5ч40м)
  P_PAUSE     — пауза между запросами
"""
import re, io, os, sys, csv, time
from urllib.parse import quote

try:
    import requests, pypdf
except ImportError:
    print("Установи: pip install requests pypdf"); sys.exit(1)

# --- параметры задачи ---
TASK_INDEX = int(os.environ.get("TASK_INDEX", "0"))
TASK_TOTAL = int(os.environ.get("TASK_TOTAL", "150"))
NUMBERS_FILE = os.environ.get("P_NUMBERS", "all_numbers.txt")
OUT_CSV = os.environ.get("P_OUT", f"leads_part_{TASK_INDEX:03d}.csv")

TIME_BUDGET = int(os.environ.get("P_TIME_BUDGET", str(5 * 3600 + 40 * 60)))
START_TS = time.time()
PAUSE = float(os.environ.get("P_PAUSE", "1.2"))

BASE = "https://goszakup.gov.kz"
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
})

# --- счётчики (диагностика) ---
stats = {
    "лотов_моих": 0, "обработано": 0, "пропущен_дубль_announce": 0,
    "не_нашёл_лот": 0, "нет_протокола": 0, "pdf_не_скачался": 0,
    "pdf_битый": 0, "0_записей": 0, "успешно": 0, "ретраев": 0,
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
                    stats["ретраев"] += 1
                return r
        except Exception:
            time.sleep(PAUSE * 2)
    return None

def parse_pdf(pdf_bytes, aid):
    """Разбор всех форматов протокола (2 прохода: обычный + перенос названия)."""
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
        found = {}
        p1 = r"\n\s*(\d+)\s+([^\n]+?)\s+(\d{12})\s+(?:[\d.]+\s+)*?([\d.]+)\s+(\d{4}-\d{2}-\d{2})"
        p2 = r"\n\s*(\d+)\s+([А-Яа-я][^\n]*(?:\n[^\n\d][^\n]*)*)\n\s*(\d{12})\s+(?:[\d.]+\s+)*?([\d.]+)\s+(\d{4}-\d{2}-\d{2})"
        for pat in (p1, p2):
            for p in re.finditer(pat, block):
                _, pn, pb, tot, pd = p.groups()
                key = (pb, tot)
                if key in found:
                    continue
                found[key] = {
                    "БИН": pb, "поставщик": " ".join(pn.split()), "товар": tovar,
                    "цена_тендера": plan, "ставка": tot,
                    "победитель": "да" if pb == wbin else "",
                    "заказчик": cust, "лот": lot_no, "дата": pd[:10],
                    "announce_id": aid,
                }
        rows.extend(found.values())
    return rows

def process_lot(lot_num, seen_announces):
    """Один номер лота → протокол → записи."""
    # поиск по номеру
    r = get(f"{BASE}/ru/search/lots?filter%5Bnumber%5D={quote(lot_num)}")
    if not r:
        stats["не_нашёл_лот"] += 1
        return []
    ann = re.search(r"/ru/announce/index/(\d+)", r.text)
    if not ann:
        stats["не_нашёл_лот"] += 1
        return []
    aid = ann.group(1)
    # дедуп: этот announce уже качали?
    if aid in seen_announces:
        stats["пропущен_дубль_announce"] += 1
        return []
    seen_announces.add(aid)
    # протокол
    pr = get(f"{BASE}/ru/announce/index/{aid}?tab=protocols")
    if not pr:
        stats["нет_протокола"] += 1
        return []
    pm = re.search(r'(https?://[^"\']*download_file[^"\']*)', pr.text)
    if not pm:
        stats["нет_протокола"] += 1
        return []
    pdf = get(pm.group(1), timeout=60)
    if not pdf:
        stats["pdf_не_скачался"] += 1
        return []
    if len(pdf.content) < 1000:
        stats["pdf_битый"] += 1
        return []
    rows = parse_pdf(pdf.content, aid)
    if not rows:
        stats["0_записей"] += 1
        return []
    stats["успешно"] += 1
    return rows

def save(rows, reason):
    cols = ["БИН","поставщик","товар","цена_тендера","ставка","победитель",
            "заказчик","лот","дата","announce_id"]
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader(); w.writerows(rows)
    dt = int(time.time() - START_TS)
    print("\n" + "=" * 56)
    print(f" СОХРАНЕНО ({reason})")
    print(f" Задача {TASK_INDEX}/{TASK_TOTAL} · время {dt//60}м{dt%60}с")
    print(f" Записей: {len(rows)} · поставщиков: {len(set(r['БИН'] for r in rows))}")
    print("=" * 56)

def main():
    print("=" * 56)
    print(f" Сбор по номерам · задача {TASK_INDEX} из {TASK_TOTAL}")
    print(f" Бюджет: {TIME_BUDGET//3600}ч{(TIME_BUDGET%3600)//60}м · пауза {PAUSE}с")
    print("=" * 56)

    # читаем номера, берём СВОЮ часть
    if not os.path.exists(NUMBERS_FILE):
        print(f"!!! Нет файла {NUMBERS_FILE}"); sys.exit(1)
    with open(NUMBERS_FILE, encoding="utf-8") as f:
        all_nums = [ln.strip() for ln in f if ln.strip()]
    # моя часть: индексы i, где i % TASK_TOTAL == TASK_INDEX
    my_nums = [n for i, n in enumerate(all_nums) if i % TASK_TOTAL == TASK_INDEX]
    stats["лотов_моих"] = len(my_nums)
    print(f" Всего номеров в файле: {len(all_nums):,}")
    print(f" Моя часть: {len(my_nums):,} лотов")
    print("-" * 56)

    all_rows, seen = [], set()
    stopped = False
    for i, lot in enumerate(my_nums):
        if time_left() < 60:
            print(f"\n⏰ Время на исходе — сохраняю ({i}/{len(my_nums)} обработано).")
            stopped = True
            break
        stats["обработано"] += 1
        rows = process_lot(lot, seen)
        all_rows.extend(rows)
        # прогресс каждые 25 лотов
        if (i + 1) % 25 == 0:
            print(f"  [{i+1}/{len(my_nums)}] записей: {len(all_rows)} · "
                  f"успешно: {stats['успешно']} · дублей: {stats['пропущен_дубль_announce']} · "
                  f"осталось ~{int(time_left())//60}м")

    reason = "время вышло" if stopped else "вся моя часть собрана"
    save(all_rows, reason)

    # отчёт
    print("\n ОТЧЁТ ПО ЗАДАЧЕ:")
    for k, v in stats.items():
        print(f"   {k}: {v}")

if __name__ == "__main__":
    main()
