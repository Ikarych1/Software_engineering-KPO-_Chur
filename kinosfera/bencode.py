"""Minimal, strict bencode codec (BEP 3) used to inspect .torrent files without libtorrent."""
from __future__ import annotations


class BencodeError(ValueError):
    pass


MAX_DEPTH = 64


def decode(data: bytes):
    if not isinstance(data, (bytes, bytearray)):
        raise BencodeError('Ожидались байты.')
    value, end = _decode(memoryview(data), 0, 0)
    if end != len(data):
        raise BencodeError('Лишние данные после bencode-структуры.')
    return value


def decode_prefix(data: bytes, start: int = 0):
    """Decode one value and return (value, start, end) offsets — used to hash the raw info dict."""
    value, end = _decode(memoryview(data), start, 0)
    return value, start, end


def _decode(buf, i, depth):
    if depth > MAX_DEPTH:
        raise BencodeError('Слишком глубокая вложенность.')
    if i >= len(buf):
        raise BencodeError('Неожиданный конец данных.')
    c = buf[i]
    if c == 0x69:  # i
        end = _find(buf, 0x65, i + 1)
        raw = bytes(buf[i + 1:end])
        if not raw or raw in (b'-', b'-0') or (raw.startswith(b'0') and len(raw) > 1) or raw.startswith(b'-0'):
            raise BencodeError('Некорректное число.')
        try:
            return int(raw), end + 1
        except ValueError as exc:
            raise BencodeError('Некорректное число.') from exc
    if c == 0x6c:  # l
        i += 1
        items = []
        while True:
            if i >= len(buf):
                raise BencodeError('Незавершённый список.')
            if buf[i] == 0x65:
                return items, i + 1
            item, i = _decode(buf, i, depth + 1)
            items.append(item)
    if c == 0x64:  # d
        i += 1
        result = {}
        while True:
            if i >= len(buf):
                raise BencodeError('Незавершённый словарь.')
            if buf[i] == 0x65:
                return result, i + 1
            key, i = _decode(buf, i, depth + 1)
            if not isinstance(key, bytes):
                raise BencodeError('Ключ словаря должен быть строкой.')
            value, i = _decode(buf, i, depth + 1)
            result[key] = value
    if 0x30 <= c <= 0x39:
        colon = _find(buf, 0x3a, i)
        try:
            length = int(bytes(buf[i:colon]))
        except ValueError as exc:
            raise BencodeError('Некорректная длина строки.') from exc
        start = colon + 1
        end = start + length
        if end > len(buf):
            raise BencodeError('Строка выходит за пределы данных.')
        return bytes(buf[start:end]), end
    raise BencodeError('Неизвестный тип bencode.')


def _find(buf, char, start):
    for index in range(start, min(len(buf), start + 64)):
        if buf[index] == char:
            return index
    raise BencodeError('Некорректный bencode.')


def encode(value) -> bytes:
    out = []
    _encode(value, out)
    return b''.join(out)


def _encode(value, out):
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        out.append(b'i%de' % value)
    elif isinstance(value, (bytes, bytearray)):
        out.append(b'%d:' % len(value))
        out.append(bytes(value))
    elif isinstance(value, str):
        _encode(value.encode('utf-8'), out)
    elif isinstance(value, (list, tuple)):
        out.append(b'l')
        for item in value:
            _encode(item, out)
        out.append(b'e')
    elif isinstance(value, dict):
        out.append(b'd')
        for key in sorted(k.encode('utf-8') if isinstance(k, str) else k for k in value):
            original = key if key in value else key.decode('utf-8')
            _encode(key, out)
            _encode(value[original], out)
        out.append(b'e')
    else:
        raise TypeError(f'Невозможно закодировать {type(value)!r}')
