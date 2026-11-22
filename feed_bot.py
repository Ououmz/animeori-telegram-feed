from datetime import datetime
import html
import json
import os
import re
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


def is_dubbed_ita(ep: dict) -> bool:
    title = str(ep.get("title") or "")
    ep_label = str(ep.get("episode") or "")
    combined = f"{title} {ep_label}"
    return bool(re.search(r"[\(\[]\s*ITA\s*[\)\]]|\bITA\b", combined, flags=re.IGNORECASE)) and not bool(
        re.search(r"\bSUB\s*ITA\b", combined, flags=re.IGNORECASE)
    )


def build_reply_markup(ep: dict, episode_url: str) -> dict:
    ep_raw = str(ep.get("episode") or "").strip()
    ep_btn_label = f"Ep. {ep_raw} Streaming & Download" if ep_raw else "Streaming & Download"

    keyboard = [
        [{"text": ep_btn_label, "url": episode_url}]
    ]

    if GROUP_URL.startswith(("http://", "https://")):
        keyboard.append([{"text": "👥 GRUPPO", "url": GROUP_URL}])

    return {"inline_keyboard": keyboard}


def format_caption(ep: dict) -> str:
    raw_title = str(ep.get("title") or "Nuovo episodio").strip()
    ep_raw = str(ep.get("episode") or "").strip()
    lang_tag = "ITA HD" if is_dubbed_ita(ep) else "SUB ITA HD"

    title = html.escape(raw_title)

    if ep_raw:
        ep_escaped = html.escape(ep_raw)
        return f"✅ Disponibile 「<b>{title}</b>」 - Episodio {ep_escaped} {lang_tag}"
    return f"✅ Disponibile 「<b>{title}</b>」 - {lang_tag}"


def send_text_message(api_base: str, text: str, reply_markup: dict) -> bool:
    resp = requests.post(
        f"{api_base}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": reply_markup,
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    if not resp.ok or not resp.json().get("ok", False):
        print("Errore sendMessage Telegram:", resp.status_code, resp.text)
        return False
    return True


def send_telegram_item(ep: dict) -> bool:
    episode_url = build_episode_url(ep)
    photo_url = build_image_url(ep)
    caption = format_caption(ep)
    reply_markup = build_reply_markup(ep, episode_url)

    api_base = f"https://api.telegram.org/bot{BOT_TOKEN}"
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
        print("sendPhoto non riuscito, uso sendMessage:", resp.status_code, resp.text)

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
        print(f"Pubblicati {new_sent} nuovi episodi in ordine cronologico.")
    else:
        print("Nessun nuovo episodio da pubblicare.")


if __name__ == "__main__":
    main()
