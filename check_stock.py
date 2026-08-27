"""
Zara Stock Alert
-----------------
Проверяет список товаров с сайта Zara и присылает уведомление в Telegram,
как только нужный размер становится доступен для заказа.

Как это работает:
Обычная страница товара на zara.com отдаётся серверу-роботу в виде пустой
заглушки — данные о наличии подгружаются уже в браузере. Поэтому скрипт
берёт их напрямую из открытого JSON-эндпоинта, которым пользуется сам сайт:

    https://www.zara.com/us/en/products-details?productIds=<ID>&ajax=true

<ID> — это число из хвоста ссылки на товар (?v1=...), то есть идентификатор
конкретного цвета. За один запрос можно спросить до 10 товаров сразу
(несколько параметров productIds), поэтому 38 товаров = 4 запроса.

Значения availability:
  - "in_stock"      -> есть в наличии
  - "low_on_stock"  -> можно заказать, но осталось мало
  - "coming_soon"   -> товара пока нет в продаже
  - "out_of_stock"  -> товара нет

ВАЖНО: редактировать нужно только products.json — см. файл
"КАК-ОБНОВЛЯТЬ-ТОВАРЫ.md". Этот файл трогать не нужно.
"""

import json
import os
import random
import re
import time
from datetime import datetime
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests

CONFIG_FILE = "products.json"
STATE_FILE = "state.json"

API_URL = "https://www.zara.com/us/en/products-details"
CHUNK_SIZE = 10          # больше 10 сервер не принимает (отвечает 400)
CHUNK_PAUSE_SEC = (1.0, 2.0)

# Статусы, которые считаем "можно заказать прямо сейчас"
AVAILABLE_STATUSES = {"in_stock", "low_on_stock"}
# Статусы, которые точно означают "нельзя заказать" (не ошибка разбора)
NOT_AVAILABLE_STATUSES = {"out_of_stock", "coming_soon"}

# --- Окно проверки: Пн–Пт, 15:00–19:00 по Вашингтону -----------------------
ACTIVE_WEEKDAYS_ONLY = True
ACTIVE_START_HOUR = 15   # с 15:00
ACTIVE_END_HOUR = 19     # до 19:00 (последняя проверка ~18:55)
TIMEZONE = ZoneInfo("America/New_York")  # Восточное время США (Вашингтон)
# --------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.zara.com/us/en/",
    "Origin": "https://www.zara.com",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def in_active_window() -> bool:
    if not ACTIVE_WEEKDAYS_ONLY:
        return True
    now = datetime.now(TIMEZONE)
    is_weekday = now.weekday() < 5  # 0 = понедельник ... 4 = пятница
    is_in_hours = ACTIVE_START_HOUR <= now.hour < ACTIVE_END_HOUR
    return is_weekday and is_in_hours


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[error] не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[error] не удалось отправить сообщение в Telegram: {exc}")


def normalize_size(value) -> str:
    """Приводит размер к единому виду: 7½ = 7 1/2 = 7.5, а m = M.

    На сайте обувные размеры записаны как "7½", в products.json их удобнее
    писать как "7.5" — эта функция делает их одинаковыми.
    """
    s = str(value).upper().strip()
    s = s.replace("¼", ".25").replace("½", ".5").replace("¾", ".75")
    s = re.sub(r"\s*1\s*/\s*2\b", ".5", s)
    s = re.sub(r"\s*1\s*/\s*4\b", ".25", s)
    s = re.sub(r"\s*3\s*/\s*4\b", ".75", s)
    s = s.replace(",", ".").replace(" ", "")
    if re.fullmatch(r"\d+(\.\d+)?", s):   # "7.50" -> "7.5", "8.0" -> "8"
        s = f"{float(s):g}"
    return s


def variant_id(url: str):
    """Достаёт из ссылки параметр v1 — идентификатор цвета товара."""
    try:
        value = parse_qs(urlparse(url).query).get("v1", [None])[0]
    except ValueError:
        return None
    return str(value) if value else None


def fetch_chunk(session, ids):
    """Спрашивает у сайта наличие для группы товаров.

    Возвращает словарь {id_цвета: {"color": название, "sizes": [(размер, статус)]}}
    или None, если запрос не удался.
    """
    params = [("productIds", i) for i in ids] + [("ajax", "true")]
    try:
        resp = session.get(API_URL, params=params, headers=HEADERS, timeout=30)
    except requests.RequestException as exc:
        print(f"[warn] запрос не удался: {exc}")
        return None

    if resp.status_code != 200:
        print(f"[warn] сайт ответил {resp.status_code} "
              f"(длина ответа {len(resp.content)} байт)")
        return None

    try:
        data = resp.json()
    except ValueError:
        print(f"[warn] сайт вернул не JSON (длина {len(resp.content)} байт). "
              f"Начало ответа: {resp.text[:200]!r}")
        return None

    if not isinstance(data, list):
        print(f"[warn] неожиданный ответ сайта: {str(data)[:200]}")
        return None

    result = {}
    for product in data:
        try:
            colors = product["detail"]["colors"]
        except (KeyError, TypeError):
            continue
        for color in colors:
            cid = color.get("productId")
            if cid is None:
                continue
            result[str(cid)] = {
                "color": color.get("name"),
                "sizes": [((s.get("name") or "").strip(),
                           s.get("availability", "unknown"))
                          for s in (color.get("sizes") or [])],
            }
    return result


def fetch_all(ids):
    """Забирает наличие для всех нужных цветов, группами по CHUNK_SIZE."""
    session = requests.Session()
    combined, failed_any = {}, False
    chunks = [ids[i:i + CHUNK_SIZE] for i in range(0, len(ids), CHUNK_SIZE)]
    for n, chunk in enumerate(chunks, start=1):
        if n > 1:
            time.sleep(random.uniform(*CHUNK_PAUSE_SEC))
        part = fetch_chunk(session, chunk)
        if part is None:
            failed_any = True
            print(f"[warn] группа {n} из {len(chunks)}: данные не получены")
            continue
        combined.update(part)
        print(f"[ok] группа {n} из {len(chunks)}: получено цветов — {len(part)}")
    return combined, failed_any


def load_products():
    """Читает products.json и проверяет его на ошибки.

    Если файл сломан или заполнен неправильно — присылает предупреждение
    в Telegram, чтобы ошибка не осталась незамеченной.
    """
    try:
        raw = load_json(CONFIG_FILE, None)
    except json.JSONDecodeError as exc:
        msg = (f"⚠️ Ошибка в файле products.json (строка {exc.lineno}): "
               f"{exc.msg}.\n\nПроверка остановлена, пока файл не исправлен. "
               f"Частые причины: лишняя или пропущенная запятая, незакрытая "
               f"кавычка или скобка.")
        print(f"[error] {msg}")
        send_telegram_message(msg)
        return None

    if raw is None:
        print(f"[error] файл {CONFIG_FILE} не найден.")
        return None

    if not isinstance(raw, list):
        msg = "⚠️ products.json должен начинаться с [ и заканчиваться ]."
        print(f"[error] {msg}")
        send_telegram_message(msg)
        return None

    valid, problems, seen = [], [], set()
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            problems.append(f"товар №{i}: должен быть объектом в фигурных скобках")
            continue
        url = item.get("url")
        sizes = item.get("sizes")
        label = item.get("name") or url or f"товар №{i}"
        if not url or not isinstance(url, str):
            problems.append(f"товар №{i}: не указан url")
            continue
        if not sizes or not isinstance(sizes, list):
            problems.append(f"{label}: не указаны sizes")
            continue
        if item.get("active", True) is False:
            print(f"[skip] {label}: отключён (active: false)")
            continue
        if url in seen:
            print(f"[skip] {label}: такая ссылка уже есть в списке")
            continue
        vid = variant_id(url)
        if not vid:
            problems.append(
                f"{label}: в ссылке нет хвоста ?v1=... — откройте товар на "
                f"сайте, выберите цвет и скопируйте адрес целиком")
            continue
        seen.add(url)
        item = dict(item)
        item["_vid"] = vid
        valid.append(item)

    if problems:
        msg = "⚠️ Проблемы в products.json:\n- " + "\n- ".join(problems)
        print(f"[error] {msg}")
        send_telegram_message(msg)

    return valid


def main():
    if not in_active_window():
        now = datetime.now(TIMEZONE)
        print(f"Сейчас {now:%a %H:%M} по Вашингтону — вне окна проверки "
              f"(Пн–Пт {ACTIVE_START_HOUR}:00–{ACTIVE_END_HOUR}:00). Пропускаем.")
        return

    products = load_products()
    if not products:
        print("Нечего проверять.")
        return

    ids = sorted({p["_vid"] for p in products})
    print(f"Проверяем {len(products)} товар(ов), {len(ids)} цвет(ов)...")

    stock, failed_any = fetch_all(ids)
    if not stock:
        msg = ("⚠️ Не удалось получить данные с сайта Zara ни по одному товару. "
               "Подробности — в логах GitHub Actions.")
        print(f"[error] {msg}")
        send_telegram_message(msg)
        return

    state = load_json(STATE_FILE, {})
    new_state = {}

    for product in products:
        key = product["url"]
        name = product.get("name", key)
        vid = product["_vid"]
        wanted = {normalize_size(s) for s in product["sizes"]}
        previously_notified = set(state.get(key, []))

        entry = stock.get(vid)
        if entry is None:
            # Данных нет: либо запрос не прошёл, либо товар/цвет убрали с сайта
            reason = ("запрос не прошёл" if failed_any
                      else "товар или цвет больше не найден на сайте")
            print(f"[warn] {name}: данных нет — {reason}")
            new_state[key] = sorted(previously_notified)
            continue

        found, seen_raw = [], []
        for raw_size, status in entry["sizes"]:
            seen_raw.append(raw_size)
            if normalize_size(raw_size) not in wanted:
                continue
            if status not in AVAILABLE_STATUSES and status not in NOT_AVAILABLE_STATUSES:
                print(f"[info] {name} / {raw_size}: неизвестный статус '{status}' "
                      f"— возможно, стоит добавить его в AVAILABLE_STATUSES")
            if status in AVAILABLE_STATUSES:
                found.append(raw_size)

        missing = wanted - {normalize_size(s) for s in seen_raw}
        if missing:
            print(f"[info] {name}: размера(ов) {', '.join(sorted(missing))} нет "
                  f"среди размеров этого товара. Есть: "
                  f"{', '.join(seen_raw) or '—'}")

        available_now = {normalize_size(s) for s in found}
        newly_available = available_now - previously_notified

        if newly_available:
            shown = ", ".join(s for s in found
                              if normalize_size(s) in newly_available)
            color = entry.get("color")
            color_line = f"\nЦвет: {color}" if color else ""
            send_telegram_message(
                f"🟢 Появился размер!\n"
                f"{name}{color_line}\n"
                f"Размер(ы): {shown}\n"
                f"{key}"
            )
            print(f"[notify] {name}: {shown}")
        else:
            print(f"[ok] {name}: новых размеров нет "
                  f"(сейчас доступно: {', '.join(sorted(available_now)) or '—'})")

        new_state[key] = sorted(available_now)

    # Удаляем из state товары, убранные или отключённые в products.json
    if new_state != state:
        save_json(STATE_FILE, new_state)


if __name__ == "__main__":
    main()
