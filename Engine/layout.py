"""
Model podziału rozdziału na plansze — granice jako indeksy słów, plansze jako widok pochodny.

Dziś podział jest efektem ubocznym chunkera i nie da się go tknąć bez ponownego
przetworzenia rozdziału. Tutaj podział jest osobnym, edytowalnym bytem:

    tokens   — słowa rozdziału w kolejności czytania (niezmienne)
    breaks   — indeksy tokenów, na których zaczyna się nowa plansza (edytowalne)
    plansze  — wyliczane w locie z tokens + breaks

Dzięki temu przesunięcie, podział albo scalenie granicy to operacja na tablicy liczb,
a nie ponowna transkrypcja. Chunker zostaje w roli, w której jest dobry: proponuje
podział startowy. Użytkownik ma ostatnie słowo.

Czasy cięć trzyma słownik ``overrides``, kluczowany indeksem tokenu, na którym stoi
granica. To identyfikator stabilny: gdy granica zniknie (scalenie) albo się przesunie,
przypisany jej ręczny czas znika razem z nią, zamiast po cichu przykleić się do sąsiada.

Moduł nie zna audio ani Whispera — da się go testować bez nagrań:

    python -m Engine.layout --chapter 1 --verify
    python -m Engine.layout --all --verify
    python -m Engine.layout --chapter 1 --list
    python -m Engine.layout --chapter 1 --split 3 120 --save
"""

from __future__ import annotations

import os
import re
import sys
import json
import glob
import hashlib
import argparse
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .text_parser import Chapter, estimate_line_count, chapters_from_map, parse_book
from .chunker import create_chunks_for_chapter

LAYOUT_VERSION = 1
INTRO_OUTRO_PLACEHOLDER = "(brak tekstu w pliku źródłowym)"


class LayoutError(ValueError):
    """Niepoprawna operacja na podziale — komunikat nadaje się do pokazania użytkownikowi."""


def book_cut_key(token_index: int) -> str:
    """Identyfikator cięcia stojącego na słowie książki."""
    return f"b{token_index}"


def insert_cut_key(anchor: int, board: int) -> str:
    """Identyfikator cięcia rozpoczynającego kolejną planszę wstawki."""
    return f"i{anchor}.{board}"


def _cut_key(raw: Any) -> str:
    """
    Klucz cięcia z zapisu na dysku.

    Starsze podziały trzymały czysty indeks słowa; od czasu wstawek granica może
    stać także w środku dopisanego fragmentu, więc klucze są tekstowe. Liczby
    z dawnych plików tłumaczymy na nowy format zamiast je odrzucać.
    """
    text = str(raw)
    return book_cut_key(int(text)) if text.lstrip("-").isdigit() else text


# ---------------------------------------------------------------------------
# Tokenizacja
# ---------------------------------------------------------------------------

@dataclass
class Token:
    """Jedno słowo rozdziału wraz z separatorem prowadzącym do następnego."""
    word: str
    sep: str            # " " albo "\n" — biały znak do następnego tokenu
    block: int          # indeks bloku źródłowego (akapit / linia dialogowa)
    is_dialogue: bool
    start: int          # offset znakowy w tekście z render_text(tokens)
    end: int


def tokenize_chapter(chapter: Chapter) -> List[Token]:
    """
    Rozbija rozdział na tokeny, zachowując informację o białych znakach.

    Separator jest znormalizowany: ciąg zawierający znak nowej linii staje się "\\n",
    każdy inny — pojedynczą spacją. Bez tego odtworzony tekst różniłby się od
    oryginału podwójnymi spacjami i twardymi łamaniami z pliku źródłowego.
    """
    tokens: List[Token] = []

    for block_idx, block in enumerate(chapter.blocks):
        matches = list(re.finditer(r"\S+", block.text))
        for m_idx, match in enumerate(matches):
            if m_idx + 1 < len(matches):
                gap = block.text[match.end():matches[m_idx + 1].start()]
                sep = "\n" if "\n" in gap else " "
            else:
                sep = "\n"  # granica bloku; ostatni token dostanie "" niżej
            tokens.append(Token(
                word=match.group(0),
                sep=sep,
                block=block_idx,
                is_dialogue=block.is_dialogue,
                start=0,
                end=0,
            ))

    if tokens:
        tokens[-1].sep = ""

    cursor = 0
    for token in tokens:
        token.start = cursor
        token.end = cursor + len(token.word)
        cursor = token.end + len(token.sep)

    return tokens


def render_text(tokens: Sequence[Token], lo: int = 0, hi: Optional[int] = None) -> str:
    """Odtwarza tekst z zakresu tokenów [lo, hi). Separator ostatniego tokenu jest pomijany."""
    hi = len(tokens) if hi is None else hi
    if lo >= hi:
        return ""
    parts: List[str] = []
    for i in range(lo, hi):
        parts.append(tokens[i].word)
        if i + 1 < hi:
            parts.append(tokens[i].sep)
    return "".join(parts)


def tokens_hash(tokens: Sequence[Token]) -> str:
    """Odcisk sekwencji słów. Zmiana tekstu źródłowego unieważnia zapisany podział."""
    joined = "\n".join(t.word for t in tokens)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

@dataclass
class Layout:
    """Podział rozdziału na plansze. Sam w sobie nie zawiera tekstu ani czasów."""
    chapter_num: int
    text_hash: str
    token_count: int
    breaks: List[int] = field(default_factory=list)
    overrides: Dict[str, float] = field(default_factory=dict)
    reviewed: List[str] = field(default_factory=list)
    # Wstawki: kotwica (indeks słowa książki) -> {"text", "breaks", "edited"}.
    # Same czasy i słowa Whispera zostają w wyniku przetwarzania — tutaj trzymamy
    # wyłącznie to, co użytkownik postanowił.
    inserts: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Korekty tekstu plansz książkowych: "lo-hi" -> tekst. Zmieniają wyłącznie
    # to, co idzie na wyjście; czasy i podział zostają nietknięte.
    corrections: Dict[str, str] = field(default_factory=dict)
    version: int = LAYOUT_VERSION

    def bounds(self) -> List[int]:
        return [0] + list(self.breaks) + [self.token_count]

    @property
    def board_count(self) -> int:
        return len(self.breaks) + 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "chapter_num": self.chapter_num,
            "text_hash": self.text_hash,
            "token_count": self.token_count,
            "breaks": list(self.breaks),
            "overrides": dict(sorted(self.overrides.items())),
            "reviewed": sorted(self.reviewed),
            "inserts": self.inserts,
            "corrections": self.corrections,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Layout":
        return cls(
            chapter_num=int(data["chapter_num"]),
            text_hash=str(data.get("text_hash", "")),
            token_count=int(data["token_count"]),
            breaks=[int(b) for b in data.get("breaks", [])],
            overrides={_cut_key(k): float(v) for k, v in (data.get("overrides") or {}).items()},
            reviewed=[_cut_key(t) for t in (data.get("reviewed") or [])],
            inserts={str(k): dict(v) for k, v in (data.get("inserts") or {}).items()},
            corrections={str(k): str(v) for k, v in (data.get("corrections") or {}).items()},
            version=int(data.get("version", LAYOUT_VERSION)),
        )


def propose_layout(
    chapter: Chapter,
    tokens: Optional[List[Token]] = None,
    max_lines: int = 11,
    max_chars_per_line: int = 45,
) -> Layout:
    """
    Podział startowy — dokładnie ten, który dziś produkuje chunker.

    Granice odczytujemy z długości plansz chunkera, zamiast powielać jego heurystykę.
    Gdyby obie ścieżki rozjechały się w tokenizacji, zgłaszamy to zamiast po cichu
    przesuwać podział o kilka słów.
    """
    tokens = tokenize_chapter(chapter) if tokens is None else tokens
    chunks = create_chunks_for_chapter(chapter.blocks, max_lines, max_chars_per_line)

    chunk_words = [w for c in chunks for w in c.words]
    token_words = [t.word for t in tokens]
    if chunk_words != token_words:
        first = next(
            (i for i, (a, b) in enumerate(zip(chunk_words, token_words)) if a != b),
            min(len(chunk_words), len(token_words)),
        )
        raise LayoutError(
            f"Tokenizacja rozeszła się z chunkerem w rozdziale {chapter.number} "
            f"(słowo {first}: chunker {chunk_words[first:first + 3]!r} "
            f"vs layout {token_words[first:first + 3]!r})."
        )

    breaks: List[int] = []
    acc = 0
    for chunk in chunks[:-1]:
        acc += len(chunk.words)
        breaks.append(acc)

    return Layout(
        chapter_num=chapter.number,
        text_hash=tokens_hash(tokens),
        token_count=len(tokens),
        breaks=breaks,
    )


# ---------------------------------------------------------------------------
# Operacje edycyjne
# ---------------------------------------------------------------------------

def _drop_override(layout: Layout, token_index: int) -> None:
    """Ręczny czas i znacznik przejrzenia są przypisane do granicy — znikają razem z nią."""
    key = book_cut_key(token_index)
    layout.overrides.pop(key, None)
    if key in layout.reviewed:
        layout.reviewed.remove(key)


def _effective_text(layout: Layout, tokens: Sequence[Token], lo: int, hi: int) -> str:
    """Tekst planszy taki, jaki widzi użytkownik: korekta, a w jej braku tekst książki."""
    return layout.corrections.get(correction_key(lo, hi)) or render_text(tokens, lo, hi)


def _store_correction(layout: Layout, tokens: Sequence[Token],
                      lo: int, hi: int, text: str) -> None:
    """Zapisuje korektę, pomijając taką, która nie różni się od tekstu książki."""
    text = (text or "").strip()
    # Korekta równa tekstowi książki nic nie wnosi, a fałszywie oznaczałaby planszę
    # jako poprawioną ręcznie.
    if text and text != render_text(tokens, lo, hi):
        layout.corrections[correction_key(lo, hi)] = text


def _merge_corrections(
    layout: Layout,
    tokens: Optional[Sequence[Token]],
    old_ranges: Sequence[Tuple[int, int]],
    new_lo: int,
    new_hi: int,
) -> None:
    """
    Skleja korekty scalanych plansz w jedną.

    Wierne co do słowa: nowa plansza dostaje dokładnie to, co użytkownik widział na
    obu poprzednich, w tej samej kolejności. Wcześniej obie korekty zostawały przypięte
    do zakresów, które po scaleniu przestawały istnieć, i znikały z wyjścia bez śladu.
    """
    if tokens is None:
        return
    keys = [correction_key(lo, hi) for lo, hi in old_ranges]
    if not any(k in layout.corrections for k in keys):
        return  # nic nie było poprawione — nie tworzymy korekty tam, gdzie jej nie było

    parts = [_effective_text(layout, tokens, lo, hi) for lo, hi in old_ranges]
    for key in keys:
        layout.corrections.pop(key, None)
    _store_correction(layout, tokens, new_lo, new_hi,
                      " ".join(p for p in parts if p))


def _split_correction(
    layout: Layout,
    tokens: Optional[Sequence[Token]],
    lo: int,
    hi: int,
    at: int,
) -> None:
    """
    Dzieli korektę dzielonej planszy na dwie części.

    Miejsce cięcia w tekście poprawionym wyznaczamy proporcjonalnie do liczby słów —
    dokładnego odpowiednika nie ma, bo użytkownik mógł dopisać albo skreślić słowa.
    Obie połowy zostają widoczne i edytowalne, więc nic nie ginie: to znacznie lepsze
    niż dotychczasowe ciche skasowanie całej korekty.
    """
    if tokens is None:
        return
    key = correction_key(lo, hi)
    text = layout.corrections.pop(key, None)
    if text is None:
        return

    words = text.split()
    if not words:
        return
    cut = round(len(words) * (at - lo) / (hi - lo)) if hi > lo else 0
    cut = max(0, min(cut, len(words)))
    _store_correction(layout, tokens, lo, at, " ".join(words[:cut]))
    _store_correction(layout, tokens, at, hi, " ".join(words[cut:]))


def _shift_correction(
    layout: Layout,
    tokens: Optional[Sequence[Token]],
    old_range: Tuple[int, int],
    new_range: Tuple[int, int],
) -> None:
    """
    Przepina korektę na przesunięty zakres słów, nie zmieniając jej treści.

    Przy przesuwaniu granicy tekst poprawiony zostaje nietknięty. Rozdzielanie go
    proporcjonalnie byłoby tu szkodliwe: korekta bywa dużo krótsza od zastępowanego
    fragmentu (użytkownik skraca zdanie), więc „proporcja" mieszałaby jego słowa ze
    słowami książki. Granica przesuwa się zwykle o jedno słowo, a właściciel tekstu
    i tak widzi obie plansze i może je dopieścić.
    """
    if tokens is None:
        return
    text = layout.corrections.pop(correction_key(*old_range), None)
    if text is None:
        return
    _store_correction(layout, tokens, new_range[0], new_range[1], text)


def split_board(layout: Layout, board_index: int, token_index: int,
                tokens: Optional[Sequence[Token]] = None) -> Layout:
    """Dzieli planszę na dwie w podanym tokenie (staje się on początkiem drugiej z nich)."""
    bounds = layout.bounds()
    if not 0 <= board_index < layout.board_count:
        raise LayoutError(f"Nie ma planszy o numerze {board_index + 1}.")

    lo, hi = bounds[board_index], bounds[board_index + 1]
    if not lo < token_index < hi:
        raise LayoutError(
            f"Podział musi wypaść wewnątrz planszy {board_index + 1} "
            f"(słowa {lo + 1}–{hi}), a wskazano słowo {token_index + 1}."
        )

    layout.breaks = sorted(layout.breaks + [token_index])
    _split_correction(layout, tokens, lo, hi, token_index)
    return layout


def merge_boards(layout: Layout, board_index: int,
                 tokens: Optional[Sequence[Token]] = None) -> Layout:
    """Scala planszę z następną."""
    if not 0 <= board_index < layout.board_count - 1:
        raise LayoutError(
            f"Plansza {board_index + 1} nie ma następnej, z którą można ją scalić."
        )
    bounds = layout.bounds()
    lo, mid, hi = bounds[board_index], bounds[board_index + 1], bounds[board_index + 2]

    removed = layout.breaks.pop(board_index)
    _drop_override(layout, removed)
    _merge_corrections(layout, tokens, [(lo, mid), (mid, hi)], lo, hi)
    return layout


def move_break(layout: Layout, break_index: int, token_index: int,
               tokens: Optional[Sequence[Token]] = None) -> Layout:
    """Przesuwa granicę o podanym numerze na inny token."""
    if not 0 <= break_index < len(layout.breaks):
        raise LayoutError(f"Nie ma granicy o numerze {break_index + 1}.")

    lower = layout.breaks[break_index - 1] if break_index > 0 else 0
    upper = layout.breaks[break_index + 1] if break_index + 1 < len(layout.breaks) else layout.token_count
    if not lower < token_index < upper:
        raise LayoutError(
            f"Granicę {break_index + 1} można przesunąć tylko między słowa "
            f"{lower + 2} i {upper} (wskazano {token_index + 1})."
        )

    old = layout.breaks[break_index]
    _drop_override(layout, old)
    layout.breaks[break_index] = token_index
    _shift_correction(layout, tokens, (lower, old), (lower, token_index))
    _shift_correction(layout, tokens, (old, upper), (token_index, upper))
    return layout


CUT_KEY_RE = re.compile(r"^(?:b(\d+)|i(\d+)\.(\d+)|end)$")


def validate_cut_key(layout: Layout, key: str) -> str:
    """
    Sprawdza, że klucz cięcia wskazuje na istniejącą granicę.

    Dla tekstu książki weryfikujemy to w pełni — granica musi stać na brzegu rozdziału
    albo na jednym z podziałów. Dla wstawek sprawdzamy tylko postać klucza: wstawka,
    której użytkownik jeszcze nie tknął, nie ma wpisu w podziale, a jej granice i tak
    są poprawnym celem.
    """
    key = _cut_key(key)
    m = CUT_KEY_RE.match(key)
    if not m:
        raise LayoutError(f"Niepoprawny identyfikator cięcia: {key!r}.")
    if m.group(1) is not None:
        token = int(m.group(1))
        if token not in (0, layout.token_count) and token not in layout.breaks:
            raise LayoutError(f"Na słowie {token + 1} nie stoi żadna granica.")
    return key


def set_cut_time(layout: Layout, key: str, time: Optional[float]) -> Layout:
    """
    Ustawia ręczny czas cięcia. `key` identyfikuje granicę: "b<slowo>" dla tekstu
    książki, "i<kotwica>.<plansza>" dla granicy wewnątrz wstawki, "end" dla końca
    rozdziału. Przekazanie None przywraca czas wyliczony automatycznie.
    """
    key = validate_cut_key(layout, key)
    if time is None:
        layout.overrides.pop(key, None)
    else:
        layout.overrides[key] = round(float(time), 3)
    return layout


def set_reviewed(layout: Layout, key: str, flag: bool = True) -> Layout:
    """
    Oznacza granicę jako obejrzaną w edytorze fali.

    Znacznik jest trwały, bo przeglądanie kilkuset cięć w jednym posiedzeniu jest
    nierealne — bez zapisu użytkownik po powrocie nie wie, gdzie skończył.
    """
    key = validate_cut_key(layout, key)
    if flag:
        if key not in layout.reviewed:
            layout.reviewed.append(key)
    elif key in layout.reviewed:
        layout.reviewed.remove(key)
    return layout


# ---------------------------------------------------------------------------
# Wstawki i korekty
# ---------------------------------------------------------------------------

def correction_key(lo: int, hi: int) -> str:
    return f"{lo}-{hi}"


def set_correction(layout: Layout, lo: int, hi: int, text: Optional[str]) -> Layout:
    """
    Poprawia tekst planszy książkowej bez ruszania czasów i podziału.

    Korekta jest przypisana do konkretnego zakresu słów. Gdy granica się przesunie,
    zakres przestaje istnieć i poprawka nie ma się do czego odnieść — dlatego
    `stale_corrections()` pokazuje takie sieroty, zamiast po cichu przyklejać je
    do sąsiedniej planszy.
    """
    key = correction_key(lo, hi)
    if text is None or not text.strip():
        layout.corrections.pop(key, None)
    else:
        layout.corrections[key] = text.strip()
    return layout


def stale_corrections(layout: Layout) -> List[str]:
    """Korekty wskazujące na zakresy słów, których po zmianie podziału już nie ma."""
    live = {correction_key(a, b) for a, b in zip(layout.bounds(), layout.bounds()[1:])}
    return sorted(set(layout.corrections) - live)


def insert_state(layout: Layout, anchor: int) -> Dict[str, Any]:
    """Stan wstawki: tekst wpisany przez użytkownika i jej własny podział."""
    return layout.inserts.get(str(anchor), {"text": None, "breaks": [], "edited": False})


def set_insert_text(layout: Layout, anchor: int, text: str) -> Layout:
    """
    Wpisuje treść wstawki.

    Zmiana tekstu unieważnia jej podział na plansze — indeksy słów odnosiłyby się
    do poprzedniej wersji, więc granice wypadłyby w przypadkowych miejscach.
    Czasy cięć wewnątrz wstawki znikają z tego samego powodu.
    """
    key = str(anchor)
    state = dict(layout.inserts.get(key) or {})
    state["text"] = (text or "").strip()
    state["breaks"] = []
    state["edited"] = True
    layout.inserts[key] = state
    _drop_insert_cuts(layout, anchor)
    return layout


def _drop_insert_cuts(layout: Layout, anchor: int) -> None:
    prefix = f"i{anchor}."
    for k in [k for k in layout.overrides if k.startswith(prefix)]:
        layout.overrides.pop(k, None)
    layout.reviewed = [k for k in layout.reviewed if not k.startswith(prefix)]


def _drop_insert_cut(layout: Layout, anchor: int, board_index: int) -> None:
    """Kasuje ręczny czas i znacznik jednej granicy wstawki."""
    key = insert_cut_key(anchor, board_index)
    layout.overrides.pop(key, None)
    if key in layout.reviewed:
        layout.reviewed.remove(key)


def split_insert(layout: Layout, anchor: int, board_index: int, word_index: int,
                 word_count: int) -> Layout:
    """Dzieli planszę wstawki na dwie w podanym słowie."""
    state = dict(layout.inserts.get(str(anchor)) or {"text": None, "breaks": [], "edited": False})
    breaks = list(state.get("breaks") or [])
    bounds = [0] + breaks + [word_count]
    if not 0 <= board_index < len(bounds) - 1:
        raise LayoutError(f"Wstawka nie ma planszy o numerze {board_index + 1}.")
    lo, hi = bounds[board_index], bounds[board_index + 1]
    if not lo < word_index < hi:
        raise LayoutError(
            f"Podział musi wypaść wewnątrz planszy wstawki (słowa {lo + 1}–{hi})."
        )
    state["breaks"] = sorted(breaks + [word_index])
    layout.inserts[str(anchor)] = state
    return layout


def move_insert(layout: Layout, anchor: int, break_index: int, word_index: int,
                word_count: int) -> Layout:
    """
    Przesuwa granicę wewnątrz wstawki na inne słowo.

    Odpowiednik `move_break()` dla tekstu książki. Bez tego jedyną drogą do poprawienia
    granicy o jedno słowo było scalenie plansz i podzielenie ich od nowa — a to gubiło
    ręczne czasy wszystkich cięć wstawki.

    Granice wstawki numerujemy słowami, a cięcia — numerem planszy, którą otwierają.
    Granica `break_index` otwiera planszę `break_index + 1` i tylko jej ręczny czas
    traci sens po przesunięciu; pozostałe zostają nietknięte.
    """
    state = dict(layout.inserts.get(str(anchor)) or {"text": None, "breaks": [], "edited": False})
    breaks = list(state.get("breaks") or [])
    if not 0 <= break_index < len(breaks):
        raise LayoutError(f"Wstawka nie ma granicy o numerze {break_index + 1}.")

    lower = breaks[break_index - 1] if break_index > 0 else 0
    upper = breaks[break_index + 1] if break_index + 1 < len(breaks) else word_count
    if not lower < word_index < upper:
        raise LayoutError(
            f"Granicę {break_index + 1} wstawki można przesunąć tylko między słowa "
            f"{lower + 2} i {upper} (wskazano {word_index + 1})."
        )

    breaks[break_index] = word_index
    state["breaks"] = sorted(breaks)
    layout.inserts[str(anchor)] = state
    _drop_insert_cut(layout, anchor, break_index + 1)
    return layout


def merge_insert(layout: Layout, anchor: int, board_index: int) -> Layout:
    """Scala planszę wstawki z następną."""
    state = dict(layout.inserts.get(str(anchor)) or {"text": None, "breaks": [], "edited": False})
    breaks = list(state.get("breaks") or [])
    if not 0 <= board_index < len(breaks):
        raise LayoutError("Ta plansza wstawki nie ma następnej, z którą można ją scalić.")
    breaks.pop(board_index)
    state["breaks"] = breaks
    layout.inserts[str(anchor)] = state
    _drop_insert_cuts(layout, anchor)
    return layout


# ---------------------------------------------------------------------------
# Plansze jako widok pochodny
# ---------------------------------------------------------------------------

def boards_from_layout(
    layout: Layout,
    tokens: Sequence[Token],
    word_times: Optional[Sequence[Tuple[float, float]]] = None,
    max_chars_per_line: int = 45,
) -> List[Dict[str, Any]]:
    """
    Buduje plansze w formacie, który zjada exporter (te same klucze co dziś w chapter_*.json).

    ``word_times`` to czasy słów z alignmentu, po jednym na token. Gdy ich nie ma
    (etap przed alignmentem), czasy wychodzą zerowe — podział tekstu i tak jest
    od nich niezależny, co jest całym sensem tego rozdzielenia.
    """
    if layout.token_count != len(tokens):
        raise LayoutError(
            f"Podział opisuje {layout.token_count} słów, a rozdział ma {len(tokens)}. "
            "Tekst źródłowy zmienił się — podział trzeba zaproponować od nowa."
        )
    if word_times is not None and len(word_times) != len(tokens):
        raise LayoutError(
            f"Czasy słów ({len(word_times)}) nie pasują do liczby słów rozdziału ({len(tokens)})."
        )

    bounds = layout.bounds()
    boards: List[Dict[str, Any]] = []

    for i in range(len(bounds) - 1):
        lo, hi = bounds[i], bounds[i + 1]
        text = render_text(tokens, lo, hi)
        dialogue = hi > lo and all(tokens[k].is_dialogue for k in range(lo, hi))

        if word_times is None:
            start = end = 0.0
        else:
            start = float(layout.overrides.get(book_cut_key(lo), word_times[lo][0]))
            end = float(layout.overrides.get(book_cut_key(hi), word_times[hi - 1][1]))

        boards.append({
            "chunk_id": i + 1,
            "text": text,
            "chunk_type": "dialogue" if dialogue else "narration",
            "lines_count": estimate_line_count(text, max_chars_per_line),
            "start_time": round(start, 3),
            "end_time": round(end, 3),
            "duration": round(end - start, 3),
            "token_start": lo,
            "token_end": hi,
            "manual_start": book_cut_key(lo) in layout.overrides,
            "manual_end": book_cut_key(hi) in layout.overrides,
            "correction": layout.corrections.get(correction_key(lo, hi)),
        })

    return boards


def word_times_from_payload(payload: Dict[str, Any]) -> List[Tuple[float, float]]:
    """Czasy słów z chapter_*.json w formacie przyjmowanym przez boards_from_layout()."""
    words = payload.get("words")
    if not words:
        raise LayoutError(
            f"Rozdział {payload.get('chapter_num')} nie ma zapisanych czasów słów. "
            "Przetwórz go ponownie — starsze wyniki niosły tylko czasy plansz."
        )
    return [(float(w["s"]), float(w["e"])) for w in words]


def validate_layout(
    layout: Layout,
    tokens: Sequence[Token],
    max_lines: int = 11,
    max_chars_per_line: int = 45,
) -> List[Dict[str, Any]]:
    """
    Ostrzeżenia o planszach wykraczających poza limit ekranu.

    Świadomie tylko ostrzeżenia, nie blokada — użytkownik, który scala dwie plansze,
    zwykle wie, po co to robi, a twarde odbicie jego decyzji byłoby gorsze niż
    plansza o dwie linie za długa.
    """
    warnings: List[Dict[str, Any]] = []
    for board in boards_from_layout(layout, tokens, max_chars_per_line=max_chars_per_line):
        if board["lines_count"] > max_lines:
            warnings.append({
                "chunk_id": board["chunk_id"],
                "level": "warning",
                "message": f"Plansza {board['chunk_id']}: {board['lines_count']} linii "
                           f"przy limicie {max_lines}.",
            })
        elif not board["text"].strip():
            warnings.append({
                "chunk_id": board["chunk_id"],
                "level": "error",
                "message": f"Plansza {board['chunk_id']} jest pusta.",
            })
    return warnings


# ---------------------------------------------------------------------------
# Zapis
# ---------------------------------------------------------------------------

class LayoutStore:
    """Podziały leżą w Data/Layouts/ — osobno od wyników przetwarzania, bo przeżywają je."""

    # Podziały, które przestały pasować do tekstu, lądują tutaj zamiast zniknąć.
    # To godziny ręcznej pracy: granice, ręczne czasy cięć, znaczniki przejrzenia.
    ARCHIVE_DIRNAME = "Niezgodne"

    def __init__(self, data_dir: str):
        self.dir = os.path.join(data_dir, "Layouts")
        self.archive_dir = os.path.join(self.dir, self.ARCHIVE_DIRNAME)
        os.makedirs(self.dir, exist_ok=True)

    def path(self, chapter_num: int) -> str:
        return os.path.join(self.dir, f"chapter_{chapter_num:03d}.json")

    def _archive(self, chapter_num: int, reason: str) -> Optional[str]:
        """
        Odkłada podział, którego nie da się użyć, zamiast go kasować.

        Wcześniej niezgodny podział był po cichu pomijany i przy pierwszym zapisie
        nadpisywany propozycją automatu — bezpowrotnie. Teraz zostaje na dysku pod
        nazwą z powodem i znacznikiem czasu, więc da się do niego wrócić.
        """
        source = self.path(chapter_num)
        if not os.path.exists(source):
            return None
        os.makedirs(self.archive_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = f"chapter_{chapter_num:03d}_{stamp}_{reason}"
        # Znacznik czasu ma rozdzielczość sekundy, a dwa podziały potrafią wpaść tu
        # w tej samej sekundzie — bez licznika drugi nadpisałby pierwszy i archiwum
        # gubiłoby dokładnie to, przed czym ma chronić.
        target = os.path.join(self.archive_dir, f"{base}.json")
        seq = 1
        while os.path.exists(target):
            target = os.path.join(self.archive_dir, f"{base}-{seq}.json")
            seq += 1
        try:
            os.replace(source, target)
        except OSError as exc:
            print(f"[LayoutStore] Nie udało się odłożyć podziału rozdz. {chapter_num}: {exc}")
            return None
        print(f"[LayoutStore] Podział rozdz. {chapter_num} odłożono do {target}")
        return target

    def archived(self, chapter_num: Optional[int] = None) -> List[str]:
        """Ścieżki odłożonych podziałów — dla rozdziału albo dla całego projektu."""
        if not os.path.isdir(self.archive_dir):
            return []
        prefix = f"chapter_{chapter_num:03d}_" if chapter_num is not None else "chapter_"
        return sorted(
            os.path.join(self.archive_dir, name)
            for name in os.listdir(self.archive_dir)
            if name.startswith(prefix) and name.endswith(".json")
        )

    def load(self, chapter_num: int, expected_hash: Optional[str] = None) -> Optional[Layout]:
        path = self.path(chapter_num)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                layout = Layout.from_dict(json.load(f))
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            print(f"[LayoutStore] Uszkodzony podział rozdz. {chapter_num}: {exc}")
            self._archive(chapter_num, "uszkodzony")
            return None

        if expected_hash and layout.text_hash != expected_hash:
            print(f"[LayoutStore] Podział rozdz. {chapter_num} dotyczy innej wersji tekstu "
                  f"— zostanie zaproponowany od nowa.")
            self._archive(chapter_num, "inny-tekst")
            return None
        return layout

    def save(self, layout: Layout) -> Layout:
        """
        Zapis atomowy: najpierw plik tymczasowy, potem podmiana.

        Zapis wprost do pliku docelowego zostawiał po przerwaniu obcięty JSON, a taki
        plik `load()` odrzuca — czyli awaria w trakcie zapisu kasowała cały podział
        rozdziału. `os.replace` jest niepodzielne, więc na dysku jest albo stara,
        albo nowa wersja.
        """
        path = self.path(layout.chapter_num)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(layout.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
        return layout

    def delete(self, chapter_num: int) -> bool:
        path = self.path(chapter_num)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False

    def load_or_propose(
        self,
        chapter: Chapter,
        tokens: Optional[List[Token]] = None,
        max_lines: int = 11,
        max_chars_per_line: int = 45,
    ) -> Tuple[Layout, List[Token], bool]:
        """Zwraca (layout, tokens, czy_nowy)."""
        tokens = tokenize_chapter(chapter) if tokens is None else tokens
        existing = self.load(chapter.number, expected_hash=tokens_hash(tokens))
        if existing:
            return existing, tokens, False
        return propose_layout(chapter, tokens, max_lines, max_chars_per_line), tokens, True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def load_chapters(data_dir: str) -> Tuple[List[Chapter], Dict[str, Any]]:
    """Rozdziały aktywnego projektu — bez dotykania pipeline'u i Whispera."""
    from .project import ProjectStore, DEFAULT_SETTINGS

    store = ProjectStore(data_dir)
    project = store.load()
    settings = {**DEFAULT_SETTINGS, **(project.settings or {})}

    if project.chapter_map:
        text = store.read_source_text(project)
        chapters = chapters_from_map(text, project.chapter_map, settings["max_chars_per_line"])
    else:
        if not project.text_file or not os.path.exists(project.text_file):
            raise FileNotFoundError("Brak pliku .txt w projekcie.")
        chapters = parse_book(project.text_file, settings["max_chars_per_line"])
    return chapters, settings


def _verify(chapter: Chapter, settings: Dict[str, Any], verbose: bool) -> Dict[str, int]:
    """
    Dowód, że nowa ścieżka odtwarza dzisiejszy podział.

    Porównujemy z chunkerem sekwencję słów każdej planszy (musi się zgadzać co do słowa)
    oraz tekst planszy. Różnice czysto białoznakowe liczymy osobno: biorą się z tego,
    że chunker skleja zdania spacją, a layout zachowuje łamanie z pliku źródłowego.
    """
    tokens = tokenize_chapter(chapter)
    layout = propose_layout(chapter, tokens,
                            settings["max_lines_per_board"], settings["max_chars_per_line"])
    boards = boards_from_layout(layout, tokens,
                                max_chars_per_line=settings["max_chars_per_line"])
    chunks = create_chunks_for_chapter(chapter.blocks,
                                       settings["max_lines_per_board"], settings["max_chars_per_line"])

    stats = {"boards": len(boards), "exact": 0, "whitespace_only": 0, "different": 0,
             "type_mismatch": 0, "count_mismatch": int(len(boards) != len(chunks))}

    for board, chunk in zip(boards, chunks):
        if board["text"] == chunk.text:
            stats["exact"] += 1
        elif re.sub(r"\s+", " ", board["text"]) == re.sub(r"\s+", " ", chunk.text):
            stats["whitespace_only"] += 1
            if verbose:
                print(f"    plansza {board['chunk_id']}: różnica tylko w białych znakach")
        else:
            stats["different"] += 1
            print(f"    plansza {board['chunk_id']} RÓŻNI SIĘ:")
            print(f"      chunker: {chunk.text[:90]!r}")
            print(f"      layout:  {board['text'][:90]!r}")
        if board["chunk_type"] != chunk.chunk_type:
            stats["type_mismatch"] += 1
            print(f"    plansza {board['chunk_id']}: typ {board['chunk_type']} != {chunk.chunk_type}")

    return stats


def _verify_times(chapter: Chapter, settings: Dict[str, Any], processed_dir: str,
                  verbose: bool) -> Optional[Dict[str, int]]:
    """
    Sprawdza, że plansze odtworzone z czasów słów zgadzają się z zapisem pipeline'u.

    Rozbieżności są oczekiwane w dwóch miejscach, oba w `aligner.py`: docisk początku
    planszy do końca poprzedniej (warunek monotoniczności) oraz wymuszone minimum
    pół sekundy na planszę. Nowy przepływ zastępuje jedno i drugie dosunięciem do ciszy
    i ręczną korektą, więc liczymy je osobno zamiast traktować jako błąd.

    Poprzednikiem planszy bywa wtrącenie lektora (intro), którego nie ma w podziale —
    dlatego docisk sprawdzamy względem pełnej listy zapisanych plansz, a nie samych
    plansz tekstowych.
    """
    path = os.path.join(processed_dir, f"chapter_{chapter.number:03d}.json")
    if not os.path.exists(path):
        print(f"  rozdział {chapter.number}: brak wyniku przetwarzania, pomijam")
        return None

    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    tokens = tokenize_chapter(chapter)
    times = word_times_from_payload(payload)
    if len(times) != len(tokens):
        print(f"  rozdział {chapter.number}: BŁĄD — {len(times)} czasów słów "
              f"przy {len(tokens)} słowach rozdziału")
        return {"boards": 0, "text_diff": 0, "time_exact": 0, "clamped": 0, "unexplained": 1}

    layout = propose_layout(chapter, tokens,
                            settings["max_lines_per_board"], settings["max_chars_per_line"])
    boards = boards_from_layout(layout, tokens, times, settings["max_chars_per_line"])
    stored = [c for c in payload["chunks"] if c["chunk_type"] != "intro_outro"]

    stats = {"boards": len(boards), "text_diff": 0, "time_exact": 0, "clamped": 0,
             "min_duration": 0, "unexplained": 0}
    if len(stored) != len(boards):
        print(f"  rozdział {chapter.number}: {len(boards)} plansz z podziału "
              f"vs {len(stored)} zapisanych")
        stats["unexplained"] += 1
        return stats

    all_chunks = payload["chunks"]
    positions = [i for i, c in enumerate(all_chunks) if c["chunk_type"] != "intro_outro"]

    for board, saved, pos in zip(boards, stored, positions):
        if board["text"] != saved["text"]:
            stats["text_diff"] += 1

        prev = all_chunks[pos - 1] if pos > 0 else None
        d_start = abs(board["start_time"] - saved["start_time"])
        d_end = abs(board["end_time"] - saved["end_time"])

        start_ok = d_start < 0.002 or (
            prev is not None and abs(saved["start_time"] - prev["end_time"]) < 0.002
        )
        clamped = start_ok and d_start >= 0.002
        # aligner.py: c_end = max(c_start + 0.5, c_end) — krótka plansza (np. sam tytuł)
        # dostaje wymuszone pół sekundy.
        stretched = d_end >= 0.002 and abs(saved["end_time"] - (saved["start_time"] + 0.5)) < 0.002
        end_ok = d_end < 0.002 or stretched

        if d_start < 0.002 and d_end < 0.002:
            stats["time_exact"] += 1
        elif start_ok and end_ok:
            stats["clamped"] += int(clamped)
            stats["min_duration"] += int(stretched)
        else:
            stats["unexplained"] += 1
            if verbose:
                print(f"    plansza {board['chunk_id']}: z podziału "
                      f"{board['start_time']:.2f}-{board['end_time']:.2f}, "
                      f"zapisane {saved['start_time']:.2f}-{saved['end_time']:.2f}")
    return stats


def _list_boards(layout: Layout, tokens: List[Token], settings: Dict[str, Any]) -> None:
    boards = boards_from_layout(layout, tokens, max_chars_per_line=settings["max_chars_per_line"])
    print(f"  {'nr':>3} {'słowa':>13} {'linii':>6} {'typ':>10}  tekst")
    for b in boards:
        flag = "!" if b["lines_count"] > settings["max_lines_per_board"] else " "
        preview = re.sub(r"\s+", " ", b["text"])[:58]
        print(f"  {b['chunk_id']:>3} {b['token_start'] + 1:>6}-{b['token_end']:<6} "
              f"{b['lines_count']:>5}{flag} {b['chunk_type']:>10}  {preview}")


def main(argv: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(description="Edytowalny podział rozdziału na plansze (etap 2).")
    parser.add_argument("--chapter", type=int, action="append")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--data-dir", default=os.path.join(base, "Data"))
    parser.add_argument("--verify", action="store_true",
                        help="Porównaj podział z dzisiejszym wynikiem chunkera")
    parser.add_argument("--verify-times", action="store_true",
                        help="Porównaj czasy plansz odtworzone z czasów słów z zapisem pipeline'u")
    parser.add_argument("--processed-dir", default=os.path.join(base, "Data", "Processed_JSON"))
    parser.add_argument("--list", action="store_true", help="Wypisz plansze")
    parser.add_argument("--split", nargs=2, type=int, metavar=("PLANSZA", "SLOWO"))
    parser.add_argument("--merge", type=int, metavar="PLANSZA")
    parser.add_argument("--move", nargs=2, type=int, metavar=("GRANICA", "SLOWO"))
    parser.add_argument("--reset", action="store_true", help="Wróć do propozycji chunkera")
    parser.add_argument("--save", action="store_true", help="Zapisz podział w Data/Layouts/")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    chapters, settings = load_chapters(args.data_dir)
    if args.chapter:
        wanted = set(args.chapter)
        chapters = [c for c in chapters if c.number in wanted]
    elif not args.all:
        parser.error("Podaj --chapter N albo --all.")

    if not chapters:
        print("Nie znaleziono rozdziałów.")
        return 1

    store = LayoutStore(args.data_dir)
    totals = {"boards": 0, "exact": 0, "whitespace_only": 0, "different": 0,
              "type_mismatch": 0, "count_mismatch": 0}

    time_totals = {"boards": 0, "text_diff": 0, "time_exact": 0, "clamped": 0,
                   "min_duration": 0, "unexplained": 0}

    for chapter in chapters:
        if args.verify_times:
            stats = _verify_times(chapter, settings, args.processed_dir, args.verbose)
            if stats:
                for k in time_totals:
                    time_totals[k] += stats[k]
            continue

        if args.verify:
            print(f"\nRozdział {chapter.number}: {chapter.header}")
            stats = _verify(chapter, settings, args.verbose)
            for k in totals:
                totals[k] += stats[k]
            print(f"  {stats['boards']} plansz | zgodnych co do znaku {stats['exact']}"
                  f" | różnice białych znaków {stats['whitespace_only']}"
                  f" | rozbieżnych {stats['different']}")
            continue

        if args.reset:
            store.delete(chapter.number)
        layout, tokens, fresh = store.load_or_propose(
            chapter, max_lines=settings["max_lines_per_board"],
            max_chars_per_line=settings["max_chars_per_line"])

        for op, fn in (("split", lambda: split_board(layout, args.split[0] - 1, args.split[1] - 1)),
                       ("merge", lambda: merge_boards(layout, args.merge - 1)),
                       ("move", lambda: move_break(layout, args.move[0] - 1, args.move[1] - 1))):
            if getattr(args, op):
                try:
                    fn()
                    print(f"  {op}: OK")
                except LayoutError as exc:
                    print(f"  {op}: {exc}")
                    return 2

        source = "nowy z chunkera" if fresh else "wczytany z dysku"
        print(f"\nRozdział {chapter.number}: {chapter.header}  "
              f"({layout.token_count} słów, {layout.board_count} plansz, podział {source})")
        if args.list:
            _list_boards(layout, tokens, settings)

        for w in validate_layout(layout, tokens, settings["max_lines_per_board"],
                                 settings["max_chars_per_line"]):
            print(f"  [{w['level']}] {w['message']}")

        if args.save:
            store.save(layout)
            print(f"  zapisano: {store.path(chapter.number)}")

    if args.verify_times:
        t = time_totals
        print("\n" + "=" * 74)
        print(f"WERYFIKACJA CZASÓW — {len(chapters)} rozdz., {t['boards']} plansz")
        print(f"  tekst planszy zgodny:                {t['boards'] - t['text_diff']}/{t['boards']}")
        print(f"  czasy zgodne co do milisekundy:      {t['time_exact']}")
        print(f"  różnica z docisku monotoniczności:   {t['clamped']}")
        print(f"  różnica z minimum 0,5 s na planszę:  {t['min_duration']}")
        print(f"  różnic niewyjaśnionych:              {t['unexplained']}")
        print("=" * 74)
        return 0 if not (t["unexplained"] or t["text_diff"]) else 3

    if args.verify:
        print("\n" + "=" * 74)
        print(f"WERYFIKACJA — {len(chapters)} rozdz., {totals['boards']} plansz")
        print(f"  zgodnych co do znaku:        {totals['exact']}")
        print(f"  różnica tylko białych znaków:{totals['whitespace_only']:>4}")
        print(f"  rozbieżnych:                 {totals['different']}")
        print(f"  niezgodny typ planszy:       {totals['type_mismatch']}")
        print(f"  niezgodna liczba plansz:     {totals['count_mismatch']} rozdz.")
        print("=" * 74)
        return 0 if not (totals["different"] or totals["type_mismatch"]
                         or totals["count_mismatch"]) else 3

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
