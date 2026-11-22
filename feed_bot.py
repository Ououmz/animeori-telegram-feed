import html
import json
import os
import requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
FEED_URL = os.environ["EPISODES_FEED_URL"]
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "https://animeori.edgeone.dev").rstrip("/")
MEDIA_BASE_URL = os.environ.get("MEDIA_BASE_URL", "https://animeori.ai.studio").rstrip("/")
STATE_FILE = "seen_episodes.json"


def load_seen() -> set[str]:
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return set(str(x) for x in data) if isinstance(data, list) else set()


def save_seen(seen: set[str]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, indent=2, ensure_ascii=False)


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


def send_text_message(api_base: str, text: str) -> bool:
    resp = requests.post(
        f"{api_base}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    if not resp.ok or not resp.json().get("ok", False):
        print("Errore sendMessage Telegram:", resp.status_code, resp.text)
        return False
    return True


def send_telegram_item(ep: dict) -> bool:
    title = html.escape(str(ep.get("title") or "Nuovo episodio"))
    episode_label = html.escape(str(ep.get("episode") or ""))
    url = html.escape(build_episode_url(ep))
    photo_url = build_image_url(ep)

    header = f"<b>{title}</b>" + (f" — Ep. {episode_label}" if episode_label else "")
    text = (
        f"🆕 <b>Nuovo episodio disponibile!</b>\n"
        f"{header}\n\n"
        f"▶️ Guardalo su AnimeOri:\n{url}"
    )

    api_base = f"https://api.telegram.org/bot{BOT_TOKEN}"
    if photo_url:
        resp = requests.post(
            f"{api_base}/sendPhoto",
            json={
                "chat_id": CHAT_ID,
                "photo": photo_url,
                "caption": text,
                "parse_mode": "HTML",
            },
            timeout=30,
        )
        if resp.ok and resp.json().get("ok", False):
            return True
        print("sendPhoto non riuscito, uso sendMessage:", resp.status_code, resp.text)

    return send_text_message(api_base, text)


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

    seen = load_seen()
    new_sent = 0

    for ep in reversed(episodes):
        if not isinstance(ep, dict):
            continue
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
