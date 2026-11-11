"""Example only: copy together with a manifest before enabling in Telegram."""


def on_message(message: str):
    """Return text only for an exact command; None leaves the message unchanged."""
    if message.strip().casefold() == "!help":
        return "Здравствуйте! Опишите ваш вопрос в этом чате, и продавец ответит вам."
    return None
