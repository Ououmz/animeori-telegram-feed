import html
from io import BytesIO
import json
import os
import re
import time
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat
import requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()
FEED_URL = os.environ["EPISODES_FEED_URL"]
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "https://animeori.edgeone.dev").rstrip("/")
MEDIA_BASE_URL = os.environ.get("MEDIA_BASE_URL", "https://animeori.ai.studio").rstrip("/")
GROUP_URL = os.environ.get("TELEGRAM_GROUP_URL", "").strip()
FANART_API_KEY = os.environ.get("FANART_API_KEY", "").strip()
STATE_FILE = "seen_episodes.json"
CUSTOM_LOGOS_FILE = "custom_logos.json"
DM_STATE_FILE = "logo_dm_state.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}

URL_REGEX = re.compile(r"https?://[^\s<>\"']+", flags=re.IGNORECASE)
KEY_URL_LINE_REGEX = re.compile(r"^([^:\n]+?)\s*:\s*(https?://\S+)?\s*$", flags=re.IGNORECASE)


def load_seen() -> set[str]:
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return set(str(x) for x in data) if isinstance(data, list) else set()


def save_seen(seen: set[str]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, indent=2, ensure_ascii=False)


def normalize_custom_entry(val: object) -> dict:
    if isinstance(val, dict):
        fanart_flag = bool(val.get("fanart", False))
        url_val = str(val.get("url") or "").strip()
        return {"fanart": fanart_flag, "url": url_val}
    if isinstance(val, str):
        url_val = val.strip()
        return {"fanart": False, "url": url_val}
    return {"fanart": False, "url": ""}


def load_custom_logos() -> dict[str, dict]:
    if not os.path.exists(CUSTOM_LOGOS_FILE):
        return {}
    try:
        with open(CUSTOM_LOGOS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            result: dict[str, dict] = {}
            for k, v in data.items():
                key = str(k).strip().lower()
                if key:
                    result[key] = normalize_custom_entry(v)
            return result
    except Exception as exc:
        print(f"[custom_logos.json] Errore lettura: {exc}")
    return {}


def save_custom_logos(custom_logos: dict[str, dict]) -> None:
    ordered = {
        k: {
            "fanart": bool(custom_logos[k].get("fanart", False)),
            "url": str(custom_logos[k].get("url") or "").strip(),
        }
        for k in sorted(custom_logos.keys())
    }
    with open(CUSTOM_LOGOS_FILE, "w", encoding="utf-8") as f:
        json.dump(ordered, f, indent=2, ensure_ascii=False)


def load_dm_state() -> dict:
    default_state = {
        "last_update_id": 0,
        "requested_keys": [],
        "pending_queue": [],
    }
    if not os.path.exists(DM_STATE_FILE):
        return default_state
    try:
        with open(DM_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            last_upd = int(data.get("last_update_id") or 0)
            req_keys = [
                str(x).strip().lower()
                for x in (data.get("requested_keys") or [])
                if str(x).strip()
            ]
            queue: list[dict] = []
            for item in (data.get("pending_queue") or []):
                if isinstance(item, dict) and str(item.get("key") or "").strip():
                    queue.append({
                        "key": str(item["key"]).strip().lower(),
                        "title": str(item.get("title") or item["key"]).strip(),
                        "message_id": item.get("message_id"),
                    })
            return {
                "last_update_id": last_upd,
                "requested_keys": req_keys,
                "pending_queue": queue,
            }
    except Exception as exc:
        print(f"[logo_dm_state.json] Errore lettura: {exc}")
    return default_state


def save_dm_state(dm_state: dict) -> None:
    clean_state = {
        "last_update_id": int(dm_state.get("last_update_id") or 0),
        "requested_keys": sorted(set(str(x).strip().lower() for x in (dm_state.get("requested_keys") or []) if str(x).strip())),
        "pending_queue": [
            {
                "key": str(item["key"]).strip().lower(),
                "title": str(item.get("title") or item["key"]).strip(),
                "message_id": item.get("message_id"),
            }
            for item in (dm_state.get("pending_queue") or [])
            if isinstance(item, dict) and str(item.get("key") or "").strip()
        ],
    }
    with open(DM_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(clean_state, f, indent=2, ensure_ascii=False)


def order_episodes_chronologically(episodes: list) -> list[dict]:
    valid = [ep for ep in episodes if isinstance(ep, dict)]
    return list(reversed(valid))


def normalize_hash(raw_hash: str) -> str:
    h = raw_hash.strip()
    if not h:
        return ""
    if not h.startswith("#"):
        h = "#" + (h if h.startswith("/") else "/" + h)
    elif len(h) > 1 and not h[1:].startswith("/"):
        h = "#/" + h[1:]
    return h


def build_episode_url(ep: dict) -> str:
    ep_hash = normalize_hash(str(ep.get("hash") or ""))
    if ep_hash:
        return f"{SITE_BASE_URL}/{ep_hash}"
    return f"{SITE_BASE_URL}/"


def build_image_url(ep: dict) -> str:
    img = str(ep.get("image") or "").strip()
    if not img:
        return ""
    if img.startswith("http://") or img.startswith("https://"):
        return img
    if img.startswith("/"):
        return f"{MEDIA_BASE_URL}{img}"
    return f"{MEDIA_BASE_URL}/{img}"


def is_dub_ita(title: str) -> bool:
    has_sub_ita = bool(re.search(r"\bSUB\s*ITA\b", title, flags=re.IGNORECASE))
    if has_sub_ita:
        return False
    return bool(
        re.search(r"\(\s*ITA\s*\)", title, flags=re.IGNORECASE)
        or re.search(r"\bITA\b", title, flags=re.IGNORECASE)
    )


def clean_title_for_logo(title: str) -> str:
    cleaned = re.sub(r"\(\s*ITA\s*\)", "", title, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bSUB\s*ITA\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bITA\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bHD\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -–—|")
    return cleaned or title.strip()


def _extract_int(val: object) -> int | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return int(val)
    m = re.search(r"\d+", str(val))
    return int(m.group(0)) if m else None


def parse_episode_range(ep: dict) -> tuple[str, int | None, bool]:
    raw_ep = str(ep.get("episode") or "").strip()
    cleaned_ep = re.sub(r"^\s*episodi?\s*[:\-]?\s*|\s*ep\.?\s*", "", raw_ep, flags=re.IGNORECASE).strip()

    start_val = _extract_int(ep.get("episodeStart") if ep.get("episodeStart") is not None else ep.get("episode_start"))
    end_val = _extract_int(ep.get("episodeEnd") if ep.get("episodeEnd") is not None else ep.get("episode_end"))
    count_val = _extract_int(ep.get("episodesCount") if ep.get("episodesCount") is not None else ep.get("episodes_count"))

    range_match = re.search(r"(\d+)\s*[-–—]\s*(\d+)", cleaned_ep)
    if range_match:
        a = int(range_match.group(1))
        b = int(range_match.group(2))
        if b > a:
            return f"{a}-{b}", b, True
        return f"{a}", a, False

    if start_val is not None and end_val is not None and end_val > start_val:
        return f"{start_val}-{end_val}", end_val, True

    single_match = re.search(r"\d+", cleaned_ep)
    single_num = int(single_match.group(0)) if single_match else start_val

    if count_val is not None and count_val > 1 and single_num is not None:
        end_calc = single_num + count_val - 1
        return f"{single_num}-{end_calc}", end_calc, True

    if single_num is not None:
        return str(single_num), single_num, False

    return cleaned_ep, None, False


def is_final_episode(ep: dict, end_ep_num: int | None) -> bool:
    for flag_key in ("isFinal", "is_final", "isLastEpisode", "is_last_episode", "isCompleted", "is_completed", "final"):
        val = ep.get(flag_key)
        if isinstance(val, bool) and val:
            return True
        if isinstance(val, str) and val.strip().lower() in ("true", "1", "yes", "fine"):
            return True

    if re.search(r"\(\s*FINE\s*\)|\bFINE\b", str(ep.get("episode") or ""), flags=re.IGNORECASE):
        return True

    for total_key in ("totalEpisodes", "total_episodes", "lastEpisode", "last_episode", "maxEpisodes", "max_episodes"):
        total_val = _extract_int(ep.get(total_key))
        if total_val is not None and total_val > 0 and end_ep_num is not None and end_ep_num >= total_val:
            return True

    return False


def download_rgba_image(url: str) -> Image.Image | None:
    if not url:
        return None
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        img = Image.open(BytesIO(r.content)).convert("RGBA")
        if img.width < 20 or img.height < 10:
            return None
        return img
    except Exception as exc:
        print(f"Errore download immagine logo ({url}): {exc}")
        return None


def extract_fanart_show_id(ep: dict) -> str:
    candidate_keys = (
        "tvdb_id",
        "tvdbId",
        "thetvdb_id",
        "tvdb",
        "tmdb_id",
        "tmdbId",
        "tmdb",
        "show_id",
        "showId",
        "series_id",
        "seriesId",
    )
    containers: list[dict] = [ep]
    for sub_key in ("show", "series", "anime", "ids", "meta", "metadata"):
        sub_val = ep.get(sub_key)
        if isinstance(sub_val, dict):
            containers.append(sub_val)

    for box in containers:
        for key in candidate_keys:
            val = box.get(key)
            if val is None:
                continue
            s = str(val).strip()
            digits = re.sub(r"\D+", "", s)
            if digits and (s.isdigit() or key.lower().startswith(("tvdb", "tmdb", "show", "series"))):
                return digits
    return ""


def fetch_from_fanart_tv(ep: dict, query_title: str) -> Image.Image | None:
    if not FANART_API_KEY:
        print(f"[Fanart.tv] FANART_API_KEY assente per '{query_title}'")
        return None

    show_id = extract_fanart_show_id(ep)
    if not show_id:
        print(f"[Fanart.tv] Nessun ID serie trovato nel feed per '{query_title}' (chiavi: {list(ep.keys())})")
        return None

    try:
        url = f"https://webservice.fanart.tv/v3/tv/{show_id}"
        resp = requests.get(
            url,
            params={"api_key": FANART_API_KEY},
            headers=HEADERS,
            timeout=20,
        )
        if resp.status_code != 200:
            print(f"[Fanart.tv] HTTP {resp.status_code} per '{query_title}' (ID {show_id})")
            return None

        data = resp.json()

        def lang_rank(lang_val: str) -> int:
            lang = lang_val.strip().lower()
            if lang == "ja":
                return 0
            if lang == "en":
                return 1
            return 2

        candidates: list[tuple[int, int, str, str]] = []
        for field_idx, field in enumerate(("hdtvlogo", "clearlogo")):
            items = data.get(field)
            if not isinstance(items, list) or not items:
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                logo_url = str(it.get("url") or "").strip()
                if not logo_url:
                    continue
                lang_label = str(it.get("lang") or "").strip()
                candidates.append((lang_rank(lang_label), field_idx, lang_label or "n/d", logo_url))

        candidates.sort(key=lambda item: (item[0], item[1]))

        for _, _, lang_label, logo_url in candidates:
            logo_img = download_rgba_image(logo_url)
            if logo_img is not None:
                print(f"[Fanart.tv] Logo (lang={lang_label}) trovato per '{query_title}' (ID {show_id})")
                return logo_img

        print(f"[Fanart.tv] Nessun logo valido in hdtvlogo/clearlogo per '{query_title}' (ID {show_id})")
    except Exception as exc:
        print(f"[Fanart.tv] Errore per '{query_title}' (ID {show_id}): {exc}")

    return None


def get_anime_logo(
    ep: dict,
    title: str,
    logo_cache: dict[str, Image.Image | None],
    custom_logos: dict[str, dict],
    custom_logos_dirty: list[bool],
) -> Image.Image | None:
    query_title = clean_title_for_logo(title)
    cache_key = query_title.strip().lower()
    if not cache_key:
        return None

    if cache_key in logo_cache:
        cached = logo_cache[cache_key]
        return cached.copy() if cached is not None else None

    entry = custom_logos.get(cache_key)
    is_new_entry = entry is None
    if is_new_entry:
        entry = {"fanart": False, "url": ""}

    fanart_enabled = bool(entry.get("fanart", False))
    custom_url = str(entry.get("url") or "").strip()

    if not fanart_enabled and custom_url:
        logo_img = download_rgba_image(custom_url)
        if logo_img is not None:
            print(f"[custom_logos.json] Logo custom caricato per '{query_title}'")
            logo_cache[cache_key] = logo_img.copy()
            return logo_img
        print(f"[custom_logos.json] URL custom non valido per '{query_title}': {custom_url}")
        logo_cache[cache_key] = None
        return None

    logo_img = fetch_from_fanart_tv(ep, query_title)
    if logo_img is not None:
        if is_new_entry or not fanart_enabled:
            custom_logos[cache_key] = {"fanart": True, "url": custom_url}
            custom_logos_dirty[0] = True
        logo_cache[cache_key] = logo_img.copy()
        return logo_img

    if is_new_entry:
        custom_logos[cache_key] = {"fanart": False, "url": custom_url}
        custom_logos_dirty[0] = True
        print(f"[custom_logos.json] Aggiunto '{cache_key}' (fanart=false) in attesa di URL")

    logo_cache[cache_key] = None
    return None


def format_caption(ep: dict) -> str:
    raw_title = str(ep.get("title") or "Nuovo episodio").strip()
    ep_label, end_ep_num, is_range = parse_episode_range(ep)

    if ep_label:
        ep_part = f"Episodi {ep_label}" if is_range else f"Episodio {ep_label}"
    else:
        ep_part = "Nuovo episodio"

    tag_part = "ITA HD" if is_dub_ita(raw_title) else "SUB ITA HD"
    final_suffix = " <b>(FINE)</b>" if is_final_episode(ep, end_ep_num) else ""

    clean_title = html.escape(raw_title)
    clean_ep_part = html.escape(ep_part)
    clean_tag_part = html.escape(tag_part)

    return f"✅ Disponibile 「<b>{clean_title}</b>」 - {clean_ep_part} {clean_tag_part}{final_suffix}"


def build_reply_markup(ep: dict) -> dict:
    ep_label, _, _ = parse_episode_range(ep)
    if ep_label:
        ep_btn_label = f"Ep. {ep_label} Streaming & Download"
    else:
        ep_btn_label = "Streaming & Download"

    episode_url = build_episode_url(ep)
    rows = [
        [{"text": ep_btn_label, "url": episode_url}]
    ]

    if GROUP_URL:
        rows.append([{"text": "👥 GRUPPO", "url": GROUP_URL}])

    return {"inline_keyboard": rows}


def find_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def wrap_text_to_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def build_gradient_overlay(width: int, height: int) -> Image.Image:
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for x in range(width):
        ratio = x / float(max(1, width - 1))
        alpha = int(38 + (184 - 38) * (ratio ** 1.35))
        alpha = max(0, min(255, alpha))
        draw.line([(x, 0), (x, height)], fill=(10, 10, 14, alpha))
    return overlay


def apply_black_if_too_dark(ambilight_rgba: Image.Image, left_w: int, threshold: float = 48.0) -> Image.Image:
    width, height = ambilight_rgba.size
    right_crop = ambilight_rgba.crop((left_w, 0, width, height)).convert("L")
    avg_luma = ImageStat.Stat(right_crop).mean[0]

    if avg_luma >= threshold:
        return ambilight_rgba

    darkness_factor = max(0.0, min(1.0, (threshold - avg_luma) / max(1.0, threshold - 15.0)))
    black_layer = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    return Image.blend(ambilight_rgba, black_layer, alpha=darkness_factor)


def draw_logo_or_title(
    canvas_rgba: Image.Image,
    title: str,
    left_w: int,
    logo_img: Image.Image | None,
) -> Image.Image:
    width, height = canvas_rgba.size
    right_x0 = left_w + 48
    right_width = width - right_x0 - 48

    if logo_img is not None:
        max_w = right_width
        max_h = int(height * 0.62)
        fitted_logo = ImageOps.contain(logo_img, (max_w, max_h), method=Image.Resampling.LANCZOS).convert("RGBA")

        lx = right_x0 + (right_width - fitted_logo.width) // 2
        ly = (height - fitted_logo.height) // 2

        shadow = Image.new("RGBA", fitted_logo.size, (0, 0, 0, 0))
        shadow_alpha = fitted_logo.split()[3].point(lambda a: int(a * 0.45))
        shadow.putalpha(shadow_alpha)
        canvas_rgba.alpha_composite(shadow, dest=(lx + 3, ly + 4))
        canvas_rgba.alpha_composite(fitted_logo, dest=(lx, ly))
        return canvas_rgba

    canvas_rgb = canvas_rgba.convert("RGB")
    draw = ImageDraw.Draw(canvas_rgb)
    font_size = 46
    font = find_font(font_size, bold=True)
    lines = wrap_text_to_width(draw, title, font, right_width)

    while len(lines) > 4 and font_size > 28:
        font_size -= 4
        font = find_font(font_size, bold=True)
        lines = wrap_text_to_width(draw, title, font, right_width)

    lines = lines[:5]
    line_heights = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_heights.append(bbox[3] - bbox[1])

    gap = 14
    total_text_h = sum(line_heights) + gap * max(0, len(lines) - 1)
    y = (height - total_text_h) // 2

    for line, lh in zip(lines, line_heights):
        bbox = draw.textbbox((0, 0), line, font=font)
        lw = bbox[2] - bbox[0]
        x = right_x0 + (right_width - lw) // 2
        draw.text((x + 2, y + 3), line, font=font, fill=(0, 0, 0))
        draw.text((x, y), line, font=font, fill=(255, 255, 255))
        y += lh + gap

    return canvas_rgb.convert("RGBA")


def build_banner_image(
    ep: dict,
    photo_url: str,
    title: str,
    logo_cache: dict[str, Image.Image | None],
    custom_logos: dict[str, dict],
    custom_logos_dirty: list[bool],
) -> bytes | None:
    if not photo_url:
        return None
    try:
        resp = requests.get(photo_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        src_img = Image.open(BytesIO(resp.content)).convert("RGB")

        width, height = 1200, 600
        left_w = 450

        ambilight = ImageOps.fit(src_img, (width, height), method=Image.Resampling.LANCZOS)
        ambilight = ambilight.filter(ImageFilter.GaussianBlur(radius=55)).convert("RGBA")
        ambilight = apply_black_if_too_dark(ambilight, left_w=left_w, threshold=48.0)

        gradient = build_gradient_overlay(width, height)
        canvas_rgba = Image.alpha_composite(ambilight, gradient)

        cover = ImageOps.fit(src_img, (left_w, height), method=Image.Resampling.LANCZOS).convert("RGBA")
        alpha_mask = Image.new("L", (left_w, height), 255)
        mask_draw = ImageDraw.Draw(alpha_mask)

        fade_start = max(0, left_w - 55)
        for x in range(fade_start, left_w):
            t = (x - fade_start) / float(max(1, left_w - fade_start))
            a_val = int(255 * (1.0 - t))
            mask_draw.line([(x, 0), (x, height)], fill=max(0, min(255, a_val)))

        cover.putalpha(alpha_mask)
        canvas_rgba.alpha_composite(cover, dest=(0, 0))

        logo_img = get_anime_logo(ep, title, logo_cache, custom_logos, custom_logos_dirty)
        canvas_rgba = draw_logo_or_title(canvas_rgba, title, left_w=left_w, logo_img=logo_img)

        canvas = canvas_rgba.convert("RGB")
        out = BytesIO()
        canvas.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue()
    except Exception as exc:
        print(f"Errore generazione banner: {exc}")
        return None


def telegram_post_with_retry(
    url: str,
    *,
    json_payload: dict | None = None,
    data: dict | None = None,
    files: dict | None = None,
    max_retries: int = 3,
) -> requests.Response | None:
    for _ in range(max_retries):
        resp = requests.post(url, json=json_payload, data=data, files=files, timeout=30)
        if resp.status_code == 429:
            retry_after = 5
            try:
                body = resp.json()
                retry_after = int(body.get("parameters", {}).get("retry_after", 5))
            except Exception:
                pass
            wait_sec = max(1, retry_after) + 1
            print(f"[Telegram 429] Attendo {wait_sec}s prima di riprovare...")
            time.sleep(wait_sec)
            continue
        return resp
    return None


def send_dm_text(text: str, reply_to_message_id: int | None = None) -> int | None:
    if not ADMIN_CHAT_ID:
        return None
    api_base = f"https://api.telegram.org/bot{BOT_TOKEN}"
    payload: dict = {
        "chat_id": ADMIN_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id

    resp = telegram_post_with_retry(f"{api_base}/sendMessage", json_payload=payload)
    if resp is None or not resp.ok:
        return None
    try:
        body = resp.json()
        if body.get("ok"):
            return body.get("result", {}).get("message_id")
    except Exception:
        pass
    return None


def parse_key_url_pairs_from_text(text: str) -> list[tuple[str, str]]:
    raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
    pairs: list[tuple[str, str]] = []
    idx = 0

    while idx < len(raw_lines):
        line = raw_lines[idx]
        m = KEY_URL_LINE_REGEX.match(line)
        if m:
            raw_key = m.group(1).strip().lower()
            inline_url = (m.group(2) or "").strip()

            next_url = ""
            if idx + 1 < len(raw_lines):
                nxt = raw_lines[idx + 1].strip()
                if not KEY_URL_LINE_REGEX.match(nxt) and not URL_REGEX.match(nxt) and (
                    nxt.startswith("/") or (inline_url and not inline_url.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")))
                ):
                    next_url = nxt
                    idx += 1

            combined_url = f"{inline_url}{next_url}".strip().rstrip(".,;:!?")
            if raw_key and re.match(r"^https?://\S+$", combined_url, flags=re.IGNORECASE):
                pairs.append((raw_key, combined_url))
            idx += 1
            continue

        if line.endswith(":") and len(line) > 1 and idx + 1 < len(raw_lines):
            raw_key = line[:-1].strip().lower()
            nxt = raw_lines[idx + 1].strip()
            url_matches = URL_REGEX.findall(nxt)
            if raw_key and url_matches:
                pairs.append((raw_key, url_matches[0].rstrip(".,;:!?")))
                idx += 2
                continue

        idx += 1

    return pairs


def process_dm_replies(
    dm_state: dict,
    custom_logos: dict[str, dict],
    custom_logos_dirty: list[bool],
    dm_state_dirty: list[bool],
) -> None:
    if not ADMIN_CHAT_ID:
        return

    api_base = f"https://api.telegram.org/bot{BOT_TOKEN}"
    offset = int(dm_state.get("last_update_id") or 0) + 1

    try:
        resp = requests.get(
            f"{api_base}/getUpdates",
            params={"offset": offset, "timeout": 5, "allowed_updates": json.dumps(["message"])},
            timeout=20,
        )
        if not resp.ok:
            print(f"[DM getUpdates] HTTP {resp.status_code}")
            return
        body = resp.json()
        if not body.get("ok"):
            return
        updates = body.get("result") or []
    except Exception as exc:
        print(f"[DM getUpdates] Errore: {exc}")
        return

    max_seen_update_id = int(dm_state.get("last_update_id") or 0)
    queue: list[dict] = list(dm_state.get("pending_queue") or [])

    for upd in updates:
        if not isinstance(upd, dict):
            continue
        upd_id = int(upd.get("update_id") or 0)
        if upd_id > max_seen_update_id:
            max_seen_update_id = upd_id
            dm_state_dirty[0] = True

        msg = upd.get("message")
        if not isinstance(msg, dict):
            continue

        chat = msg.get("chat") or {}
        if str(chat.get("id") or "") != ADMIN_CHAT_ID:
            continue

        text = str(msg.get("text") or msg.get("caption") or "").strip()
        if not text:
            continue

        reply_to = msg.get("reply_to_message")
        replied_mid = reply_to.get("message_id") if isinstance(reply_to, dict) else None

        kv_pairs = parse_key_url_pairs_from_text(text)
        if kv_pairs:
            updated_items: list[str] = []
            handled_keys = set(k for k, _ in kv_pairs)
            queue = [q for q in queue if str(q.get("key") or "").strip().lower() not in handled_keys]

            for key, logo_url in kv_pairs:
                custom_logos[key] = {"fanart": False, "url": logo_url}
                updated_items.append(f"• <code>{html.escape(key)}</code> → {html.escape(logo_url)}")
                print(f"[DM] Salvato logo per '{key}' -> {logo_url}")

            custom_logos_dirty[0] = True
            dm_state_dirty[0] = True
            send_dm_text(
                "✅ Loghi aggiornati su <code>custom_logos.json</code>:\n" + "\n".join(updated_items),
                reply_to_message_id=msg.get("message_id"),
            )
            continue

        matches = URL_REGEX.findall(text)
        if not matches:
            continue

        logo_url = matches[0].rstrip(".,;:!?")
        if not queue:
            send_dm_text("ℹ️ Nessun anime in attesa di logo al momento.", reply_to_message_id=msg.get("message_id"))
            continue

        target_idx = 0
        if replied_mid is not None:
            for idx, q_item in enumerate(queue):
                if q_item.get("message_id") == replied_mid:
                    target_idx = idx
                    break

        target = queue.pop(target_idx)
        key = str(target["key"]).strip().lower()
        title_label = str(target.get("title") or key).strip()

        custom_logos[key] = {"fanart": False, "url": logo_url}
        custom_logos_dirty[0] = True
        dm_state_dirty[0] = True

        print(f"[DM] Salvato logo per '{key}' -> {logo_url}")
        send_dm_text(
            f"✅ Logo salvato per <b>{html.escape(title_label)}</b> (<code>{html.escape(key)}</code>):\n{html.escape(logo_url)}\nVerrà usato dai prossimi episodi dopo il commit.",
            reply_to_message_id=msg.get("message_id"),
        )

    dm_state["last_update_id"] = max_seen_update_id
    dm_state["pending_queue"] = queue


def notify_missing_custom_logos(
    dm_state: dict,
    custom_logos: dict[str, dict],
    dm_state_dirty: list[bool],
) -> None:
    if not ADMIN_CHAT_ID:
        return

    requested_set = set(str(x).strip().lower() for x in (dm_state.get("requested_keys") or []) if str(x).strip())
    queue: list[dict] = list(dm_state.get("pending_queue") or [])
    queued_keys = set(str(item.get("key") or "").strip().lower() for item in queue)

    for key in sorted(custom_logos.keys()):
        entry = custom_logos[key]
        fanart_enabled = bool(entry.get("fanart", False))
        url_val = str(entry.get("url") or "").strip()

        if fanart_enabled or url_val:
            continue
        if key in requested_set or key in queued_keys:
            continue

        mid = send_dm_text(
            f"🖼️ Nessun logo trovato su Fanart.tv per <b>{html.escape(key)}</b>.\n"
            f"Rispondi a questo messaggio con l'URL diretto, oppure scrivi <code>{html.escape(key)}: URL</code>."
        )
        requested_set.add(key)
        queue.append({
            "key": key,
            "title": key,
            "message_id": mid,
        })
        dm_state_dirty[0] = True
        time.sleep(0.5)

    dm_state["requested_keys"] = sorted(requested_set)
    dm_state["pending_queue"] = queue


def send_text_message(api_base: str, text: str, reply_markup: dict) -> bool:
    resp = telegram_post_with_retry(
        f"{api_base}/sendMessage",
        json_payload={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
            "reply_markup": reply_markup,
        },
    )
    if resp is None or not resp.ok or not resp.json().get("ok", False):
        status = resp.status_code if resp is not None else "no-resp"
        detail = resp.text if resp is not None else ""
        print(f"Errore sendMessage Telegram: {status} {detail}")
        return False
    return True


def send_telegram_item(
    ep: dict,
    logo_cache: dict[str, Image.Image | None],
    custom_logos: dict[str, dict],
    custom_logos_dirty: list[bool],
) -> bool:
    raw_title = str(ep.get("title") or "Nuovo episodio").strip()
    caption = format_caption(ep)
    reply_markup = build_reply_markup(ep)
    photo_url = build_image_url(ep)
    api_base = f"https://api.telegram.org/bot{BOT_TOKEN}"

    banner_bytes = build_banner_image(ep, photo_url, raw_title, logo_cache, custom_logos, custom_logos_dirty)
    if banner_bytes:
        resp = telegram_post_with_retry(
            f"{api_base}/sendPhoto",
            data={
                "chat_id": CHAT_ID,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(reply_markup),
            },
            files={"photo": ("banner.jpg", banner_bytes, "image/jpeg")},
        )
        if resp is not None and resp.ok and resp.json().get("ok", False):
            return True
        status = resp.status_code if resp is not None else "no-resp"
        detail = resp.text if resp is not None else ""
        print(f"Invio banner non riuscito, provo con URL: {status} {detail}")

    if photo_url:
        resp = telegram_post_with_retry(
            f"{api_base}/sendPhoto",
            json_payload={
                "chat_id": CHAT_ID,
                "photo": photo_url,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": reply_markup,
            },
        )
        if resp is not None and resp.ok and resp.json().get("ok", False):
            return True
        status = resp.status_code if resp is not None else "no-resp"
        detail = resp.text if resp is not None else ""
        print(f"sendPhoto con URL non riuscito, uso sendMessage: {status} {detail}")

    return send_text_message(api_base, caption, reply_markup)


def main() -> None:
    custom_logos = load_custom_logos()
    custom_logos_dirty = [False]
    dm_state = load_dm_state()
    dm_state_dirty = [False]

    process_dm_replies(dm_state, custom_logos, custom_logos_dirty, dm_state_dirty)

    res = requests.get(FEED_URL, headers=HEADERS, timeout=30)
    res.raise_for_status()
    payload = res.json()

    if isinstance(payload, list):
        episodes = payload
    elif isinstance(payload, dict):
        episodes = payload.get("episodes") or payload.get("items") or []
    else:
        episodes = []

    ordered_episodes = order_episodes_chronologically(episodes)
    seen = load_seen()
    logo_cache: dict[str, Image.Image | None] = {}
    new_sent = 0

    for ep in ordered_episodes:
        ep_id = str(ep.get("id") or ep.get("hash") or "").strip()
        if not ep_id or ep_id in seen:
            continue
        if send_telegram_item(ep, logo_cache, custom_logos, custom_logos_dirty):
            seen.add(ep_id)
            new_sent += 1
            time.sleep(1.5)

    notify_missing_custom_logos(dm_state, custom_logos, dm_state_dirty)

    if custom_logos_dirty[0]:
        save_custom_logos(custom_logos)

    if dm_state_dirty[0]:
        save_dm_state(dm_state)

    if new_sent > 0:
        save_seen(seen)
        print(f"Pubblicati {new_sent} nuovi episodi.")
    else:
        print("Nessun nuovo episodio da pubblicare.")


if __name__ == "__main__":
    main()
