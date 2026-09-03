"""
Yandex.Почта — у Яндекса нет публичного REST API для почты, доступ идёт
через обычный IMAP с паролем приложения (создаётся на id.yandex.ru →
Пароли приложений). imaplib синхронный — оборачиваем в asyncio.to_thread,
чтобы не блокировать event loop.

"Канал" здесь — это папка почты (обычно просто "Входящие"), а не
отправитель: так проще всего реализуемо и достаточно для триажа.
"""
import asyncio
import base64
import email
import email.utils
import html as html_module
import imaplib
import logging
import os
import re
from datetime import datetime
from email.header import decode_header
from typing import AsyncIterator, Callable, Optional

from adapters.base import NormalizedChannel, NormalizedMessage, SourceAdapter

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.yandex.ru"
IMAP_PORT = 993

# Формат строки ответа LIST: (флаги) "разделитель" имя — имя может быть в
# кавычках (если содержит пробелы/спецсимволы) или голым атомом (простые
# имена вроде INBOX часто приходят без кавычек вообще).
_LIST_LINE_RE = re.compile(r'^\(.*?\)\s+(?:"(?P<delim>[^"]*)"|NIL)\s+(?P<name>.+)$')


def _decode_imap_utf7(s: str) -> str:
    """Названия папок с не-ASCII символами IMAP кодирует в "modified
    UTF-7" (RFC 3501) — это НЕ обычный UTF-7, у него другой набор
    безопасных символов ('&' вместо '+', ',' вместо '/'). Используется
    только для отображения (title) — сам external_id для операций с
    сервером остаётся в исходном (закодированном) виде, как его прислал
    IMAP."""
    result = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch != "&":
            result.append(ch)
            i += 1
            continue
        end = s.find("-", i + 1)
        if end == -1:
            end = n
        chunk = s[i + 1:end]
        if chunk == "":
            result.append("&")
        else:
            b64 = chunk.replace(",", "/")
            b64 += "=" * (-len(b64) % 4)
            try:
                result.append(base64.b64decode(b64).decode("utf-16-be"))
            except Exception:
                result.append(chunk)  # не смогли раскодировать — лучше показать как есть, чем упасть
        i = end + 1
    return "".join(result)


def _parse_list_line(decoded: str) -> Optional[str]:
    m = _LIST_LINE_RE.match(decoded.strip())
    if not m:
        return None
    name = m.group("name").strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    return name


def _decode_mime_words(s: str) -> str:
    if not s:
        return ""
    parts = decode_header(s)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _html_to_text(html: str) -> str:
    """Грубая, но достаточная для превью очистка HTML: убираем теги
    скриптов/стилей целиком, остальные теги — просто вырезаем, сущности
    вроде &nbsp; разворачиваем."""
    html = re.sub(r"(?is)<(script|style).*?>.*?(</\1>)", " ", html)
    html = re.sub(r"(?s)<br\s*/?>|</p>|</div>", "\n", html)
    html = re.sub(r"(?s)<[^>]+>", "", html)
    html = html_module.unescape(html)
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def _extract_text(msg) -> str:
    if msg.is_multipart():
        plain, html = None, None
        for part in msg.walk():
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp:
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain" and plain is None:
                payload = part.get_payload(decode=True)
                if payload:
                    plain = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            elif ctype == "text/html" and html is None:
                payload = part.get_payload(decode=True)
                if payload:
                    html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if plain:
            return plain
        if html:
            return _html_to_text(html)
        return ""

    payload = msg.get_payload(decode=True)
    if not payload:
        return ""
    text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    if msg.get_content_type() == "text/html":
        return _html_to_text(text)
    return text


class YandexMailAdapter(SourceAdapter):
    source_code = "yandex_mail"

    def __init__(self) -> None:
        self.login = os.environ["YANDEX_MAIL_LOGIN"]
        self.app_password = os.environ["YANDEX_MAIL_APP_PASSWORD"]
        self.poll_seconds = int(os.environ.get("YANDEX_MAIL_POLL_SECONDS", "180"))

    def _connect_sync(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        conn.login(self.login, self.app_password)
        return conn

    def _list_folders_sync(self) -> list[str]:
        conn = self._connect_sync()
        try:
            status, folders = conn.list()
            names = []
            if status == "OK":
                for raw in folders or []:
                    decoded = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
                    name = _parse_list_line(decoded)
                    if name and name not in names:
                        names.append(name)
            return names
        finally:
            conn.logout()

    async def discover_channels(self) -> list[NormalizedChannel]:
        names = await asyncio.to_thread(self._list_folders_sync)
        return [
            NormalizedChannel(external_id=n, title=_decode_imap_utf7(n), kind="mailbox")
            for n in names
        ]

    def _get_latest_uid_sync(self, folder: str) -> int:
        conn = self._connect_sync()
        try:
            status, _ = conn.select(f'"{folder}"', readonly=True)
            if status != "OK":
                return 0
            status, data = conn.uid("search", None, "ALL")
            if status != "OK" or not data or not data[0]:
                return 0
            uids = [int(x) for x in data[0].split()]
            return max(uids) if uids else 0
        finally:
            conn.logout()

    def _fetch_new_sync(self, folder: str, since_uid: int) -> tuple[list[NormalizedMessage], int]:
        conn = self._connect_sync()
        try:
            status, _ = conn.select(f'"{folder}"', readonly=True)
            if status != "OK":
                return [], since_uid

            status, data = conn.uid("search", None, f"UID {since_uid + 1}:*")
            if status != "OK" or not data or not data[0]:
                return [], since_uid

            uids = [u for u in (int(x) for x in data[0].split()) if u > since_uid]
            if not uids:
                return [], since_uid

            messages = []
            max_uid = since_uid
            for uid in uids:
                status, msg_data = conn.uid("fetch", str(uid), "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                msg = email.message_from_bytes(msg_data[0][1])

                subject = _decode_mime_words(msg.get("Subject", ""))
                from_name, from_addr = email.utils.parseaddr(msg.get("From", ""))
                from_name = _decode_mime_words(from_name) or from_addr

                body = _extract_text(msg).strip()
                text = f"{subject}\n\n{body}" if subject else body

                date_str = msg.get("Date")
                try:
                    sent_at = email.utils.parsedate_to_datetime(date_str) if date_str else datetime.utcnow()
                except Exception:
                    sent_at = datetime.utcnow()

                messages.append(NormalizedMessage(
                    channel_external_id=folder,
                    external_id=str(uid),
                    sender_name=from_name,
                    sender_id=None,
                    text=text[:5000],
                    is_reply_to_user=False,
                    is_direct_mention=False,
                    has_media=False,
                    sent_at=sent_at,
                    raw={"uid": uid, "folder": folder},
                ))
                max_uid = max(max_uid, uid)
            return messages, max_uid
        finally:
            conn.logout()

    async def listen(self, get_monitored_ids: Callable[[], set[str]]) -> AsyncIterator[NormalizedMessage]:
        last_uid: dict[str, int] = {}
        while True:
            for folder in get_monitored_ids():
                if folder not in last_uid:
                    # Первый раз для этой папки — не тащим всю историю,
                    # запоминаем текущий "верх" и ждём только новых писем.
                    try:
                        last_uid[folder] = await asyncio.to_thread(self._get_latest_uid_sync, folder)
                    except Exception:
                        logger.exception("Не удалось получить стартовый UID для папки %s", folder)
                    continue

                try:
                    messages, new_max = await asyncio.to_thread(self._fetch_new_sync, folder, last_uid[folder])
                except Exception:
                    logger.exception("Ошибка при опросе папки %s", folder)
                    continue

                last_uid[folder] = new_max
                for m in messages:
                    yield m

            await asyncio.sleep(self.poll_seconds)
