"""
Zara Stock Alert
-----------------
Проверяет список товаров с сайта Zara и присылает уведомление в Telegram,
как только нужный размер становится доступен для заказа.

Как это работает:
Zara встраивает данные о наличии размеров прямо в HTML-код страницы товара
(в JSON-объект window.zara.viewPayload). Обычного HTTP-запроса достаточно,
браузер не требуется.

Возможные значения availability:
  - "in_stock"      -> есть в наличии
  - "low_on_stock"  -> можно заказать, но осталось мало
  - "coming_soon"   -> товара пока нет в продаже
  - "out_of_stock"  -> товара нет
Если сайт вернёт другой статус, скрипт выведет предупреждение в лог.

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

# Статусы, которые считаем "можно заказать прямо сейчас"
AVAILABLE_STATUSES = {"in_stock", "low_on_stock"}
# Статусы, которые точно означают "нельзя заказать" (не ошибка парсинга)
NOT_AVAILABLE_STATUSES = {"out_of_stock", "coming_soon"}

# --- Окно проверки: Пн–Пт, 15:00–19:00 по Вашингтону -----------------------
ACTIVE_WEEKDAYS_ONLY = True
ACTIVE_START_HOUR = 15   # с 15:00
ACTIVE_END_HOUR = 19     # до 19:00 (последняя проверка ~18:55)
TIMEZONE = ZoneInfo("America/New_York")  # Восточное время США (Вашингтон)
# --------------------------------------------------------------------------

# Пауза между товарами, чтобы не отправлять запросы сплошным потоком
DELAY_MIN_SEC = 1.0
DELAY_MAX_SEC = 2.5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
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
    """Приводит размер к единому виду, чтобы 7½ = 7 1/2 = 7.5, а m = M.

    Обувные размеры на сайте могут быть записаны как "7½", а в products.json
    их удобнее писать как "7.5" — эта функция делает их одинаковыми.
    """
    s = str(value).upper().strip()
    s = s.replace("¼", ".25").replace("½", ".5").replace("¾", ".75")
    s = re.sub(r"\s*1\s*/\s*2\b", ".5", s)
    s = re.sub(r"\s*1\s*/\s*4\b", ".25", s)
    s = re.sub(r"\s*3\s*/\s*4\b", ".75", s)
    s = s.replace(",", ".").replace(" ", "")
    # "7.50" -> "7.5", "8.0" -> "8"
    if re.fullmatch(r"\d+(\.\d+)?", s):
        s = f"{float(s):g}"
    return s


def color_variant_id(url: str):
    """Достаёт из ссылки параметр v1 — идентификатор цвета товара."""
    try:
        return parse_qs(urlparse(url).query).get("v1", [None])[0]
    except ValueError:
        return None


def pick_colors(colors, variant_id, name):
    """Оставляет только тот цвет, что указан в ссылке (?v1=...).

    Если сопоставить не удалось — проверяем все цвета и пишем об этом в лог.
    Так уведомление придёт в любом случае, просто, возможно, по другому цвету.
    """
    if not variant_id:
        return colors, None
    keys = ("id", "productId", "colorId", "reference", "sku")
    for color in colors:
        candidates = {str(color.get(k)) for k in keys if color.get(k) is not None}
        if str(variant_id) in candidates:
            return [color], color.get("name")
    print(f"[info] {name}: не удалось сопоставить v1={variant_id} с конкретным "
          f"цветом — проверяем все цвета этого товара")
    return colors, None


def extract_view_payload(html: str):
    marker = "window.zara.viewPayload = "
    idx = html.find(marker)
    if idx == -1:
        return None
    start = idx + len(marker)
    decoder = json.JSONDecoder()
    try:
        data, _ = decoder.raw_decode(html[start:])
        return data
    except json.JSONDecodeError:
        return None


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
        if not url or not isinstance(url, str):
            problems.append(f"товар №{i}: не указан url")
            continue
        if not sizes or not isinstance(sizes, list):
            problems.append(f"товар №{i} ({item.get('name', url)}): не указаны sizes")
            continue
        if item.get("active", True) is False:
            print(f"[skip] {item.get('name', url)}: отключён (active: false)")
            continue
        if url in seen:
            print(f"[skip] {item.get('name', url)}: такая ссылка уже есть в списке")
            continue
        seen.add(url)
        valid.append(item)

    if problems:
        msg = "⚠️ Проблемы в products.json:\n- " + "\n- ".join(problems)
        print(f"[error] {msg}")
        send_telegram_message(msg)

    return valid


def check_product(product: dict):
    """Размеры из нужных, которые сейчас можно заказать.

    Возвращает (список_размеров, название_цвета) или None, если проверить
    не удалось (сайт не ответил и т.п.) — это не то же самое, что
    "размеров нет".
    """
    url = product["url"]
    name = product.get("name", url)
    wanted = {normalize_size(s) for s in product["sizes"]}

    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[warn] {name}: не удалось загрузить страницу — {exc}")
        return None

    payload = extract_view_payload(resp.text)
    if not payload:
        print(f"[warn] {name}: не нашли данные о наличии на странице "
              f"(возможно, изменилась структура сайта)")
        return None

    try:
        colors = payload["product"]["detail"]["colors"]
    except (KeyError, TypeError):
        print(f"[warn] {name}: неожиданная структура данных")
        return None

    colors, color_name = pick_colors(colors, color_variant_id(url), name)

    found, seen_raw = [], []
    for color in colors:
        for size in color.get("sizes", []):
            raw = (size.get("name") or "").strip()
            seen_raw.append(raw)
            if normalize_size(raw) not in wanted:
                continue
            status = size.get("availability", "unknown")
            if status not in AVAILABLE_STATUSES and status not in NOT_AVAILABLE_STATUSES:
                print(f"[info] {name} / {raw}: неизвестный статус наличия "
                      f"'{status}' — возможно, стоит добавить его в AVAILABLE_STATUSES")
            if status in AVAILABLE_STATUSES:
                found.append(raw)

    missing = wanted - {normalize_size(s) for s in seen_raw}
    if missing:
        print(f"[info] {name}: размера(ов) {', '.join(sorted(missing))} нет среди "
              f"размеров этого товара. Доступные варианты: "
              f"{', '.join(sorted(set(seen_raw))) or '—'}")

    return sorted(set(found)), color_name


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

    print(f"Проверяем {len(products)} товар(ов)...")
    state = load_json(STATE_FILE, {})
    new_state = {}

    for i, product in enumerate(products):
        if i:
            time.sleep(random.uniform(DELAY_MIN_SEC, DELAY_MAX_SEC))

        key = product["url"]
        name = product.get("name", key)
        result = check_product(product)
        previously_notified = set(state.get(key, []))

        if result is None:
            # Проверить не удалось — сохраняем прошлое состояние как есть
            new_state[key] = sorted(previously_notified)
            continue

        sizes_list, color_name = result
        available_now = {normalize_size(s) for s in sizes_list}
        newly_available = available_now - previously_notified

        if newly_available:
            shown = ", ".join(sorted(s for s in sizes_list
                                     if normalize_size(s) in newly_available))
            color_line = f"\nЦвет: {color_name}" if color_name else ""
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
