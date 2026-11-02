"""Первоисточник редакции ПП №719: страница Контура, RTF и сборка текста корпуса (`A10`).

ЗАЧЕМ. Актуализация начинается с вопроса «есть ли новая редакция» и с текста этой редакции. Обе
операции делались руками, хотя текст собирается детерминированно: замер 02.11.2026 на редакции
508574 — штатное извлечение из RTF побайтно равно `pp719_full.txt`, кроме одного блока, а HTML той
же редакции совпал с корпусом на 3 159 366 символах из 3 159 366.

ТРИ ФАКТА, НА КОТОРЫХ СТОИТ МОДУЛЬ:

1. Экспортёр Контура теряет таблицы. Вместо таблицы раздела IV в RTF стоит `LOST_MARK`. Блок берётся
   из прежнего корпуса по границам вокруг маркера (`restore_lost_blocks`).
2. HTML той же редакции — независимый свидетель: он содержит потерянную таблицу. Поэтому отдельной
   проверки «менял ли акт таблицу» нет: собранный текст обязан совпасть с HTML посимвольно
   (`witness`), и изменённая таблица роняет сверку сама.
3. Шапка «(в ред. …)» не называет акты до вступления в силу. Они перечислены в блоке «Примечания»,
   «ИЗМЕНЕН» той же страницы, со сроками (`parse_page`). Так в задачник не попали N 774 и N 1035.

ТРИ ИСХОДА ВМЕСТО ДВУХ. Сеть не пустила или разметка страницы изменилась — это «не проверено», а не
«всё хорошо» и не тревога. Ноль ложных тревог важнее полноты (`AGENTS_PLAN` §9): `parse_page`
возвращает `None`, если не нашёл статус редакции, и вызывающий обязан сказать об этом прямо.
"""

from __future__ import annotations

import html as html_lib
import http.client
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

KONTUR_DOC = ("https://www.kontur-extern.ru/info/normativ/document/1/"
              "{doc_id}-postanovlenie-pravitelstva-rf-ot-17-07-2015-n-719")
KONTUR_RTF = "https://www.kontur-extern.ru/info/normativ/rtf?moduleId=1&documentId={doc_id}"

LOST_MARK = "Невозможно сохранить выбранный фрагмент таблицы. Сохраните весь документ."
# Шапка редакции, вступающей позже: снимается Контуром к дате вступления, в корпус не входит.
BANNER_RE = re.compile(r"\A\s*Редакция данного документа вступает в силу с \d{2}\.\d{2}\.\d{4}\.?\s*")

_USER_AGENT = "Mozilla/5.0 (navigator-719 edition watch)"
# Кнопки интерфейса в конце страницы, после текста документа.
_PAGE_TAIL = "напечататьскачатьподелитьсяссылкой"
_HEAD_SLACK = 400     # сколько символов заголовка страницы может стоять перед текстом документа
_ANCHOR = 240         # длина границ вокруг потерянного блока
_DOC_START = 'id="js-doc-frame-container"'

_STATUS_RE = re.compile(r'id="js-revisions-status"[^>]*data-status="(?P<status>[^"]*)"[^>]*>(?P<text>.*?)</div>',
                        re.S)
_EDITION_RE = re.compile(r"Редакция от (\d{2}\.\d{2}\.\d{4})")
_IN_FORCE_RE = re.compile(r"(?:Действует|Вступит в силу) с (\d{2}\.\d{2}\.\d{4})")
_NOTES_RE = re.compile(r'<div class="doc_info-title2">Примечания</div>(?P<block>.*?)(?:</div>\s*</div>|<div class="doc_info-title)',
                       re.S)
_ACT_RE = re.compile(r"от (\d{2}\.\d{2}\.\d{4}) N (\d+)\s*(?:</A>)?\s*\(с\s*(\d{2}\.\d{2}\.\d{4})\)", re.I)
# Важно: Контур пишет эту ссылку в ОДИНАРНЫХ кавычках (страница 508767, 02.11.2026), остальные — в двойных.
_JUMP_RE = re.compile(r"""href=['"][^'"]*/document/1/(\d+)-[^'"]*-n-719[^'"]*['"][^>]*>\s*(Перейти[^<]*)<""", re.I)


@dataclass(frozen=True)
class PendingAct:
    """Акт из блока «Примечания», «ИЗМЕНЕН»: принят, но вступает позже."""
    date: str
    number: str
    effective: str

    @property
    def key(self) -> str:
        return f"N {self.number} от {self.date}"


@dataclass
class PageInfo:
    """Что страница Контура говорит о редакции."""
    status: str                       # data-status: Actual — действует
    title: str                        # «Редакция от 05.10.2026 — Действует с 31.10.2026»
    edition_date: str | None
    in_force_from: str | None
    pending: list[PendingAct] = field(default_factory=list)
    jumps: dict[str, str] = field(default_factory=dict)   # documentId: текст ссылки «Перейти …»

    @property
    def is_actual(self) -> bool:
        return self.status == "Actual"

    @property
    def is_future(self) -> bool:
        """Редакция принята, но ещё не вступила: корпус к дате готовят заранее (508574 до 31.10)."""
        return self.status == "NotInForce"


def fetch(url: str, tries: int = 3, timeout: int = 60, pause: float = 5.0) -> tuple[bytes | None, str]:
    """(данные, причина отказа). Важно: Сеть рвётся: отказ — это отказ, а не пустой документ."""
    last = ""
    for i in range(tries):
        if i:
            time.sleep(pause)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — адрес из констант модуля
                return r.read(), ""
        except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as e:
            last = f"{type(e).__name__}: {e}"
    return None, last


def _plain(fragment: str) -> str:
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_page(page: str) -> PageInfo | None:
    """Статус редакции, отложенные акты, ссылки на другие редакции. `None` — страница не разобрана."""
    m = _STATUS_RE.search(page)
    if not m:
        return None
    title = _plain(m.group("text"))
    edition = _EDITION_RE.search(title)
    if not edition:
        return None
    in_force = _IN_FORCE_RE.search(title)
    pending: list[PendingAct] = []
    notes = _NOTES_RE.search(page)
    if notes:
        block = html_lib.unescape(notes.group("block")).replace("\xa0", " ")
        pending = [PendingAct(d, n, e) for d, n, e in _ACT_RE.findall(block)]
    jumps = {doc: _plain(text) for doc, text in _JUMP_RE.findall(page)}
    return PageInfo(status=m.group("status"), title=title, edition_date=edition.group(1),
                    in_force_from=in_force.group(1) if in_force else None,
                    pending=pending, jumps=jumps)


def document_body(page: str, sep: str = "") -> str:
    """Текст документа со страницы, без тегов.

    `sep=""` — для посимвольной сверки: у `Y<sub>t</sub>` не должно появиться разрыва.
    `sep=" "` — для поиска номеров: без него «N 2139» склеивается с «28» из соседней ячейки, и
    появляется несуществующий акт N 213928 (ложная тревога на живой странице, 02.11.2026).

    Важно: Нет контейнера документа — пустая строка, а не вся страница: иначе блок отложенных актов попал
    бы в текст, и смена разметки выглядела бы как новые акты.
    """
    i = page.find(_DOC_START)
    if i < 0:
        return ""
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", page[i:], flags=re.S | re.I)
    return html_lib.unescape(re.sub(r"<[^>]+>", sep, body))


def rtf_to_corpus_text(raw: bytes) -> str:
    """Текст из RTF Контура теми же правилами, что `pp719_full.txt` (`parse_rtf.clean`)."""
    from striprtf.striprtf import rtf_to_text

    text = rtf_to_text(raw.decode("cp1251", errors="replace"), encoding="cp1251", errors="replace")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text).strip()
    return BANNER_RE.sub("", text)


@dataclass
class Restored:
    """Один потерянный экспортёром блок, подставленный из прежнего корпуса."""
    offset: int
    chars: int
    head: str


class SourceError(RuntimeError):
    """Текст не собирается механически: дальше нужен человек, причина — в сообщении."""


def restore_lost_blocks(text: str, old: str) -> tuple[str, list[Restored]]:
    """Вместо `LOST_MARK` — блок прежнего корпуса между теми же границами.

    Границы обязаны встретиться в прежнем корпусе ровно по одному разу и по порядку: иначе блок был
    бы взят наугад. Что блок не менялся, доказывает не эта функция, а `witness` на готовом тексте.
    """
    out = text
    restored: list[Restored] = []
    while LOST_MARK in out:
        i = out.index(LOST_MARK)
        pre, post = out[max(0, i - _ANCHOR):i], out[i + len(LOST_MARK):i + len(LOST_MARK) + _ANCHOR]
        if len(pre) < _ANCHOR or len(post) < _ANCHOR:
            raise SourceError("потерянный блок у края документа: границ для поиска нет")
        if old.count(pre) != 1 or old.count(post) != 1:
            raise SourceError(f"граница потерянного блока не единственна в прежнем корпусе "
                              f"(до: {old.count(pre)}, после: {old.count(post)}) рядом с {pre[-60:]!r}")
        start, end = old.index(pre) + len(pre), old.index(post)
        if end < start:
            raise SourceError("границы потерянного блока стоят в прежнем корпусе в обратном порядке")
        block = old[start:end]
        out = out[:i] + block + out[i + len(LOST_MARK):]
        restored.append(Restored(i, len(block), block[:80]))
    return out, restored


def stream(text: str) -> str:
    """Поток для сверки свидетелей: буквы, цифры и знаки, несущие смысл чисел, без разделителей.

    Разделитель между цифрами сохраняется меткой: иначе «034-2014» и «03-42014» были бы одним потоком.
    """
    low = re.sub(r"(?<=\d)[^0-9a-zа-я.,%<>()]+(?=\d)", "~", text.lower().replace("ё", "е"))
    return re.sub(r"[^0-9a-zа-я.,%<>()~]+", "", low)


@dataclass
class Witness:
    equal: bool
    compared: int
    offset: int = -1
    ours: str = ""
    theirs: str = ""
    note: str = ""


def witness(text: str, page: str) -> Witness:
    """Текст обязан посимвольно совпасть с HTML той же редакции."""
    ours, theirs = stream(text), stream(document_body(page, sep=" "))
    head = ours[:80]
    start = theirs.find(head)
    if start < 0 or start > _HEAD_SLACK:
        return Witness(False, len(ours), 0, ours[:120], theirs[:120], "начало документа не найдено в HTML")
    theirs = theirs[start:]
    limit = min(len(ours), len(theirs))
    n = 0
    step = 65536
    while n < limit and ours[n:n + step] == theirs[n:n + step]:
        n += step
    n = min(n, limit)
    while n < limit and ours[n] == theirs[n]:
        n += 1
    tail = theirs[len(ours):]
    if n == len(ours) and tail in ("", _PAGE_TAIL):
        return Witness(True, len(ours))
    return Witness(False, len(ours), n, ours[max(0, n - 80):n + 160], theirs[max(0, n - 80):n + 160],
                   f"расхождение на символе {n} из {len(ours)}")
