"""Name matching shared by extraction and world-engine actor admission."""


def normalized_name(text: str) -> str:
    return "".join(str(text or "").split()).casefold()
