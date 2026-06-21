def utf16_len(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


def utf16_slice(text: str, start: int, end: int) -> str:

    result = []
    current = 0

    for char in text:
        char_len = utf16_len(char)
        if current >= end:
            break
        if current >= start:
            result.append(char)
        current += char_len

    return ''.join(result)


def truncate_to_utf16(text: str, limit: int) -> str:

    result = []
    current = 0

    for char in text:
        char_len = utf16_len(char)
        if current + char_len > limit:
            break
        result.append(char)
        current += char_len

    return ''.join(result)