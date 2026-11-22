from datetime import datetime
import html
from io import BytesIO
import json
import os
import re
from urllib.parse import quote_plus, urljoin
from bs4 import BeautifulSoup
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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
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
        print(f"Errore download immagine logo ({url}):", exc)
        return None


def fetch_from_fanart_tv(ep: dict, query_title: str) -> Image.Image | None:
    if not FANART_API_KEY:
        return None

    show_id = ""
    for key in ("tvdb_id", "tvdbId", "tmdb_id", "tmdbId", "show_id", "showId"):
        val = ep.get(key)
        if val is not None and str(val).strip().isdigit():
            show_id = str(val).strip()
            break

    if not show_id:
        return None

    try:
        url = f"https://webservice.fanart.tv/v3/tv/{show_id}"
        resp = requests.get(url, params={"api_key": FANART_API_KEY}, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            return None
        data = resp.json()
        for field in ("hdtvlogo", "clearlogo"):
            items = data.get(field)
            if isinstance(items, list) and items:
                preferred = next(
                    (it for it in items if str(it.get("lang", "")).lower() in ("it", "en", "ja")),
                    items[0],
                )
                logo_url = str(preferred.get("url") or "").strip()
                logo_img = download_rgba_image(logo_url)
                if logo_img is not None:
                    print(f"Logo trovato su Fanart.tv per '{query_title}'")
                    return logo_img
    except Exception as exc:
        print(f"Errore Fanart.tv per '{query_title}':", exc)

    return None


def fetch_from_seeklogo(query_title: str) -> Image.Image | None:
    try:
        search_url = f"https://seeklogo.com/search?q={quote_plus(query_title)}"
        resp = requests.get(search_url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        candidates: list[str] = []

        for img_tag in soup.find_all("img"):
            src = str(img_tag.get("src") or img_tag.get("data-src") or "").strip()
            if not src:
                continue
            full_src = urljoin("https://seeklogo.com", src)
            if "seeklogo" in full_src.lower() or "images.seeklogo.com" in full_src.lower():
                if not any(bad in full_src.lower() for bad in ("avatar", "icon", "sprite", "banner-ad")):
                    candidates.append(full_src)

        for a_tag in soup.find_all("a", href=True):
            href = str(a_tag["href"]).strip()
            if "/vector-logo/" in href:
                logo_page_url = urljoin("https://seeklogo.com", href)
                page_resp = requests.get(logo_page_url, headers=HEADERS, timeout=20)
                if page_resp.status_code == 200:
                    page_soup = BeautifulSoup(page_resp.text, "html.parser")
                    for p_img in page_soup.find_all("img"):
                        p_src = str(p_img.get("src") or p_img.get("data-src") or "").strip()
                        if p_src and ("images.seeklogo.com" in p_src.lower() or "seeklogo" in p_src.lower()):
                            full_p_src = urljoin("https://seeklogo.com", p_src)
                            if full_p_src not in candidates:
                                candidates.insert(0, full_p_src)
                break

        for cand_url in candidates[:4]:
            logo_img = download_rgba_image(cand_url)
            if logo_img is not None:
                print(f"Logo trovato su Seeklogo per '{query_title}'")
                return logo_img
    except Exception as exc:
        print(f"Errore Seeklogo per '{query_title}':", exc)

    return None


def get_anime_logo(ep: dict, title: str, logo_cache: dict[str, Image.Image | None]) -> Image.Image | None:
    query_title = clean_title_for_logo(title)
    cache_key = query_title.lower()

    if cache_key in logo_cache:
        cached = logo_cache[cache_key]
        return cached.copy() if cached is not None else None

    logo_img = fetch_from_fanart_tv(ep, query_title)
    if logo_img is None:
        logo_img = fetch_from_seeklogo(query_title)

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
        print("Errore generazione banner:", exc)
        return None


def send_text_message(api_base: str, text: str, reply_markup: dict) -> bool:
    resp = requests.post(
        f"{api_base}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
            "reply_markup": reply_markup,
        },
        timeout=30,
    )
    if not resp.ok or not resp.json().get("ok", False):
        print("Errore sendMessage Telegram:", resp.status_code, resp.text)
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
        resp = requests.post(
            f"{api_base}/sendPhoto",
            data={
                "chat_id": CHAT_ID,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(reply_markup),
            },
            files={"photo": ("banner.jpg", banner_bytes, "image/jpeg")},
            timeout=30,
        )
        if resp.ok and resp.json().get("ok", False):
            return True
        print("Invio banner non riuscito, provo con URL:", resp.status_code, resp.text)

    if photo_url:
        resp = requests.post(
            f"{api_base}/sendPhoto",
            json={
                "chat_id": CHAT_ID,
                "photo": photo_url,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": reply_markup,
            },
            timeout=30,
        )
        if resp.ok and resp.json().get("ok", False):
            return True
        print("sendPhoto con URL non riuscito, uso sendMessage:", resp.status_code, resp.text)

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

    if new_sent > 0:
        save_seen(seen)
        print(f"Pubblicati {new_sent} nuovi episodi.")
    else:
        print("Nessun nuovo episodio da pubblicare.")


if __name__ == "__main__":
    main()
