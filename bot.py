from __future__ import annotations

import json
import logging
import pickle
import re
from datetime import datetime, timedelta, date
from html import escape as html_escape
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    PicklePersistence,
    filters,
)

from config import load_config

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)

LEADS_FILE = BASE_DIR / "leads.json"
BACK_TEXT = "⬅️ Назад"
CONTACT_TEXT = "📞 Отправить номер телефона"
HOME_TEXT = "🏠 В начало"
STATE_FILE = BASE_DIR / "bot_state.pkl"


def ensure_valid_state_file() -> None:
    if not STATE_FILE.exists():
        return
    try:
        with open(STATE_FILE, "rb") as f:
            pickle.load(f)
    except Exception:
        broken_name = STATE_FILE.with_suffix(f".broken-{datetime.now().strftime('%Y%m%d-%H%M%S')}.pkl")
        try:
            STATE_FILE.rename(broken_name)
            logger.warning("Broken bot_state.pkl moved to %s", broken_name.name)
        except Exception:
            try:
                STATE_FILE.unlink(missing_ok=True)
                logger.warning("Broken bot_state.pkl removed")
            except Exception:
                logger.exception("Failed to cleanup broken bot_state.pkl")


# Conversation states
(
    S_DEVICE,
    S_MODEL,
    S_SERVICE,
    S_PROBLEM,
    S_PART_CHOICE,
    S_PHONE,
    S_DATE,
    S_TIME,
    S_CONFIRM,
    S_SUPPORT,
) = range(10)


# --- Load catalogs

def load_services() -> List[Dict[str, Any]]:
    with open(BASE_DIR / "services.json", "r", encoding="utf-8") as f:
        return json.load(f)


def load_repair_catalog() -> Dict[str, Any]:
    p = BASE_DIR / "repair_catalog.json"
    if not p.exists():
        return {"services": {}}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


SERVICES = load_services()
REPAIR_CATALOG = load_repair_catalog()
SERVICE_BY_CODE = {s["code"]: s for s in SERVICES}


def load_site_model_map() -> Dict[str, Any]:
    p = BASE_DIR / "site_model_map.json"
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


SITE_MODEL_MAP = load_site_model_map()


def get_cfg(context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data.get("cfg")
    if cfg is not None:
        return cfg
    cfg = load_config()
    context.bot_data["cfg"] = cfg
    return cfg


# --- Helpers

def esc(value: Any) -> str:
    return html_escape(str(value), quote=False)


def format_rub(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " ₽"


def format_price_range(mn: Optional[int], mx: Optional[int]) -> str:
    if mn is None or mx is None:
        return "Уточним после осмотра"
    if mn == 0 and mx == 0:
        return "Бесплатно"
    if mn == mx:
        return format_rub(mn)
    return f"{format_rub(mn)} — {format_rub(mx)}"


def parse_start_payload(payload: str) -> Dict[str, str]:
    payload = (payload or "").strip()
    if not payload:
        return {}

    def clean_site_value(value: str) -> str:
        value = (value or "").strip()
        return "" if value.lower() in {"", "na", "none", "null"} else value

    if payload == "consult":
        return {"mode": "consult"}

    if payload.startswith("b_") and "_" in payload:
        parts = payload.split("_")
        if len(parts) == 3:
            return {"mode": "book", "site_code": parts[1], "service_short": parts[2]}
        if len(parts) >= 4:
            device = parts[1]
            model = parts[2].replace("-", " ")
            service = parts[3]
            return {"mode": "book", "device": device, "model": model, "service": service}

    if payload.startswith("b|"):
        p = payload.split("|")
        if len(p) == 3:
            return {"mode": "book", "site_code": p[1], "service_short": p[2]}
        if len(p) >= 4:
            return {"mode": "book", "device": p[1], "model": p[2], "service": p[3]}

    if payload.startswith("o_"):
        p = payload.split("_", 4)
        if len(p) >= 5:
            return {
                "mode": "site_order",
                "code": clean_site_value(p[1]),
                "problem": clean_site_value(p[2]),
                "phone": clean_site_value(p[3]),
                "tg": clean_site_value(p[4]),
            }

    if payload.startswith("o|"):
        p = payload.split("|")
        if len(p) >= 5:
            return {
                "mode": "site_order",
                "code": clean_site_value(p[1]),
                "problem": clean_site_value(p[2]),
                "phone": clean_site_value(p[3]),
                "tg": clean_site_value(p[4]),
            }

    return {}


def site_problem_to_service(problem_code: str) -> str:
    return {
        "s": "screen",
        "b": "battery",
        "c": "flex_charge",
        "k": "speaker",
        "o": "diag",
    }.get((problem_code or "").strip().lower(), "diag")


def site_problem_label(problem_code: str) -> str:
    return {
        "s": "Разбитый экран",
        "b": "Быстро садится батарея",
        "c": "Не заряжается / разъём",
        "k": "Проблемы со звуком",
        "o": "Другая проблема",
    }.get((problem_code or "").strip().lower(), "Другая проблема")


def site_service_short_to_key(service_code: str) -> str:
    return {
        "s": "screen",
        "b": "battery",
        "r": "rear_glass",
        "c": "flex_charge",
        "f": "flash_mic",
        "m": "camera",
        "k": "speaker",
        "w": "water",
        "p": "software",
        "d": "diag",
        "o": "diag",
    }.get((service_code or "").strip().lower(), "diag")


def normalize_tg_handle(raw: str) -> str:
    value = (raw or "").strip().replace(" ", "")
    if not value or value.lower() in {"na", "none", "null"}:
        return ""
    value = value.lstrip("@")
    return f"@{value}" if value else ""


def site_model_info(code: str) -> Dict[str, Any]:
    return SITE_MODEL_MAP.get((code or "").strip(), {})


def site_prefill_intro_html(context: ContextTypes.DEFAULT_TYPE) -> str:
    lines: List[str] = []
    flow = str(context.user_data.get("flow") or "")
    device = str(context.user_data.get("device") or "").strip()

    if flow == "other":
        lines.append(f"📦 <b>Тип устройства:</b> {esc(other_kind_label(str(context.user_data.get('other_kind') or '')))}")
    elif device == "iphone":
        lines.append("📦 <b>Тип устройства:</b> iPhone")
    elif device == "android":
        lines.append("📦 <b>Тип устройства:</b> Android смартфон")
    elif device == "tablet":
        lines.append("📦 <b>Тип устройства:</b> Планшет")
    elif device == "laptop":
        lines.append("📦 <b>Тип устройства:</b> Ноутбук")

    lines.append(f"📱 <b>Ваше устройство:</b> {esc(context.user_data.get('model', '—'))}")
    if context.user_data.get("site_problem_label"):
        lines.append(f"🔧 <b>Проблема:</b> {esc(context.user_data['site_problem_label'])}")
    elif str(context.user_data.get("service") or ""):
        lines.append(f"🔧 <b>Услуга:</b> {esc(request_title(context))}")
    if context.user_data.get("phone"):
        lines.append(f"📞 <b>Телефон:</b> {esc(context.user_data['phone'])}")
    if context.user_data.get("site_tg"):
        lines.append(f"✈️ <b>Telegram:</b> {esc(context.user_data['site_tg'])}")
    return "\n".join(lines)


def normalize_device(d: str) -> str:
    d = (d or "").lower().strip()
    if "iphone" in d or d == "ios":
        return "iphone"
    if "android" in d or d in {"samsung", "honor", "xiaomi", "huawei"}:
        return "android"
    return d or "device"


def normalize_phone(raw: str) -> str:
    raw_value = (raw or "").strip()
    if not raw_value or raw_value.lower() in {"na", "none", "null"}:
        return ""
    digits = re.sub(r"\D+", "", raw_value)
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if digits.startswith("7") and len(digits) == 11:
        return "+" + digits
    if len(digits) == 10:
        return "+7" + digits
    return raw_value


def normalize_text(value: str) -> str:
    value = str(value or "").lower().replace("ё", "е")
    value = re.sub(r"[()\"'`]", " ", value)
    value = re.sub(r"[^a-zа-я0-9+/.\- ]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def model_tokens(value: str) -> List[str]:
    norm = normalize_text(value)
    norm = norm.replace("/", " ").replace("+", " ").replace("-", " ")
    tokens = [t for t in norm.split() if t]
    stop = {
        "galaxy", "iphone", "ipad", "samsung", "xiaomi", "redmi", "huawei", "honor",
        "realme", "oppo", "vivo", "poco", "google", "pixel", "oneplus", "motorola",
        "service", "pack", "oled", "amoled", "incell", "original", "copy", "ref",
        "voltpack", "рамке", "рамка", "дисплей", "экран", "module", "lcd",
    }
    return [t for t in tokens if t not in stop]


def model_core_tokens(value: str) -> List[str]:
    tokens = model_tokens(value)
    core = [t for t in tokens if re.search(r"\d", t)]
    return core or tokens


def clean_model_name(value: str) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    s = re.sub(r"\b(service\s*pack|voltpack|soft\s*oled|hard\s*oled|oled|amoled|incell|orig(?:inal)?|copy|aaa|ref|premium)\b", "", s, flags=re.I)
    s = re.sub(r"\b(дисплей|экран|тачскрин|аккумулятор|батарея|корпус|стекло|камера|шлейф|динамик|микрофон)\b.*$", "", s, flags=re.I)
    s = re.sub(r"\b\d{3,5}\s*m?ah\b.*$", "", s, flags=re.I)
    s = re.sub(r"\(\s*в\s*рамке\s*\)", "", s, flags=re.I)
    color_words = r"black|white|blue|green|silver|gold|gray|grey|purple|pink|red|yellow|orange|natural|desert|midnight|starlight|graphite|space|черн|бел|син|зел|сер|фиолет|крас|золот"
    s = re.sub(rf"\((?:{color_words})[^)]*\)$", "", s, flags=re.I)
    s = re.sub(r"\s{2,}", " ", s).strip(" -/,")
    return s


KNOWN_BRANDS = ["iphone", "samsung", "xiaomi", "redmi", "huawei", "honor", "realme", "oppo", "vivo", "poco", "google", "pixel", "oneplus", "motorola", "itel", "infinix"]


def detect_brand(value: str) -> str:
    norm = normalize_text(value)
    for brand in KNOWN_BRANDS:
        if brand in norm.split() or f"{brand} " in norm or norm.startswith(brand):
            return brand
    return ""


def infer_device_from_model(value: str) -> str:
    norm = normalize_text(value)
    if "iphone" in norm:
        return "iphone"
    return "android"


def catalog_model_entries(device: str) -> List[Dict[str, str]]:
    seen = set()
    result: List[Dict[str, str]] = []
    for svc in REPAIR_CATALOG.get("services", {}).values():
        for item in svc.get("items", []) or []:
            aliases = item.get("aliases") or [item.get("model", "")]
            for alias in aliases:
                display = clean_model_name(alias)
                if not display:
                    continue
                alias_device = infer_device_from_model(display)
                if device and device != alias_device:
                    continue
                norm = normalize_text(display)
                key = (device, norm)
                if key in seen:
                    continue
                seen.add(key)
                result.append({"display": display, "norm": norm})
    result.sort(key=lambda x: x["display"].lower())
    return result


def score_model_candidate(query: str, candidate: str) -> float:
    q_norm = normalize_text(query)
    c_norm = normalize_text(candidate)
    if not q_norm or not c_norm:
        return 0.0
    if q_norm == c_norm:
        return 10.0
    if c_norm.startswith(q_norm + " ") or c_norm.startswith(q_norm + "("):
        base = 0.89
    elif q_norm in c_norm:
        base = 0.84
    else:
        base = 0.0

    q_tokens = model_tokens(query)
    c_tokens = model_tokens(candidate)
    q_core = model_core_tokens(query)
    c_set = set(c_tokens)
    c_core_set = set(model_core_tokens(candidate))
    subset_score = 0.0
    if q_tokens and all(t in c_set for t in q_tokens):
        subset_score = 0.91
    elif q_core and all(t in c_core_set for t in q_core):
        subset_score = 0.88

    from difflib import SequenceMatcher
    ratio = SequenceMatcher(None, q_norm, c_norm).ratio()
    score = max(base, subset_score, ratio * 0.82)

    q_brand = detect_brand(query)
    c_brand = detect_brand(candidate)
    if q_brand:
        if c_brand == q_brand:
            score += 0.06
        elif not c_brand:
            score -= 0.07
        else:
            score -= 0.18

    if len(candidate.split()) <= 2 and not c_brand:
        score -= 0.05

    return score


def find_model_candidates(device: str, query: str, limit: int = 6) -> List[str]:
    query = (query or "").strip()
    if not query:
        return []
    ranked = []
    for entry in catalog_model_entries(device):
        score = score_model_candidate(query, entry["display"])
        if score >= 0.72:
            ranked.append((score, entry["display"]))
    ranked.sort(key=lambda x: (-x[0], len(x[1]), x[1]))
    out: List[str] = []
    seen = set()
    for score, display in ranked:
        key = normalize_text(display)
        if key in seen:
            continue
        seen.add(key)
        out.append(display)
        if len(out) >= limit:
            break
    return out


def resolve_model_name(device: str, query: str) -> Tuple[Optional[str], List[str]]:
    candidates = find_model_candidates(device, query, limit=6)
    if not candidates:
        return None, []
    if len(candidates) == 1:
        return candidates[0], candidates
    best = candidates[0]
    best_score = score_model_candidate(query, best)
    second_score = score_model_candidate(query, candidates[1])
    best_norm = normalize_text(best)
    query_norm = normalize_text(query)
    qualifiers = {"pro", "plus", "max", "mini", "lite", "ultra", "fe", "4g", "5g"}
    missing_qualifiers = [q for q in qualifiers if q in best_norm.split() and q not in query_norm.split()]
    if best_score >= 0.90 and (best_score - second_score) >= 0.03:
        return best, candidates
    if query_norm == best_norm:
        return best, candidates
    if best_score >= 0.96 and not missing_qualifiers and "/" not in best:
        return best, candidates
    return None, candidates


# --- Support relay mapping

LEAD_ID_RE = re.compile(r"RB-\d{8}-\d{6}-\d+")


def remember_forward(
    context: ContextTypes.DEFAULT_TYPE,
    forwarded_message_id: int,
    user_id: int,
    lead_id: str = "",
) -> None:
    context.bot_data.setdefault("fw_map", {})[forwarded_message_id] = {
        "user_id": user_id,
        "lead_id": lead_id or "",
    }


def lookup_forward(context: ContextTypes.DEFAULT_TYPE, forwarded_message_id: int) -> Optional[int]:
    data = context.bot_data.get("fw_map", {}).get(forwarded_message_id)
    if isinstance(data, dict):
        return data.get("user_id")
    if isinstance(data, int):
        return data
    return None


def remember_lead(context: ContextTypes.DEFAULT_TYPE, lead_id: str, user_id: int) -> None:
    if not lead_id:
        return
    context.bot_data.setdefault("lead_map", {})[lead_id] = user_id


def lookup_lead(context: ContextTypes.DEFAULT_TYPE, lead_id: str) -> Optional[int]:
    return context.bot_data.get("lead_map", {}).get(lead_id)


def extract_lead_id_from_text(text: str) -> str:
    if not text:
        return ""
    m = LEAD_ID_RE.search(text)
    return m.group(0) if m else ""


def resolve_reply_target_user_id(context: ContextTypes.DEFAULT_TYPE, reply_msg) -> Optional[int]:
    if not reply_msg:
        return None

    user_id = lookup_forward(context, reply_msg.message_id)
    if user_id:
        return user_id

    text = ""
    if getattr(reply_msg, "text", None):
        text = reply_msg.text or ""
    elif getattr(reply_msg, "caption", None):
        text = reply_msg.caption or ""

    lead_id = extract_lead_id_from_text(text)
    if lead_id:
        user_id = lookup_lead(context, lead_id)
        if user_id:
            return user_id

    return None


# --- Catalog helpers

def service_catalog(service_code: str) -> Dict[str, Any]:
    return REPAIR_CATALOG.get("services", {}).get(service_code, {})


def service_has_catalog(service_code: str) -> bool:
    return bool(service_catalog(service_code).get("items"))


def service_title(service_code: str) -> str:
    return SERVICE_BY_CODE.get(service_code, {}).get("title", service_code or "Консультация")


def model_matches_item(model: str, item: Dict[str, Any]) -> bool:
    selected = normalize_text(model)
    if not selected:
        return False

    aliases = [str(x or "").strip() for x in (item.get("aliases") or [item.get("model", "")]) if str(x or "").strip()]
    normalized_aliases = [normalize_text(x) for x in aliases]
    if selected in normalized_aliases:
        return True

    selected_tokens = set(model_tokens(model))
    selected_core = set(model_core_tokens(model))
    selected_brand = detect_brand(model)
    selected_words = set(selected.split())

    qualifier_words = {"pro", "plus", "max", "mini", "ultra", "fe", "lite", "5g", "4g", "2024", "2025", "2023", "e"}
    selected_qualifiers = selected_words & qualifier_words

    def alias_qualifiers(alias_norm: str) -> set[str]:
        return set(alias_norm.split()) & qualifier_words

    def qualifiers_compatible(alias_norm: str) -> bool:
        if selected_qualifiers:
            return alias_qualifiers(alias_norm) == selected_qualifiers
        return not alias_qualifiers(alias_norm)

    def prefix_match_ok(alias_norm: str) -> bool:
        if not alias_norm.startswith(selected):
            return False
        tail = alias_norm[len(selected):]
        if not tail:
            return True
        if not any(tail.startswith(x) for x in (" /", "/", " (", ",", " -", "-", ".")):
            return False
        tail_word = re.sub(r"^[^a-zа-я0-9]+", "", tail)
        tail_word = tail_word.split()[0] if tail_word else ""
        if tail_word in qualifier_words and tail_word not in selected_words:
            return False
        return qualifiers_compatible(alias_norm)

    for raw_alias, alias in zip(aliases, normalized_aliases):
        alias_brand = detect_brand(raw_alias) or detect_brand(alias) or detect_brand(item.get("model", "")) or detect_brand(item.get("raw_name", ""))

        if selected_brand and alias_brand and alias_brand != selected_brand:
            continue

        if not qualifiers_compatible(alias):
            continue

        if prefix_match_ok(alias):
            return True

        alias_tokens = set(model_tokens(raw_alias))
        alias_core = set(model_core_tokens(raw_alias))

        if selected_tokens and selected_tokens == alias_tokens:
            return True

        if selected_tokens and len(selected_tokens) >= 2 and selected_tokens.issubset(alias_tokens):
            extras = alias_tokens - selected_tokens
            if not (extras & qualifier_words):
                return True

        if selected_core:
            if len(selected_core) >= 2 and selected_core == alias_core:
                return True

            if len(selected_core) >= 2 and selected_core.issubset(alias_core):
                extras = alias_tokens - selected_tokens
                if not (extras & qualifier_words):
                    return True

            if len(selected_core) == 1:
                token = next(iter(selected_core))
                has_letters = bool(re.search(r"[a-zа-я]", token))
                if has_letters and token in alias_core and selected_tokens == alias_tokens:
                    return True

    return False


def format_item_name(item: Dict[str, Any]) -> str:
    detail = str(item.get("detail", "") or "").strip()
    quality = str(item.get("quality", "") or "").strip()
    raw_name = str(item.get("raw_name", "") or "").strip()
    model = str(item.get("model", "") or "").strip()

    parts: List[str] = []
    if detail:
        parts.append(detail)

    detail_norm = normalize_text(detail)
    quality_norm = normalize_text(quality)

    if quality and quality_norm not in detail_norm:
        parts.append(quality)

    if parts:
        return compact_detail_text(" — ".join(parts))

    if quality:
        return compact_detail_text(quality)

    if raw_name and raw_name != model:
        return compact_detail_text(raw_name)

    return compact_detail_text(model or "Вариант детали")


def dedupe_options(options: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    result = []
    for item in options:
        key = (
            item.get("model"),
            item.get("detail"),
            item.get("quality"),
            int(item.get("price", 0)),
            item.get("raw_name"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def get_service_options_for_model(model: str, service_code: str) -> List[Dict[str, Any]]:
    items = service_catalog(service_code).get("items", [])
    if not items:
        return []

    exact = [dict(x) for x in items if model_matches_item(model, x)]

    if not exact:
        selected = normalize_text(model)
        for item in items:
            raw = normalize_text(item.get("raw_name", ""))
            model_norm = normalize_text(item.get("model", ""))
            if model_norm == selected:
                exact.append(dict(item))
                continue
            if raw.startswith(selected + " (") or raw.startswith(selected + " /"):
                exact.append(dict(item))

    exact = dedupe_options(exact)
    if service_code == "battery":
        has_typed = any(str(item.get("quality") or "").strip() for item in exact)
        if has_typed:
            exact = [item for item in exact if str(item.get("quality") or "").strip()]
    for item in exact:
        if service_code == "battery":
            item["label"] = battery_quality_label(item)
        else:
            item["label"] = format_item_name(item)

    exact.sort(key=lambda x: (int(x.get("price", 0)), x.get("label", ""), x.get("quality", "")))
    return exact


def part_bucket_key(service_code: str, quality: str, label: str = "") -> str:
    if service_code == "battery":
        return battery_quality_key({"quality": quality, "label": label, "raw_name": label})

    q = normalize_text(quality)
    l = normalize_text(label)
    text = f"{q} {l}".strip()

    if any(x in text for x in ["оригинал", "сервисный", "снятый"]):
        return "original"
    return "replica"


def bucket_label(bucket: str, service_code: str = "") -> str:
    if service_code == "battery":
        mapping = {
            "original": "Оригинал",
            "copy": "Копия",
            "enhanced": "Повышенная емкость",
            "service_original": "Сервисный оригинал",
            "removed_original": "Снятый оригинал",
            "premium": "Premium",
        }
        return mapping.get(bucket, bucket)

    return "Оригинал" if bucket == "original" else "Реплика"


def get_part_buckets(model: str, service_code: str) -> Dict[str, List[Dict[str, Any]]]:
    options = get_service_options_for_model(model, service_code)

    if service_code == "battery":
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for opt in options:
            key = battery_quality_key(opt)
            buckets.setdefault(key, []).append(opt)

        order = ["original", "copy", "enhanced", "service_original", "removed_original", "premium"]
        ordered = {}
        for key in order:
            if key in buckets:
                ordered[key] = sorted(buckets[key], key=lambda x: (int(x.get("price", 0)), x.get("raw_name", "")))
        for key in buckets:
            if key not in ordered:
                ordered[key] = buckets[key]
        return ordered

    buckets: Dict[str, List[Dict[str, Any]]] = {"original": [], "replica": []}
    for opt in options:
        buckets[part_bucket_key(service_code, str(opt.get("quality", "")), str(opt.get("label", "")))].append(opt)
    return {k: v for k, v in buckets.items() if v}


def bucket_price_label(options: List[Dict[str, Any]]) -> str:
    prices = [int(x["price"]) for x in options if x.get("price") is not None]
    if not prices:
        return "без цены"
    mn, mx = min(prices), max(prices)
    if mn == mx:
        return format_rub(mn)
    return f"от {format_rub(mn)}"


def estimate_price(
    model: str,
    service_code: str,
    selected_part: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[int], Optional[int], str]:
    if selected_part:
        p = int(selected_part["price"])
        return p, p, f"Вариант: {selected_part.get('label') or selected_part.get('quality', '')}"

    if service_has_catalog(service_code):
        options = get_service_options_for_model(model, service_code)
        if options:
            prices = [int(x["price"]) for x in options]
            return min(prices), max(prices), "Цена зависит от выбранной детали"
        return None, None, "Нет цены по этой модели"

    service = SERVICE_BY_CODE.get(service_code)
    if not service:
        return None, None, "Уточним стоимость"
    return int(service.get("base_from", 0)), int(service.get("base_to", 0)), "Ориентировочно"


def battery_quality_key(item: Dict[str, Any]) -> str:
    quality = normalize_text(item.get("quality", ""))
    raw = normalize_text(item.get("raw_name", ""))
    text = f"{quality} {raw}".strip()

    if "повыш" in text or "емк" in text:
        return "enhanced"
    if "сервис" in text:
        return "service_original"
    if "снят" in text:
        return "removed_original"
    if "premium" in text:
        return "premium"
    if "коп" in text:
        return "copy"
    return "original"


def battery_quality_label(item: Dict[str, Any]) -> str:
    key = battery_quality_key(item)
    mapping = {
        "original": "Оригинал",
        "copy": "Копия",
        "enhanced": "Повышенная емкость",
        "service_original": "Сервисный оригинал",
        "removed_original": "Снятый оригинал",
        "premium": "Premium",
    }
    return mapping.get(key, str(item.get("quality") or "").strip() or "Оригинал")


# --- UI builders

def kb_inline(rows: List[List[Tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def back_text_keyboard(label: str = BACK_TEXT) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(label)], [KeyboardButton(HOME_TEXT)]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(CONTACT_TEXT, request_contact=True)],
            [KeyboardButton(BACK_TEXT), KeyboardButton(HOME_TEXT)],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def service_button_title(service_code: str) -> str:
    custom = {
        "diag": "🔎 Диагностика",
        "screen": "📱 Дисплей",
        "battery": "🔋 Аккумулятор",
        "rear_glass": "📦 Корпус / стекло",
        "flex_charge": "🔌 Шлейфы / зарядка",
        "flash_mic": "🎙️ Вспышка / микрофон",
        "speaker": "🔊 Динамики",
        "camera": "📷 Камеры",
        "water": "💧 После воды",
        "software": "🛠️ Прошивка / ПО",
    }
    return custom.get(service_code, service_title(service_code))


def other_kind_keyboard() -> InlineKeyboardMarkup:
    return kb_inline(
        [
            [("📱 Планшет", "otherkind:tablet"), ("💻 Ноутбук", "otherkind:laptop")],
            [("⬅️ Назад", "back")],
        ]
    )


def other_kind_label(kind: str) -> str:
    return {
        "tablet": "Планшет",
        "laptop": "Ноутбук",
    }.get(kind, "Другое устройство")


def compact_detail_text(label: str) -> str:
    text = str(label or "").strip()
    if not text:
        return "Вариант"

    pure_quality_map = {
        "оригинал": "Оригинал",
        "сервисный оригинал": "Сервисный оригинал",
        "снятый оригинал": "Снятый оригинал",
        "копия": "Копия",
        "lcd": "LCD",
        "oled": "OLED",
        "amoled": "AMOLED",
        "premium": "Premium",
        "повышенная емкость": "Повышенная емкость",
        "восстановленный оригинал": "Восстановленный оригинал",
    }
    pure_norm = normalize_text(text)
    if pure_norm in pure_quality_map:
        return pure_quality_map[pure_norm]

    paren_values = [x.strip() for x in re.findall(r"\(([^()]*)\)", text)]
    color = ""
    color_tokens = [
        "black", "white", "blue", "green", "silver", "gold", "gray", "grey",
        "purple", "pink", "red", "yellow", "orange", "coral", "lavender",
        "natural", "desert", "cosmic", "midnight", "starlight", "graphite",
        "sierra", "space", "ultramarine", "teal", "titanium",
        "черн", "бел", "син", "зел", "сер", "золот", "фиолет", "крас",
    ]
    for candidate in reversed(paren_values):
        c_norm = normalize_text(candidate)
        if any(tok in c_norm for tok in color_tokens):
            color = candidate
            break

    color_map = {
        "Natural Titanium": "Natural Ti",
        "White Titanium": "White Ti",
        "Black Titanium": "Black Ti",
        "Blue Titanium": "Blue Ti",
        "Desert Titanium": "Desert Ti",
        "Green Titanium": "Green Ti",
        "Cosmic Orange": "Cosmic Orange",
        "Deep Blue": "Deep Blue",
        "Space Black": "Space Black",
        "Space Gray": "Space Gray",
        "Midnight": "Midnight",
        "Starlight": "Starlight",
        "Lavender": "Lavender",
        "Coral": "Coral",
        "Сoral": "Coral",
    }
    if color:
        color = color_map.get(color, color)

    replacements = [
        ("Задняя крышка (стекло) в сборе с рамкой", "Стекло+рамка"),
        ("Задняя крышка (стекло) в комплекте со стеклом камеры", "Стекло+камера"),
        ("Задняя крышка (стекло)", "Заднее стекло"),
        ("Средняя часть корпуса с кнопками Алюминиевая", "Средняя часть"),
        ("Средняя часть корпуса с кнопками", "Средняя часть"),
        ("Корпус в сборе", "Корпус"),
        ("Основная (Задняя) камера", "Основная камера"),
        ("Основная (задняя) камера", "Основная камера"),
        ("Фронтальная камера", "Фронтальная камера"),
        ("Шлейф вспышки и микрофона + беспроводная зарядка", "Шлейф: вспышка+микрофон+Qi"),
        ("Шлейф вспышки и беспроводной зарядки и микрофон", "Шлейф: вспышка+микрофон+Qi"),
        ("Шлейф вспышки и микрофона", "Шлейф: вспышка+микрофон"),
        ("Шлейф кнопок регулировки громкости и беспроводной зарядки", "Шлейф: volume+Qi"),
        ("Шлейф кнопки включения + кнопок громкости и беспроводная зарядка", "Шлейф: power/vol+Qi"),
        ("Шлейф кнопки включения + кнопок громкости", "Шлейф: power/vol"),
        ("Шлейф беспроводной зарядки", "Шлейф: Qi"),
        ("Шлейф с разъемом зарядки", "Шлейф зарядки"),
        ("Шлейф разъема зарядки", "Шлейф зарядки"),
        ("Полифонический динамик", "Нижний динамик"),
        ("Слуховой динамик", "Верхний динамик"),
        ("Лидар", "LiDAR"),
        ("Шлейф Lidar", "Шлейф LiDAR"),
    ]
    for old, new in replacements:
        text = text.replace(old, new)

    text = re.sub(r"\([^()]*\)", "", text)
    text = re.sub(r"\b(Оригинал|Копия|Сервисный|Снятый|Фото|CE|VoltPack|Дополнительный)\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" -—•")

    short_map = {
        "Фронтальная камера": "Фронт. камера",
        "Основная камера": "Осн. камера",
        "Верхний динамик": "Верхний дин.",
        "Нижний динамик": "Нижний дин.",
    }
    for old, new in short_map.items():
        text = text.replace(old, new)

    text = text.replace(" + ", "+")
    text = text.replace(" / ", "/")
    text = re.sub(r"\s+", " ", text).strip(" -—•")

    if color and color.lower() not in text.lower():
        text = f"{text} • {color}"

    if len(text) > 34:
        text = text.replace("Шлейф:", "Шлейф")
    if len(text) > 34:
        text = text[:31].rstrip(" -—•") + "…"

    return text or "Вариант"


def compact_part_button_text(opt: Dict[str, Any], include_price: bool = True) -> str:
    label = compact_detail_text(str(opt.get("label", "")))
    if not include_price:
        return label

    price = format_rub(int(opt["price"]))
    text = f"{label} • {price}"
    if len(text) > 44:
        text = f"{label[:24].rstrip()}… • {price}"
    return text


def bucket_has_same_price(options: List[Dict[str, Any]]) -> bool:
    prices = {int(x.get("price", 0)) for x in options if x.get("price") is not None}
    return len(prices) == 1


def request_title(context: ContextTypes.DEFAULT_TYPE) -> str:
    flow = str(context.user_data.get("flow") or "")
    if flow == "other":
        return "Ремонт другого устройства"
    if flow == "consult":
        return "Консультация"
    service = context.user_data.get("service", "")
    return service_title(service)


def request_device_label(context: ContextTypes.DEFAULT_TYPE) -> str:
    flow = str(context.user_data.get("flow") or "")
    if flow == "other":
        kind = str(context.user_data.get("other_kind") or "")
        model = str(context.user_data.get("model") or "").strip()
        base = other_kind_label(kind)
        return f"{base}: {model}" if model else base
    return str(context.user_data.get("model") or "—")


def has_prefilled_phone(context: ContextTypes.DEFAULT_TYPE) -> bool:
    phone = normalize_phone(str(context.user_data.get("phone") or ""))
    return bool(re.match(r"^\+7\d{10}$", phone))


def should_skip_date_step(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return str(context.user_data.get("flow") or "") == "other" or bool(context.user_data.get("site_prefilled"))


def go_home_requested(text: str) -> bool:
    return (text or "").strip().lower() in {HOME_TEXT.lower(), "/start", "в начало", "главное меню"}


def support_keyboard(return_to: str) -> InlineKeyboardMarkup:
    if return_to == "confirm":
        label = "⬅️ Вернуться к заявке"
    elif return_to == "submitted":
        label = "🏠 Новая заявка"
    else:
        label = "⬅️ К выбору услуги"
    return kb_inline([[(label, "support:back")]])


def services_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for s in SERVICES:
        rows.append([(service_button_title(s["code"]), f"svc:{s['code']}")])
    rows.append([("💬 Написать мастеру", "support"), ("⬅️ Назад", "back")])
    return kb_inline(rows)


def device_keyboard() -> InlineKeyboardMarkup:
    return kb_inline(
        [
            [("📱 iPhone", "dev:iphone"), ("🤖 Android", "dev:android")],
            [("💻 Другие устройства", "dev:other")],
            [("💬 Консультация", "consult")],
        ]
    )


def popular_models(device: str) -> List[str]:
    if device == "iphone":
        return [
            "iPhone 17",
            "iPhone 16",
            "iPhone 16 Pro",
            "iPhone 15",
            "iPhone 15 Pro",
            "iPhone 14",
            "iPhone 13",
            "iPhone 12",
            "iPhone 11",
            "iPhone X",
        ]
    return [
        "Samsung A54",
        "Samsung S23",
        "Xiaomi Redmi Note 12",
        "Xiaomi 13",
        "Huawei P40",
        "Honor 90",
    ]


def model_keyboard(device: str) -> InlineKeyboardMarkup:
    if device == "other":
        return other_kind_keyboard()

    models = popular_models(device)
    rows = []
    for m in models:
        rows.append([(m, f"model:{m}")])
    rows.append([("✍️ Ввести модель вручную", "model:manual"), ("⬅️ Назад", "back")])
    return kb_inline(rows)


def model_suggestions_keyboard(models: List[str]) -> InlineKeyboardMarkup:
    rows = [[(m, f"model:{m}")] for m in models[:6]]
    rows.append([("✍️ Ввести ещё раз", "model:manual"), ("⬅️ Назад", "back")])
    return kb_inline(rows)


def part_type_keyboard(model: str, service_code: str) -> InlineKeyboardMarkup:
    buckets = get_part_buckets(model, service_code)
    rows = []
    for bucket, options in buckets.items():
        rows.append(
            [
                (
                    f"{bucket_label(bucket, service_code)} — {bucket_price_label(options)}",
                    f"parttype:{bucket}",
                )
            ]
        )
    rows.append([("⬅️ Назад", "back")])
    return kb_inline(rows)


def part_options_keyboard(model: str, service_code: str, bucket: str) -> InlineKeyboardMarkup:
    options = get_part_buckets(model, service_code).get(bucket, [])
    same_price = bucket_has_same_price(options)
    rows = []
    for idx, opt in enumerate(options):
        rows.append(
            [
                (
                    compact_part_button_text(opt, include_price=not same_price),
                    f"partopt:{bucket}:{idx}",
                )
            ]
        )
    rows.append([("⬅️ Назад", "back")])
    return kb_inline(rows)


def date_keyboard() -> InlineKeyboardMarkup:
    today = date.today()
    days = [today + timedelta(days=i) for i in range(0, 14)]
    rows = []
    for i in range(0, 14, 2):
        row = []
        for d in days[i: i + 2]:
            row.append((d.strftime("%d.%m"), f"date:{d.isoformat()}"))
        rows.append(row)
    rows.append([("⬅️ Назад", "back")])
    return kb_inline(rows)


def time_keyboard() -> InlineKeyboardMarkup:
    times = [
        "10:00",
        "11:00",
        "12:00",
        "13:00",
        "14:00",
        "15:00",
        "16:00",
        "17:00",
        "18:00",
        "19:00",
        "20:00",
    ]
    rows = []
    for i in range(0, len(times), 3):
        rows.append([(t, f"time:{t}") for t in times[i: i + 3]])
    rows.append([("✍️ Другое время", "time:manual"), ("⬅️ Назад", "back")])
    return kb_inline(rows)


# --- Unified selection cards

def next_step_hint(step: str) -> str:
    hints = {
        "choose_part_type": "👇 Выберите категорию детали:",
        "choose_part_option": "👇 Выберите точный вариант детали:",
        "share_phone": "👇 Оставьте номер телефона для связи:",
        "choose_time": "👇 Выберите удобное время:",
        "choose_date": "👇 Выберите удобную дату:",
    }
    return hints.get(step, "")


def price_html_for_current_selection(context: ContextTypes.DEFAULT_TYPE) -> str:
    model = context.user_data.get("model", "")
    service = context.user_data.get("service", "")
    part = context.user_data.get("part")
    mn, mx, _ = estimate_price(model, service, part)
    return format_price_range(mn, mx)


def current_selection_html(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    step: str,
    show_price: bool = True,
    show_part: bool = True,
    extra_note: str = "",
) -> str:
    flow = str(context.user_data.get("flow") or "")
    model = str(context.user_data.get("model") or "—")
    service = str(context.user_data.get("service") or "")
    part = context.user_data.get("part")

    lines: List[str] = ["✨ <b>Проверьте выбор</b>", ""]

    if flow == "other":
        lines.append(f"📦 <b>Тип устройства:</b> {esc(other_kind_label(str(context.user_data.get('other_kind') or '')))}")
        lines.append(f"📱 <b>Модель:</b> {esc(model)}")
        problem = str(context.user_data.get("problem") or "").strip()
        if problem:
            lines.append(f"📝 <b>Проблема:</b> {esc(problem)}")
    elif flow == "consult":
        lines.append("💬 <b>Консультация</b>")
        lines.append(f"📱 <b>Устройство / вопрос:</b> {esc(model)}")
    else:
        lines.append(f"📱 <b>Устройство:</b> {esc(model)}")
        lines.append(f"🔧 <b>Услуга:</b> {esc(request_title(context))}")

    if show_part and part:
        part_name = part.get("label") if service == "battery" else compact_detail_text(part.get("label", ""))
        lines.append(f"🧩 <b>Деталь:</b> {esc(part_name)}")

    if show_price and flow not in {"other", "consult"}:
        lines.append(f"💰 <b>Стоимость:</b> {esc(price_html_for_current_selection(context))}")

    if context.user_data.get("phone") and step in {"choose_time", "choose_date"}:
        lines.append(f"📞 <b>Телефон:</b> {esc(context.user_data.get('phone'))}")

    if extra_note:
        lines.append("")
        lines.append(extra_note)

    hint = next_step_hint(step)
    if hint:
        lines.append("")
        lines.append(hint)

    return "\n".join(lines)


# --- Step helpers

def is_back_text(value: str) -> bool:
    text = (value or "").strip().lower()
    return text in {BACK_TEXT.lower(), "назад", "/back"}


def selected_service_html(context: ContextTypes.DEFAULT_TYPE) -> str:
    flow = str(context.user_data.get("flow") or "")
    if flow == "other":
        kind = other_kind_label(str(context.user_data.get("other_kind") or ""))
        return (
            "💻 <b>Другое устройство</b>\n"
            f"📦 <b>Тип:</b> {esc(kind)}\n"
            f"📱 <b>Модель:</b> {esc(context.user_data.get('model', '—'))}"
        )

    model = context.user_data.get("model", "—")
    sname = request_title(context)
    return f"🔧 <b>{esc(sname)}</b>\n📱 <b>Модель:</b> {esc(model)}"


def clear_part_selection(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("part", None)
    context.user_data.pop("part_bucket_selected", None)
    context.user_data.pop("part_has_bucket_menu", None)
    context.user_data.pop("phone_back_target", None)


def set_phone_back_target(context: ContextTypes.DEFAULT_TYPE, target: str) -> None:
    context.user_data["phone_back_target"] = target


def get_phone_back_target(context: ContextTypes.DEFAULT_TYPE) -> str:
    return str(context.user_data.get("phone_back_target") or "service")


def set_support_return(context: ContextTypes.DEFAULT_TYPE, return_to: str) -> None:
    context.user_data["mode"] = "support"
    context.user_data["support_return_to"] = return_to


def get_support_return(context: ContextTypes.DEFAULT_TYPE) -> str:
    return str(context.user_data.get("support_return_to") or "service")


def build_lead_record(context: ContextTypes.DEFAULT_TYPE, user) -> Dict[str, Any]:
    model = context.user_data.get("model", "—")
    service = context.user_data.get("service", "")
    part = context.user_data.get("part")
    phone = context.user_data.get("phone", "—")
    booking_date = context.user_data.get("date", "")
    booking_time = context.user_data.get("time", "")
    comment = (context.user_data.get("problem") or "").strip()
    mn, mx, _ = estimate_price(model, service or "diag", part)

    lead_id = f"RB-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{user.id}"
    return {
        "lead_id": lead_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "user_id": user.id,
        "username": user.username or "",
        "site_tg": context.user_data.get("site_tg", ""),
        "flow": context.user_data.get("flow", ""),
        "device": context.user_data.get("device", ""),
        "device_label": request_device_label(context),
        "other_kind": context.user_data.get("other_kind", ""),
        "model": model,
        "service_code": service,
        "service_title": request_title(context),
        "part_label": (part or {}).get("label", "") if part else "",
        "part_quality": (part or {}).get("quality", "") if part else "",
        "part_price": int((part or {}).get("price", 0)) if part and part.get("price") is not None else None,
        "price_from": mn,
        "price_to": mx,
        "price_text": format_price_range(mn, mx),
        "phone": phone,
        "date": booking_date,
        "time": booking_time,
        "comment": comment,
    }


def save_lead(record: Dict[str, Any]) -> str:
    leads: List[Dict[str, Any]] = []
    if LEADS_FILE.exists():
        try:
            current = json.loads(LEADS_FILE.read_text(encoding="utf-8"))
            if isinstance(current, list):
                leads = current
        except Exception:
            logger.exception("Could not read existing leads file")

    leads.append(record)
    LEADS_FILE.write_text(json.dumps(leads, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "LEAD_SAVED id=%s source=%s model=%s service=%s part=%s phone=%s",
        record.get("lead_id", ""),
        "website" if record.get("site_tg") or "Заявка с сайта" in str(record.get("comment") or "") else "telegram",
        record.get("model", ""),
        record.get("service_code", ""),
        record.get("part_label", ""),
        record.get("phone", ""),
    )
    return str(record["lead_id"])


def booking_summary_html(context: ContextTypes.DEFAULT_TYPE) -> str:
    model = context.user_data.get("model", "—")
    phone = context.user_data.get("phone", "—")
    booking_date = context.user_data.get("date", "")
    booking_time = context.user_data.get("time", "")
    comment = (context.user_data.get("problem") or "").strip()
    part = context.user_data.get("part")
    service = context.user_data.get("service", "")
    mn, mx, _ = estimate_price(model, service or "diag", part)
    price_line = format_price_range(mn, mx)
    flow = str(context.user_data.get("flow") or "")

    date_str = booking_date
    try:
        date_str = datetime.fromisoformat(booking_date).strftime("%d.%m.%Y")
    except Exception:
        pass

    txt = "🧾 <b>Проверьте заявку</b>\n\n"
    if flow == "other":
        txt += (
            f"📦 <b>Устройство:</b> {esc(other_kind_label(str(context.user_data.get('other_kind') or '')))}\n"
            f"📱 <b>Модель:</b> {esc(model)}\n"
            "🔧 <b>Запрос:</b> Мастер свяжется и уточнит стоимость\n"
        )
    elif flow == "consult":
        txt += "💬 <b>Консультация</b>\n"
        txt += f"📝 <b>Вопрос:</b> {esc(comment or model)}\n"
    else:
        txt += (
            f"📱 <b>Модель:</b> {esc(model)}\n"
            f"🔧 <b>Услуга:</b> {esc(request_title(context))}\n"
        )
    if part:
        txt += f"🧩 <b>Деталь:</b> {esc(compact_detail_text(part.get('label') or part.get('quality', '')))}\n"
    if flow not in {"other", "consult"}:
        txt += f"💰 <b>Стоимость:</b> {esc(price_line)}\n"
    txt += f"📞 <b>Телефон:</b> {esc(phone)}\n"
    if context.user_data.get("site_tg"):
        txt += f"✈️ <b>Telegram:</b> {esc(context.user_data['site_tg'])}\n"
    if should_skip_date_step(context):
        txt += f"⏰ <b>Когда удобно связаться:</b> {esc(booking_time)}\n"
    else:
        txt += f"🗓️ <b>Когда удобно:</b> {esc(date_str)} {esc(booking_time)}\n"
    if comment and flow != "consult":
        txt += f"📝 <b>Комментарий:</b> {esc(comment)}\n"
    txt += "\nЕсли всё верно, отправьте заявку мастеру."
    return txt


def build_admin_text(context: ContextTypes.DEFAULT_TYPE, user, lead_id: str) -> str:
    model = context.user_data.get("model", "—")
    service = context.user_data.get("service", "")
    phone = context.user_data.get("phone", "—")
    booking_date = context.user_data.get("date", "")
    booking_time = context.user_data.get("time", "")
    comment = (context.user_data.get("problem") or "").strip()
    part = context.user_data.get("part")
    mn, mx, _ = estimate_price(model, service or "diag", part)
    price_line = format_price_range(mn, mx)
    flow = str(context.user_data.get("flow") or "")

    date_str = booking_date
    try:
        date_str = datetime.fromisoformat(booking_date).strftime("%d.%m.%Y")
    except Exception:
        pass

    uname = f"@{user.username}" if user and user.username else (str(context.user_data.get("site_tg") or "") or f"id:{user.id}")

    txt = (
        "📥 <b>Новая заявка из бота</b>\n\n"
        f"🆔 <b>Номер заявки:</b> <code>{esc(lead_id)}</code>\n"
        f"👤 <b>Клиент:</b> {esc(uname)}\n"
    )
    if flow == "other":
        txt += (
            f"📦 <b>Тип устройства:</b> {esc(other_kind_label(str(context.user_data.get('other_kind') or '')))}\n"
            f"📱 <b>Модель:</b> {esc(model)}\n"
            "🔧 <b>Запрос:</b> Другое устройство / мастер уточняет стоимость\n"
        )
    elif flow == "consult":
        txt += "💬 <b>Тип:</b> Консультация\n"
        txt += f"📝 <b>Вопрос:</b> {esc(comment or model)}\n"
    else:
        txt += (
            f"📱 <b>Модель:</b> {esc(model)}\n"
            f"🔧 <b>Услуга:</b> {esc(request_title(context))}\n"
        )
    if part:
        txt += (
            f"🧩 <b>Деталь:</b> {esc(compact_detail_text(part.get('label') or part.get('quality', '')))}"
            f" ({esc(format_rub(int(part.get('price', 0))))})\n"
        )
    if flow not in {"other", "consult"}:
        txt += f"💰 <b>Стоимость:</b> {esc(price_line)}\n"
    txt += f"📞 <b>Телефон:</b> {esc(phone)}\n"
    if context.user_data.get("site_tg"):
        txt += f"✈️ <b>Telegram:</b> {esc(context.user_data['site_tg'])}\n"
    if should_skip_date_step(context):
        txt += f"⏰ <b>Когда удобно связаться:</b> {esc(booking_time)}\n"
    else:
        txt += f"🗓️ <b>Когда удобно:</b> {esc(date_str)} {esc(booking_time)}\n"
    txt += f"🆔 <b>UserID:</b> <code>{user.id}</code>"
    if comment and flow != "consult":
        txt += f"\n📝 <b>Комментарий:</b> {esc(comment)}"
    return txt


async def show_support_entry_from_callback(q, context: ContextTypes.DEFAULT_TYPE, return_to: str) -> int:
    set_support_return(context, return_to)
    caption = {
        "confirm": "После ответа мастера вы сможете вернуться к заявке и подтвердить её.",
        "submitted": "Можете отправить мастеру дополнительный комментарий по уже созданной заявке.",
        "service": "Я передам сообщение мастеру, а затем вы сможете вернуться к выбору услуги.",
    }.get(return_to, "Я передам сообщение мастеру.")
    await q.edit_message_text(
        "💬 <b>Сообщение мастеру</b>\n\n"
        "Напишите всё одним сообщением: вопрос, уточнение или пожелание по ремонту.\n\n"
        f"{caption}",
        parse_mode=ParseMode.HTML,
        reply_markup=support_keyboard(return_to),
    )
    return S_SUPPORT


async def restore_from_support_callback(q, context: ContextTypes.DEFAULT_TYPE) -> int:
    return_to = get_support_return(context)
    context.user_data.pop("mode", None)
    context.user_data.pop("support_return_to", None)

    if return_to == "confirm":
        return await show_confirm(q, context)

    if return_to == "submitted":
        context.user_data.clear()
        await q.edit_message_text(
            "👋 <b>ReBootFix</b>\n\nВыберите, что хотите сделать:",
            parse_mode=ParseMode.HTML,
            reply_markup=device_keyboard(),
        )
        return S_DEVICE

    await q.edit_message_text("Выберите нужную услугу:", reply_markup=services_keyboard())
    return S_SERVICE


async def restore_phone_back_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    target = get_phone_back_target(context)
    if target.startswith("part_options:"):
        bucket = target.split(":", 1)[1]
        context.user_data["part_bucket_selected"] = bucket
        context.user_data["part_has_bucket_menu"] = True
        context.user_data.pop("part", None)
        await update.effective_chat.send_message(
            current_selection_html(context, step="choose_part_option", show_part=False),
            parse_mode=ParseMode.HTML,
            reply_markup=part_options_keyboard(
                context.user_data.get("model", ""),
                context.user_data.get("service", ""),
                bucket,
            ),
        )
        return S_PART_CHOICE

    if target == "problem":
        await update.effective_chat.send_message(
            "💬 <b>Консультация</b>\n\nНапишите модель устройства и коротко опишите вопрос.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_text_keyboard(),
        )
        return S_PROBLEM

    if target == "other_problem":
        await update.effective_chat.send_message(
            "📝 Опишите проблему ещё раз одним сообщением.",
            reply_markup=back_text_keyboard(),
        )
        return S_PROBLEM

    if target == "part_type":
        clear_part_selection(context)
        await update.effective_chat.send_message(
            current_selection_html(context, step="choose_part_type", show_part=False),
            parse_mode=ParseMode.HTML,
            reply_markup=part_type_keyboard(
                context.user_data.get("model", ""),
                context.user_data.get("service", ""),
            ),
        )
        return S_PART_CHOICE

    clear_part_selection(context)
    await update.effective_chat.send_message("Выберите нужную услугу:", reply_markup=services_keyboard())
    return S_SERVICE


async def ask_for_part_selection_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    model = context.user_data.get("model", "")
    service = context.user_data.get("service", "")
    options = get_service_options_for_model(model, service)
    buckets = get_part_buckets(model, service)

    if not options:
        set_phone_back_target(context, "service")
        await update.effective_chat.send_message(
            current_selection_html(
                context,
                step="share_phone",
                extra_note="ℹ️ По этой модели точной цены пока нет. Мастер уточнит стоимость после проверки устройства.",
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=phone_keyboard(),
        )
        return S_PHONE

    if len(options) == 1:
        chosen = options[0]
        context.user_data["part"] = chosen
        context.user_data.pop("part_bucket_selected", None)
        context.user_data.pop("part_has_bucket_menu", None)
        set_phone_back_target(context, "service")

        await update.effective_chat.send_message(
            current_selection_html(context, step="share_phone"),
            parse_mode=ParseMode.HTML,
            reply_markup=phone_keyboard(),
        )
        return S_PHONE

    if len(buckets) > 1:
        context.user_data.pop("part_bucket_selected", None)
        context.user_data.pop("part_has_bucket_menu", None)
        await update.effective_chat.send_message(
            current_selection_html(context, step="choose_part_type", show_part=False),
            parse_mode=ParseMode.HTML,
            reply_markup=part_type_keyboard(model, service),
        )
        return S_PART_CHOICE

    only_bucket = next(iter(buckets))
    context.user_data["part_bucket_selected"] = only_bucket
    context.user_data["part_has_bucket_menu"] = False

    note = ""
    bucket_options = get_part_buckets(model, service).get(only_bucket, [])
    if bucket_has_same_price(bucket_options):
        note = f"💡 Все варианты в этой категории стоят <b>{esc(bucket_price_label(bucket_options))}</b>."

    await update.effective_chat.send_message(
        current_selection_html(
            context,
            step="choose_part_option",
            show_part=False,
            extra_note=note,
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=part_options_keyboard(model, service, only_bucket),
    )
    return S_PART_CHOICE


async def ask_for_part_selection_from_callback(q, context: ContextTypes.DEFAULT_TYPE) -> int:
    model = context.user_data.get("model", "")
    service = context.user_data.get("service", "")
    options = get_service_options_for_model(model, service)
    buckets = get_part_buckets(model, service)

    if not options:
        set_phone_back_target(context, "service")
        await q.edit_message_text(
            current_selection_html(
                context,
                step="share_phone",
                extra_note="ℹ️ По этой модели точной цены пока нет. Мастер уточнит стоимость после проверки устройства.",
            ),
            parse_mode=ParseMode.HTML,
        )
        await q.message.reply_text(
            "📞 Оставьте номер телефона, чтобы мастер связался с вами:",
            reply_markup=phone_keyboard(),
        )
        return S_PHONE

    if len(options) == 1:
        chosen = options[0]
        context.user_data["part"] = chosen
        context.user_data.pop("part_bucket_selected", None)
        context.user_data.pop("part_has_bucket_menu", None)
        set_phone_back_target(context, "service")

        await q.edit_message_text(
            current_selection_html(context, step="share_phone"),
            parse_mode=ParseMode.HTML,
        )
        await q.message.reply_text(
            "📞 Оставьте номер телефона, чтобы подтвердить запись:",
            reply_markup=phone_keyboard(),
        )
        return S_PHONE

    if len(buckets) > 1:
        context.user_data.pop("part_bucket_selected", None)
        context.user_data.pop("part_has_bucket_menu", None)
        await q.edit_message_text(
            current_selection_html(context, step="choose_part_type", show_part=False),
            parse_mode=ParseMode.HTML,
            reply_markup=part_type_keyboard(model, service),
        )
        return S_PART_CHOICE

    only_bucket = next(iter(buckets))
    context.user_data["part_bucket_selected"] = only_bucket
    context.user_data["part_has_bucket_menu"] = False

    note = ""
    bucket_options = get_part_buckets(model, service).get(only_bucket, [])
    if bucket_has_same_price(bucket_options):
        note = f"💡 Все варианты в этой категории стоят <b>{esc(bucket_price_label(bucket_options))}</b>."

    await q.edit_message_text(
        current_selection_html(
            context,
            step="choose_part_option",
            show_part=False,
            extra_note=note,
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=part_options_keyboard(model, service, only_bucket),
    )
    return S_PART_CHOICE


async def proceed_after_service_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    service = context.user_data.get("service", "")

    if service_has_catalog(service):
        return await ask_for_part_selection_from_message(update, context)

    set_phone_back_target(context, "service")
    await update.effective_chat.send_message(
        current_selection_html(context, step="share_phone"),
        parse_mode=ParseMode.HTML,
        reply_markup=phone_keyboard(),
    )
    return S_PHONE


async def proceed_after_service_from_callback(q, context: ContextTypes.DEFAULT_TYPE) -> int:
    service = context.user_data.get("service", "")

    if service_has_catalog(service):
        return await ask_for_part_selection_from_callback(q, context)

    set_phone_back_target(context, "service")
    await q.edit_message_text(
        current_selection_html(context, step="share_phone"),
        parse_mode=ParseMode.HTML,
    )
    await q.message.reply_text(
        "📞 Нажмите кнопку ниже или отправьте номер в формате +79991234567",
        reply_markup=phone_keyboard(),
    )
    return S_PHONE


# --- Handlers

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    payload = " ".join(context.args) if context.args else ""
    data = parse_start_payload(payload)
    logger.info("START payload=%s parsed=%s", payload, data)
    context.user_data.clear()

    if data.get("mode") == "book":
        info = site_model_info(data.get("site_code", "")) if data.get("site_code") else {}
        context.user_data["flow"] = "repair"
        context.user_data["device"] = normalize_device(str(info.get("device") or data.get("device", "")))
        context.user_data["model"] = str(info.get("model") or data.get("model", "")).strip()
        context.user_data["service"] = str(data.get("service") or site_service_short_to_key(data.get("service_short", ""))).strip()
        if not context.user_data.get("model"):
            await update.effective_chat.send_message(
                "Не удалось определить модель с сайта. Выберите устройство заново:",
                reply_markup=device_keyboard(),
            )
            return S_DEVICE
        if not context.user_data.get("service"):
            await update.effective_chat.send_message(
                site_prefill_intro_html(context) + "\n\nВыберите нужную услугу:",
                parse_mode=ParseMode.HTML,
                reply_markup=services_keyboard(),
            )
            return S_SERVICE
        return await proceed_after_service_from_message(update, context)

    if data.get("mode") == "site_order":
        info = site_model_info(data.get("code", ""))
        if info:
            device = str(info.get("device") or "")
            other_kind = str(info.get("other_kind") or "")
            context.user_data["model"] = str(info.get("model") or "").strip()
            context.user_data["site_prefilled"] = True
            context.user_data["site_tg"] = normalize_tg_handle(data.get("tg", ""))
            context.user_data["phone"] = normalize_phone(data.get("phone", ""))
            context.user_data["site_problem_label"] = site_problem_label(data.get("problem", ""))
            context.user_data["problem"] = f"Заявка с сайта. Проблема: {context.user_data['site_problem_label']}"
            if context.user_data.get("site_tg"):
                context.user_data["problem"] += f". Telegram: {context.user_data['site_tg']}"

            logger.info(
                "WEB_ORDER_OPENED model=%s problem=%s phone=%s tg=%s",
                context.user_data.get("model", ""),
                context.user_data.get("site_problem_label", ""),
                context.user_data.get("phone", ""),
                context.user_data.get("site_tg", ""),
            )

            if device == "other" or other_kind:
                context.user_data["flow"] = "other"
                context.user_data["device"] = "other"
                context.user_data["other_kind"] = other_kind or "tablet"
                context.user_data["service"] = ""
                await update.effective_chat.send_message(
                    current_selection_html(context, step="choose_time", show_price=False, show_part=False),
                    parse_mode=ParseMode.HTML,
                    reply_markup=time_keyboard(),
                )
                return S_TIME

            context.user_data["flow"] = "repair"
            context.user_data["device"] = normalize_device(device)
            context.user_data["service"] = site_problem_to_service(data.get("problem", ""))

            return await proceed_after_service_from_message(update, context)

    if data.get("mode") == "consult":
        context.user_data["flow"] = "consult"
        await update.effective_chat.send_message(
            "💬 <b>Консультация</b>\n\nНапишите модель устройства и коротко опишите вопрос.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_text_keyboard(),
        )
        return S_PROBLEM

    greeting = (
        "👋 <b>ReBootFix</b>\n\n"
        "Помогу быстро оформить заявку на ремонт, показать цену и передать всё мастеру.\n"
        "Выберите устройство:"
    )
    await update.effective_chat.send_message(
        greeting, parse_mode=ParseMode.HTML, reply_markup=device_keyboard()
    )
    return S_DEVICE


async def on_device(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    if q.data == "consult":
        context.user_data.clear()
        context.user_data["flow"] = "consult"
        await q.edit_message_text(
            "💬 <b>Консультация</b>\n\n"
            "Напишите модель устройства и коротко опишите вопрос — я передам сообщение мастеру.",
            parse_mode=ParseMode.HTML,
        )
        await q.message.reply_text(
            "Ниже есть кнопка, чтобы вернуться назад или сразу открыть главное меню.",
            reply_markup=back_text_keyboard(),
        )
        return S_PROBLEM

    if q.data.startswith("dev:"):
        device = q.data.split(":", 1)[1]
        context.user_data["device"] = device
        context.user_data["flow"] = "other" if device == "other" else "repair"
        if device == "other":
            await q.edit_message_text(
                "💻 <b>Другие устройства</b>\n\nВыберите тип устройства:",
                parse_mode=ParseMode.HTML,
                reply_markup=model_keyboard(device),
            )
            return S_MODEL

        await q.edit_message_text(
            "Выберите модель из списка или введите её вручную:", reply_markup=model_keyboard(device)
        )
        return S_MODEL

    return S_DEVICE


async def on_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    if q.data == "back":
        await q.edit_message_text("Выберите устройство:", reply_markup=device_keyboard())
        return S_DEVICE

    if q.data.startswith("otherkind:"):
        kind = q.data.split(":", 1)[1]
        context.user_data["other_kind"] = kind
        context.user_data["await_model_manual"] = True
        await q.edit_message_text(
            f"✍️ Напишите модель устройства.\nНапример: <b>{'iPad Pro 11' if kind == 'tablet' else 'MacBook Air M1'}</b>.",
            parse_mode=ParseMode.HTML,
        )
        await q.message.reply_text(
            "Можно вернуться назад или открыть главное меню кнопками ниже.",
            reply_markup=back_text_keyboard(),
        )
        return S_MODEL

    if q.data == "model:manual":
        context.user_data["await_model_manual"] = True
        await q.edit_message_text(
            "✍️ Напишите модель устройства одним сообщением.\nНапример: <b>iPhone 16 Pro</b> или <b>Samsung A54</b>.",
            parse_mode=ParseMode.HTML,
        )
        await q.message.reply_text(
            "Можно вернуться назад или открыть главное меню кнопками ниже.",
            reply_markup=back_text_keyboard(),
        )
        return S_MODEL

    if q.data.startswith("model:"):
        model = q.data.split(":", 1)[1]
        context.user_data.pop("await_model_manual", None)
        context.user_data["model"] = model
        if str(context.user_data.get("flow") or "") == "other":
            await q.edit_message_text(
                "📝 Опишите проблему в одном сообщении.\nНапример: <b>не включается</b>, <b>нет изображения</b> или <b>сильно греется</b>.",
                parse_mode=ParseMode.HTML,
            )
            await q.message.reply_text(
                "Можно вернуться назад или открыть главное меню кнопками ниже.",
                reply_markup=back_text_keyboard(),
            )
            return S_PROBLEM

        await q.edit_message_text("Выберите нужную услугу:", reply_markup=services_keyboard())
        return S_SERVICE

    return S_MODEL


async def on_model_manual_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.user_data.get("await_model_manual"):
        return S_MODEL

    text = (update.message.text or "").strip()
    if go_home_requested(text):
        context.user_data.clear()
        await update.message.reply_text("Открыл главное меню.", reply_markup=ReplyKeyboardRemove())
        await update.effective_chat.send_message("Выберите устройство:", reply_markup=device_keyboard())
        return S_DEVICE

    if is_back_text(text):
        context.user_data.pop("await_model_manual", None)
        device = context.user_data.get("device", "iphone")
        context.user_data.pop("model", None)
        await update.message.reply_text(
            "Вернул на предыдущий шаг.", reply_markup=ReplyKeyboardRemove()
        )
        if device == "other":
            await update.effective_chat.send_message(
                "💻 Выберите тип устройства:", reply_markup=model_keyboard(device)
            )
        else:
            await update.effective_chat.send_message(
                "Выберите модель из списка или введите её вручную:", reply_markup=model_keyboard(device)
            )
        return S_MODEL

    if len(text) < 2:
        await update.message.reply_text("Напишите модель чуть точнее, пожалуйста.")
        return S_MODEL

    flow = str(context.user_data.get("flow") or "")
    device = str(context.user_data.get("device") or "android")

    if flow != "other":
        resolved, candidates = resolve_model_name(device, text)
        if resolved:
            context.user_data.pop("await_model_manual", None)
            context.user_data["model"] = resolved
            await update.message.reply_text(
                f"Нашёл модель: <b>{esc(resolved)}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=ReplyKeyboardRemove(),
            )
            await update.effective_chat.send_message("Выберите нужную услугу:", reply_markup=services_keyboard())
            return S_SERVICE

        if candidates:
            await update.message.reply_text(
                "Похоже, вы имели в виду одну из этих моделей. Выберите свою:",
                reply_markup=ReplyKeyboardRemove(),
            )
            await update.effective_chat.send_message(
                "Подобрал похожие варианты:",
                reply_markup=model_suggestions_keyboard(candidates),
            )
            return S_MODEL

    context.user_data.pop("await_model_manual", None)
    context.user_data["model"] = text
    await update.message.reply_text("Отлично, модель записал.", reply_markup=ReplyKeyboardRemove())

    if flow == "other":
        await update.effective_chat.send_message(
            "📝 Опишите проблему в одном сообщении.\nНапример: <b>не включается</b>, <b>нет изображения</b> или <b>сильно греется</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_text_keyboard(),
        )
        return S_PROBLEM

    await update.effective_chat.send_message("Выберите нужную услугу:", reply_markup=services_keyboard())
    return S_SERVICE


async def on_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    if q.data == "back":
        device = context.user_data.get("device", "iphone")
        await q.edit_message_text(
            "Выберите модель или введите её вручную:", reply_markup=model_keyboard(device)
        )
        return S_MODEL

    if q.data == "support":
        return await show_support_entry_from_callback(q, context, "service")

    if q.data.startswith("svc:"):
        context.user_data["service"] = q.data.split(":", 1)[1]
        clear_part_selection(context)
        return await proceed_after_service_from_callback(q, context)

    return S_SERVICE


async def on_problem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    flow = str(context.user_data.get("flow") or "")

    if go_home_requested(text):
        context.user_data.clear()
        await update.message.reply_text("Открыл главное меню.", reply_markup=ReplyKeyboardRemove())
        await update.effective_chat.send_message("Выберите устройство:", reply_markup=device_keyboard())
        return S_DEVICE

    if is_back_text(text):
        if flow == "other":
            context.user_data.pop("problem", None)
            context.user_data["await_model_manual"] = True
            await update.message.reply_text("Вернул к вводу модели.", reply_markup=ReplyKeyboardRemove())
            await update.effective_chat.send_message(
                "✍️ Напишите модель устройства ещё раз:",
                reply_markup=back_text_keyboard(),
            )
            return S_MODEL

        context.user_data.clear()
        await update.message.reply_text("Вернул в начало.", reply_markup=ReplyKeyboardRemove())
        await update.effective_chat.send_message("Выберите устройство:", reply_markup=device_keyboard())
        return S_DEVICE

    if len(text) < 2:
        await update.message.reply_text("Опишите вопрос чуть подробнее, пожалуйста.")
        return S_PROBLEM

    context.user_data["problem"] = text
    await update.message.reply_text("Готово, всё записал.", reply_markup=ReplyKeyboardRemove())

    if flow == "other":
        set_phone_back_target(context, "other_problem")
        await update.effective_chat.send_message(
            current_selection_html(context, step="share_phone", show_price=False, show_part=False),
            parse_mode=ParseMode.HTML,
            reply_markup=phone_keyboard(),
        )
        return S_PHONE

    if flow == "consult":
        context.user_data["model"] = context.user_data.get("model") or "Консультация"
        set_phone_back_target(context, "problem")
        await update.effective_chat.send_message(
            "📞 Оставьте номер телефона для связи:",
            reply_markup=phone_keyboard(),
        )
        return S_PHONE

    await update.effective_chat.send_message(
        "📞 Оставьте номер телефона для связи:", reply_markup=phone_keyboard()
    )
    set_phone_back_target(context, "problem")
    return S_PHONE


async def on_part_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    model = context.user_data.get("model", "")
    service = context.user_data.get("service", "")
    buckets = get_part_buckets(model, service)

    if q.data == "back":
        selected_bucket = context.user_data.get("part_bucket_selected")
        if selected_bucket:
            if context.user_data.get("part_has_bucket_menu"):
                context.user_data.pop("part_bucket_selected", None)
                context.user_data.pop("part_has_bucket_menu", None)
                await q.edit_message_text(
                    current_selection_html(context, step="choose_part_type", show_part=False),
                    parse_mode=ParseMode.HTML,
                    reply_markup=part_type_keyboard(model, service),
                )
                return S_PART_CHOICE

            context.user_data.pop("part_bucket_selected", None)
            context.user_data.pop("part_has_bucket_menu", None)
            await q.edit_message_text("Выберите нужную услугу:", reply_markup=services_keyboard())
            return S_SERVICE

        await q.edit_message_text("Выберите нужную услугу:", reply_markup=services_keyboard())
        return S_SERVICE

    if q.data.startswith("parttype:"):
        bucket = q.data.split(":", 1)[1]
        options = buckets.get(bucket, [])
        if not options:
            await q.edit_message_text(
                "Не нашёл вариантов для этой категории. Попробуйте ещё раз.",
                reply_markup=part_type_keyboard(model, service),
            )
            return S_PART_CHOICE

        if len(options) == 1:
            chosen = options[0]
            context.user_data["part"] = chosen
            context.user_data.pop("part_bucket_selected", None)
            context.user_data.pop("part_has_bucket_menu", None)
            set_phone_back_target(context, "part_type")
            await q.edit_message_text(
                current_selection_html(context, step="share_phone"),
                parse_mode=ParseMode.HTML,
            )
            return await prompt_phone_after_callback_message(q)

        context.user_data["part_bucket_selected"] = bucket
        context.user_data["part_has_bucket_menu"] = True

        note = f"📂 <b>Категория:</b> {esc(bucket_label(bucket, service))}"
        if bucket_has_same_price(options):
            note += f"\n💰 Все варианты стоят <b>{esc(bucket_price_label(options))}</b>"

        await q.edit_message_text(
            current_selection_html(
                context,
                step="choose_part_option",
                show_part=False,
                extra_note=note,
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=part_options_keyboard(model, service, bucket),
        )
        return S_PART_CHOICE

    if q.data.startswith("partopt:"):
        _, bucket, idx_str = q.data.split(":", 2)
        options = buckets.get(bucket, [])
        try:
            idx = int(idx_str)
            chosen = options[idx]
        except Exception:
            await q.edit_message_text(
                "Не получилось выбрать вариант. Попробуйте ещё раз.",
                reply_markup=part_options_keyboard(model, service, bucket),
            )
            return S_PART_CHOICE

        context.user_data["part"] = chosen
        context.user_data.pop("part_bucket_selected", None)
        context.user_data.pop("part_has_bucket_menu", None)
        set_phone_back_target(context, f"part_options:{bucket}")
        await q.edit_message_text(
            current_selection_html(context, step="share_phone"),
            parse_mode=ParseMode.HTML,
        )
        await q.message.reply_text(
            "📞 Оставьте номер телефона, чтобы подтвердить запись:",
            reply_markup=phone_keyboard(),
        )
        return S_PHONE

    return S_PART_CHOICE


async def prompt_phone_after_callback_message(q) -> int:
    await q.message.reply_text(
        "📞 Оставьте номер телефона, чтобы подтвердить запись:",
        reply_markup=phone_keyboard(),
    )
    return S_PHONE


async def on_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text and go_home_requested(update.message.text):
        context.user_data.clear()
        await update.message.reply_text("Открыл главное меню.", reply_markup=ReplyKeyboardRemove())
        await update.effective_chat.send_message("Выберите устройство:", reply_markup=device_keyboard())
        return S_DEVICE

    if update.message.text and is_back_text(update.message.text):
        await update.message.reply_text("Вернул на предыдущий шаг.", reply_markup=ReplyKeyboardRemove())
        return await restore_phone_back_from_message(update, context)

    phone = update.message.contact.phone_number if update.message.contact else (update.message.text or "")
    normalized = normalize_phone(phone)
    if not re.match(r"^\+7\d{10}$", normalized):
        await update.message.reply_text(
            "Введите номер в формате <b>+79991234567</b> или нажмите кнопку отправки контакта.",
            parse_mode=ParseMode.HTML,
        )
        return S_PHONE

    context.user_data["phone"] = normalized
    await update.message.reply_text("Отлично, номер получил.", reply_markup=ReplyKeyboardRemove())

    if should_skip_date_step(context):
        context.user_data["date"] = ""
        await update.effective_chat.send_message(
            current_selection_html(context, step="choose_time"),
            parse_mode=ParseMode.HTML,
            reply_markup=time_keyboard(),
        )
        return S_TIME

    await update.effective_chat.send_message(
        current_selection_html(context, step="choose_date"),
        parse_mode=ParseMode.HTML,
        reply_markup=date_keyboard(),
    )
    return S_DATE


async def on_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    if q.data == "back":
        await q.edit_message_text("Вернул к номеру телефона.")
        await q.message.reply_text(
            "📞 Оставьте номер телефона ещё раз:", reply_markup=phone_keyboard()
        )
        return S_PHONE

    if q.data.startswith("date:"):
        iso = q.data.split(":", 1)[1]
        context.user_data["date"] = iso
        await q.edit_message_text(
            current_selection_html(context, step="choose_time"),
            parse_mode=ParseMode.HTML,
            reply_markup=time_keyboard(),
        )
        return S_TIME

    return S_DATE


async def on_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    if q.data == "back":
        if should_skip_date_step(context):
            await q.edit_message_text("Вернул к номеру телефона.")
            await q.message.reply_text(
                "📞 Оставьте номер телефона ещё раз:", reply_markup=phone_keyboard()
            )
            return S_PHONE
        await q.edit_message_text(
            current_selection_html(context, step="choose_date"),
            parse_mode=ParseMode.HTML,
            reply_markup=date_keyboard(),
        )
        return S_DATE

    if q.data == "time:manual":
        context.user_data["await_time_manual"] = True
        await q.edit_message_text(
            "✍️ Напишите удобное время в формате <b>18:30</b>.",
            parse_mode=ParseMode.HTML,
        )
        await q.message.reply_text(
            "Ниже есть кнопка для возврата назад или в главное меню.",
            reply_markup=back_text_keyboard(),
        )
        return S_TIME

    if q.data.startswith("time:"):
        context.user_data["time"] = q.data.split(":", 1)[1]
        return await show_confirm(q, context)

    return S_TIME


async def on_time_manual_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.user_data.get("await_time_manual"):
        return S_TIME

    t = (update.message.text or "").strip()
    if go_home_requested(t):
        context.user_data.clear()
        await update.message.reply_text("Открыл главное меню.", reply_markup=ReplyKeyboardRemove())
        await update.effective_chat.send_message("Выберите устройство:", reply_markup=device_keyboard())
        return S_DEVICE

    if is_back_text(t):
        context.user_data.pop("await_time_manual", None)
        await update.message.reply_text("Вернул к выбору времени.", reply_markup=ReplyKeyboardRemove())
        await update.effective_chat.send_message(
            current_selection_html(context, step="choose_time"),
            parse_mode=ParseMode.HTML,
            reply_markup=time_keyboard(),
        )
        return S_TIME

    if not re.match(r"^\d{1,2}:\d{2}$", t):
        await update.message.reply_text("Используйте формат времени, например <b>18:30</b>.", parse_mode=ParseMode.HTML)
        return S_TIME

    context.user_data.pop("await_time_manual", None)
    context.user_data["time"] = t
    await update.message.reply_text("Отлично, время записал.", reply_markup=ReplyKeyboardRemove())
    return await show_confirm(None, context, chat_id=update.effective_chat.id)


async def show_confirm(q_or_none, context: ContextTypes.DEFAULT_TYPE, chat_id: Optional[int] = None) -> int:
    txt = booking_summary_html(context)
    kb = kb_inline(
        [
            [("✅ Отправить заявку", "confirm:yes")],
            [("💬 Написать мастеру", "support")],
            [("⬅️ Назад", "confirm:back")],
        ]
    )

    if q_or_none:
        await q_or_none.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await context.bot.send_message(
            chat_id=chat_id, text=txt, parse_mode=ParseMode.HTML, reply_markup=kb
        )
    return S_CONFIRM


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    if q.data == "support":
        return await show_support_entry_from_callback(q, context, "confirm")

    if q.data == "confirm:back":
        await q.edit_message_text(
            current_selection_html(context, step="choose_time"),
            parse_mode=ParseMode.HTML,
            reply_markup=time_keyboard(),
        )
        return S_TIME

    if q.data == "confirm:yes":
        cfg = get_cfg(context)
        user = update.effective_user

        try:
            record = build_lead_record(context, user)
            lead_id = save_lead(record)
            remember_lead(context, lead_id, user.id)
        except Exception:
            logger.exception("Failed to save lead")
            await q.edit_message_text(
                "⚠️ Не удалось сохранить заявку. Попробуйте ещё раз через минуту или напишите мастеру.",
                parse_mode=ParseMode.HTML,
                reply_markup=support_keyboard("confirm"),
            )
            set_support_return(context, "confirm")
            return S_SUPPORT

        admin_notified = False
        try:
            sent = await context.bot.send_message(
                chat_id=cfg.admin_user_id,
                text=build_admin_text(context, user, lead_id),
                parse_mode=ParseMode.HTML,
            )
            remember_forward(context, sent.message_id, user.id, lead_id=lead_id)
            admin_notified = True
        except Exception:
            logger.exception("Failed to send lead to admin")

        message = (
            f"✅ <b>Заявка принята</b>\n\n"
            f"Номер заявки: <code>{esc(lead_id)}</code>\n"
            "Мастер получил ваши данные и свяжется с вами в ближайшее время."
        )
        if not admin_notified:
            message += (
                "\n\n⚠️ Заявка сохранена, но я не смог автоматически отправить её мастеру. "
                "Проверьте <code>ADMIN_USER_ID</code> и при необходимости напишите мастеру через кнопку ниже."
            )

        await q.edit_message_text(
            message,
            parse_mode=ParseMode.HTML,
            reply_markup=support_keyboard("submitted"),
        )
        set_support_return(context, "submitted")
        context.user_data["last_lead_id"] = lead_id
        return S_SUPPORT

    return S_CONFIRM


async def on_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if go_home_requested(text):
        context.user_data.clear()
        await update.message.reply_text("Открыл главное меню.", reply_markup=ReplyKeyboardRemove())
        await update.effective_chat.send_message("Выберите устройство:", reply_markup=device_keyboard())
        return S_DEVICE

    if is_back_text(text):
        await update.message.reply_text("Возвращаю на предыдущий шаг.", reply_markup=ReplyKeyboardRemove())
        return_to = get_support_return(context)
        if return_to == "confirm":
            return await show_confirm(None, context, chat_id=update.effective_chat.id)
        if return_to == "submitted":
            context.user_data.clear()
            await update.effective_chat.send_message("Выберите устройство:", reply_markup=device_keyboard())
            return S_DEVICE
        await update.effective_chat.send_message("Выберите нужную услугу:", reply_markup=services_keyboard())
        return S_SERVICE

    cfg = get_cfg(context)
    user = update.effective_user
    lead_id = str(context.user_data.get("last_lead_id") or "")

    header = (
        f"💬 Сообщение от клиента @{user.username}"
        if user and user.username
        else f"💬 Сообщение от клиента id:{user.id}"
    )
    if lead_id:
        header += f"\n🆔 Заявка: {lead_id}"

    delivered = False
    try:
        sent = await context.bot.send_message(cfg.admin_user_id, f"{header}\n\n{text}")
        remember_forward(context, sent.message_id, user.id, lead_id=lead_id)
        if lead_id:
            remember_lead(context, lead_id, user.id)
        delivered = True
    except Exception:
        logger.exception("Failed to send support message to admin")

    response = "✅ Сообщение мастеру отправлено." if delivered else "⚠️ Сообщение не удалось отправить мастеру автоматически."
    return_to = get_support_return(context)
    if return_to == "confirm":
        response += "\nВернитесь к заявке, когда будете готовы её отправить."
    elif return_to == "submitted":
        response += "\nПри необходимости можете отправить ещё одно сообщение или начать новую заявку."
    else:
        response += "\nПри необходимости можете вернуться к выбору услуги."

    await update.message.reply_text(
        response,
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.effective_chat.send_message(
        "Выберите действие:", reply_markup=support_keyboard(return_to)
    )
    return S_SUPPORT


async def on_support_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    if q.data == "support:back":
        return await restore_from_support_callback(q, context)

    return S_SUPPORT


async def orphan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    try:
        await q.answer("Кнопка устарела. Я открыл главное меню заново.")
    except Exception:
        pass

    context.user_data.clear()
    try:
        await q.edit_message_text("Меню обновлено. Выберите устройство:", reply_markup=device_keyboard())
    except Exception:
        await q.message.reply_text("Меню обновлено. Выберите устройство:", reply_markup=device_keyboard())


async def admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = get_cfg(context)
    if not cfg or update.effective_user.id != cfg.admin_user_id:
        return

    msg = update.message
    if not msg or not msg.reply_to_message:
        return

    user_id = resolve_reply_target_user_id(context, msg.reply_to_message)
    if not user_id:
        await update.message.reply_text(
            "Не удалось определить клиента для ответа. Ответьте свайпом/реплаем на сообщение этого клиента."
        )
        return

    reply_text = (msg.text or "").strip()
    if not reply_text:
        await update.message.reply_text("Сейчас поддерживаются текстовые ответы мастера.")
        return

    try:
        await context.bot.send_message(chat_id=user_id, text=f"🧑‍🔧 Мастер:\n{reply_text}")
    except Exception:
        logger.exception("Failed to deliver admin reply to user")
        await update.message.reply_text("Не удалось отправить ответ клиенту.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_chat.send_message(
        "Ок, отменил текущую запись. Чтобы начать заново, нажмите /start.",
        reply_markup=ReplyKeyboardRemove(),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Exception while handling update", exc_info=context.error)


def build_app() -> Application:
    cfg = load_config()

    ensure_valid_state_file()
    persistence = PicklePersistence(filepath=str(STATE_FILE))
    app = (
        Application.builder()
        .token(cfg.bot_token)
        .persistence(persistence)
        .concurrent_updates(False)
        .build()
    )
    app.bot_data["cfg"] = cfg

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            S_DEVICE: [CallbackQueryHandler(on_device)],
            S_MODEL: [
                CallbackQueryHandler(on_model),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_model_manual_text),
            ],
            S_SERVICE: [CallbackQueryHandler(on_service)],
            S_PROBLEM: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_problem)],
            S_PART_CHOICE: [CallbackQueryHandler(on_part_choice)],
            S_PHONE: [
                MessageHandler(filters.CONTACT, on_phone),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_phone),
            ],
            S_DATE: [CallbackQueryHandler(on_date)],
            S_TIME: [
                CallbackQueryHandler(on_time),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_time_manual_text),
            ],
            S_CONFIRM: [CallbackQueryHandler(on_confirm)],
            S_SUPPORT: [
                CallbackQueryHandler(on_support_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_support),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="rebootfix_conv",
        persistent=True,
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(orphan_callback))
    app.add_handler(MessageHandler(filters.REPLY & filters.TEXT, admin_reply))
    app.add_error_handler(error_handler)

    return app


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        level=logging.INFO,
    )
    application = build_app()
    application.run_polling(allowed_updates=Update.ALL_TYPES)
