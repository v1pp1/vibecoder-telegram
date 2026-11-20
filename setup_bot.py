"""Interactive first-time setup. Secrets are stored with Windows DPAPI."""
import argparse
import json
from pathlib import Path

import telegram_bot as core


def main():
    parser = argparse.ArgumentParser(description="Configure the Telegram bot")
    parser.add_argument("--chat-id", type=int)
    parser.add_argument("--owner-id", type=int)
    args = parser.parse_args()
    path = Path(__file__).with_name("bot-config.json")
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
        if not isinstance(config, dict):
            raise ValueError()
    except (ValueError, OSError):
        raise SystemExit("Check the JSON in bot-config.json.") from None
    token = core.saved_token() or core.clipboard_secret(
        "Copy your BotFather token, then press Enter here. ", r"\d+:[A-Za-z0-9_-]+")
    try:
        bot = core.telegram(token, "getMe")
    except core.ApiError as error:
        raise SystemExit(str(error)) from None
    core.save_token(token)
    chat_id = args.chat_id or config.get("chat_id")
    if chat_id is None:
        print("To find the group ID, run: python telegram_bot.py --find-chat")
        try:
            chat_id = int(input("Numeric group ID: "))
        except ValueError:
            raise SystemExit("A negative numeric ID is required.") from None
    if not isinstance(chat_id, int) or chat_id >= 0:
        raise SystemExit("A negative numeric group ID is required.")
    config["chat_id"] = chat_id
    if args.owner_id:
        if args.owner_id <= 0:
            raise SystemExit("owner-id must be positive.")
        config["owner_id"] = args.owner_id
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    print(f"@{bot['username']} is configured. Run start-telegram-bot.cmd.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Setup stopped.")
