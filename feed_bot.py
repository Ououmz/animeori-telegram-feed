from datetime import datetime
import html
from io import BytesIO
import json
import os
import re
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat
import requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
FEED_URL = os.environ["EPISODES_FEED_URL"]
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "https://animeori.edgeone.dev").rstrip("/")
MEDIA_BASE_URL = os.environ.get("MEDIA_BASE_URL", "https://animeori.ai.studio").rstrip("/")
GROUP_URL = os.environ.get("TELEGRAM_GROUP_URL", "").strip()
STATE_FILE = "seen_episodes.json"

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
    """Se l'area destra sfocata è troppo scura, passa a uno sfondo nero pulito."""
    width, height = ambilight_rgba.size
    right_crop = ambilight_rgba.crop((left_w, 0, width, height)).convert("L")
    avg_luma = ImageStat.Stat(right_crop).mean[0]

    if avg_luma >= threshold:
        return ambilight_rgba

    # Sotto la soglia, sfuma verso il nero pieno (0, 0, 0)
    darkness_factor = max(0.0, min(1.0, (threshold - avg_luma) / max(1.0, threshold - 15.0)))
    black_layer = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    blended = Image.blend(ambilight_rgba, black_layer, alpha=darkness_factor)
    return blended


def build_banner_image(photo_url: str, title: str) -> bytes | None:
    if not photo_url:
        return None
    try:
        resp = requests.get(photo_url, timeout=30)
        resp.raise_for_status()
        src_img = Image.open(BytesIO(resp.content)).convert("RGB")

        width, height = 1200, 600
        left_w = 450

        ambilight = ImageOps.fit(src_img, (width, height), method=Image.Resampling.LANCZOS)
        ambilight = ambilight.filter(ImageFilter.GaussianBlur(radius=55)).convert("RGBA")

        # Se la sfumatura a destra è troppo scura, passa al nero
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

        canvas = canvas_rgba.convert("RGB")
        draw = ImageDraw.Draw(canvas)

        right_x0 = left_w + 48
        right_width = width - right_x0 - 48
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


def send_telegram_item(ep: dict) -> bool:
    raw_title = str(ep.get("title") or "Nuovo episodio").strip()
    caption = format_caption(ep)
    reply_markup = build_reply_markup(ep)
    photo_url = build_image_url(ep)
    api_base = f"https://api.telegram.org/bot{BOT_TOKEN}"

    banner_bytes = build_banner_image(photo_url, raw_title)
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
    res = requests.get(FEED_URL, timeout=30)
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
    new_sent = 0

    for ep in ordered_episodes:
        ep_id = str(ep.get("id") or ep.get("hash") or "").strip()
        if not ep_id or ep_id in seen:
            continue
        if send_telegram_item(ep):
            seen.add(ep_id)
            new_sent += 1

    if new_sent > 0:
        save_seen(seen)
        print(f"Pubblicati {new_sent} nuovi episodi.")
    else:
        print("Nessun nuovo episodio da pubblicare.")


if __name__ == "__main__":
    main()
