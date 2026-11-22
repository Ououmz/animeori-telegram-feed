from datetime import datetime
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
FEED_URL = os.environ["EPISODES_FEED_URL"]
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "https://animeori.edgeone.dev").rstrip("/")
MEDIA_BASE_URL = os.environ.get("MEDIA_BASE_URL", "https://animeori.ai.studio").rstrip("/")
GROUP_URL = os.environ.get("TELEGRAM_GROUP_URL", "").strip()
FANART_API_KEY = os.environ.get("FANART_API_KEY", "").strip()
STATE_FILE = "seen_episodes.json"
LOGOS_DIR = "logos"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}

COMMONS_HEADERS = {
    "User-Agent": f"AnimeOriTelegramBot/1.0 ({SITE_BASE_URL})"
}

DATE_KEYS = ("publishedAt", "published_at", "createdAt", "created_at", "date", "timestamp")


def load_seen() -> set[str]:
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return set(str(x) for x in data) if isinstance(data, list) else set()


def save_seen(seen: set[str]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, indent=2, ensure_ascii=False)


def parse_timestamp(ep: dict) -> float | None:
    for key in DATE_KEYS:
        val = ep.get(key)
        if val is None or val == "":
            continue
        if isinstance(val, (int, float)):
            ts = float(val)
            return ts / 1000.0 if ts > 1e12 else ts
        if isinstance(val, str):
            s = val.strip()
            try:
                num = float(s)
                return num / 1000.0 if num > 1e12 else num
            except ValueError:
                pass
            try:
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                return datetime.fromisoformat(s).timestamp()
            except ValueError:
                continue
    return None


def order_episodes_chronologically(episodes: list) -> list[dict]:
    valid = [ep for ep in episodes if isinstance(ep, dict)]
    timestamps = [parse_timestamp(ep) for ep in valid]

    if any(ts is not None for ts in timestamps):
        indexed_with_ts = [
            (ts if ts is not None else float("inf"), idx, ep)
            for idx, (ts, ep) in enumerate(zip(timestamps, valid))
        ]
        indexed_with_ts.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in indexed_with_ts]

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


def slugify_logo_name(text: str) -> str:
    s = text.lower().replace("½", "1-2")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def load_local_logo(query_title: str) -> Image.Image | None:
    if not os.path.isdir(LOGOS_DIR):
        return None

    slug = slugify_logo_name(query_title)
    no_year_slug = slugify_logo_name(re.sub(r"\(\s*\d{4}\s*\)", "", query_title))
    candidates = [
        os.path.join(LOGOS_DIR, f"{slug}.png"),
        os.path.join(LOGOS_DIR, f"{slug.replace('-', '_')}.png"),
    ]
    if no_year_slug and no_year_slug != slug:
        candidates.extend([
            os.path.join(LOGOS_DIR, f"{no_year_slug}.png"),
            os.path.join(LOGOS_DIR, f"{no_year_slug.replace('-', '_')}.png"),
        ])

    for path in candidates:
        if os.path.isfile(path):
            try:
                img = Image.open(path).convert("RGBA")
                if img.width >= 20 and img.height >= 10:
                    print(f"[Local logos/] Logo trovato per '{query_title}' ({path})")
                    return img
            except Exception as exc:
                print(f"[Local logos/] Errore lettura '{path}': {exc}")

    return None


def download_rgba_image(url: str, custom_headers: dict | None = None) -> Image.Image | None:
    if not url:
        return None
    req_headers = custom_headers if custom_headers is not None else HEADERS
    try:
        r = requests.get(url, headers=req_headers, timeout=20)
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


def fetch_from_wikimedia_commons(query_title: str) -> Image.Image | None:
    clean_query = re.sub(r"\(\s*\d{4}\s*\)", "", query_title).strip() or query_title
    queries = [f'incategory:"Logos of anime" {clean_query}']
    if clean_query != query_title:
        queries.append(f'incategory:"Logos of anime" {query_title}')

    try:
        for search_query in queries:
            params = {
                "action": "query",
                "format": "json",
                "generator": "search",
                "gsrsearch": search_query,
                "gsrnamespace": "6",
                "gsrlimit": "6",
                "prop": "imageinfo",
                "iiprop": "url|mime",
            }
            resp = requests.get(
                "https://commons.wikimedia.org/w/api.php",
                params=params,
                headers=COMMONS_HEADERS,
                timeout=20,
            )
            if resp.status_code != 200:
                print(f"[Wikimedia] HTTP {resp.status_code} per '{query_title}'")
                continue

            pages = (resp.json().get("query") or {}).get("pages") or {}
            ranked_pages: list[tuple[int, str, str]] = []
            for page in pages.values():
                if not isinstance(page, dict):
                    continue
                infos = page.get("imageinfo") or []
                if not infos or not isinstance(infos[0], dict):
                    continue
                mime = str(infos[0].get("mime") or "").lower()
                img_url = str(infos[0].get("url") or "").strip()
                if not img_url or mime == "image/svg+xml":
                    continue
                prio = 0 if mime == "image/png" else 1
                ranked_pages.append((prio, mime, img_url))

            ranked_pages.sort(key=lambda item: item[0])

            for _, mime, img_url in ranked_pages:
                logo_img = download_rgba_image(img_url, custom_headers=COMMONS_HEADERS)
                if logo_img is not None:
                    print(f"[Wikimedia] Logo ({mime}) trovato per '{query_title}'")
                    return logo_img

        print(f"[Wikimedia] Nessun logo raster valido trovato per '{query_title}'")
    except Exception as exc:
        print(f"[Wikimedia] Errore per '{query_title}': {exc}")

    return None


def get_anime_logo(ep: dict, title: str, logo_cache: dict[str, Image.Image | None]) -> Image.Image | None:
    query_title = clean_title_for_logo(title)
    cache_key = query_title.lower()

    if cache_key in logo_cache:
        cached = logo_cache[cache_key]
        return cached.copy() if cached is not None else None

    logo_img = fetch_from_fanart_tv(ep, query_title)
    if logo_img is None:
        logo_img = load_local_logo(query_title)
    if logo_img is None:
        logo_img = fetch_from_wikimedia_commons(query_title)

    logo_cache[cache_key] = logo_img.copy() if logo_img is not None else None
    return logo_img


def format_caption(ep: dict) -> str:
    raw_title = str(ep.get("title") or "Nuovo episodio").strip()
    raw_ep = str(ep.get("episode") or "").strip()

    ep_num = re.sub(r"^\s*ep\.?\s*", "", raw_ep, flags=re.IGNORECASE).strip()
    ep_part = f"Episodio {ep_num}" if ep_num else "Nuovo episodio"
    tag_part = "ITA HD" if is_dub_ita(raw_title) else "SUB ITA HD"

    clean_title = html.escape(raw_title)
    clean_ep_part = html.escape(ep_part)
    clean_tag_part = html.escape(tag_part)

    return f"✅ Disponibile 「{clean_title}」 - {clean_ep_part} {clean_tag_part}"


def build_reply_markup(ep: dict) -> dict:
    raw_ep = str(ep.get("episode") or "").strip()
    ep_num = re.sub(r"^\s*ep\.?\s*", "", raw_ep, flags=re.IGNORECASE).strip()
    ep_btn_label = f"Ep. {ep_num} Streaming & Download" if ep_num else "Streaming & Download"

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

        logo_img = get_anime_logo(ep, title, logo_cache)
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


def send_telegram_item(ep: dict, logo_cache: dict[str, Image.Image | None]) -> bool:
    raw_title = str(ep.get("title") or "Nuovo episodio").strip()
    caption = format_caption(ep)
    reply_markup = build_reply_markup(ep)
    photo_url = build_image_url(ep)
    api_base = f"https://api.telegram.org/bot{BOT_TOKEN}"

    banner_bytes = build_banner_image(ep, photo_url, raw_title, logo_cache)
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
        if send_telegram_item(ep, logo_cache):
            seen.add(ep_id)
            new_sent += 1
            time.sleep(1.5)

    if new_sent > 0:
        save_seen(seen)
        print(f"Pubblicati {new_sent} nuovi episodi.")
    else:
        print("Nessun nuovo episodio da pubblicare.")


if __name__ == "__main__":
    main()
