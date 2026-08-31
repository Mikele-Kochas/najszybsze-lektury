"""
Hybrydowe wyznaczanie granic rozdziałów w pojedynczym pliku .txt.

Strategia:
  1. HEADINGS - szukamy nagłówków ("Rozdział VII.", "ROZDZIAŁ 7", "VII.", "7.", "* * *").
     Testujemy kilka wzorców i wybieramy ten, który daje podział najbliższy liczbie plików MP3.
  2. ANCHOR   - gdy nagłówków brak lub jest ich zła liczba, transkrybujemy początek każdego
     MP3 i wyszukujemy ten fragment w tekście (fuzzy). Nie wymaga żadnych nagłówków.
  3. MANUAL   - użytkownik poprawia granice w kreatorze; wynik zapisujemy jako źródło 'manual'.
"""
import os
import re
import bisect
import difflib
from typing import List, Dict, Any, Optional, Tuple

from .transcriber import WhisperTranscriber

ROMAN_TO_INT = {
    'I': 1, 'II': 2, 'III': 3, 'IV': 4, 'V': 5, 'VI': 6, 'VII': 7, 'VIII': 8,
    'IX': 9, 'X': 10, 'XI': 11, 'XII': 12, 'XIII': 13, 'XIV': 14, 'XV': 15,
    'XVI': 16, 'XVII': 17, 'XVIII': 18, 'XIX': 19, 'XX': 20, 'XXI': 21,
    'XXII': 22, 'XXIII': 23, 'XXIV': 24, 'XXV': 25, 'XXVI': 26, 'XXVII': 27,
    'XXVIII': 28, 'XXIX': 29, 'XXX': 30, 'XXXI': 31, 'XXXII': 32, 'XXXIII': 33,
    'XXXIV': 34, 'XXXV': 35, 'XXXVI': 36, 'XXXVII': 37, 'XXXVIII': 38,
    'XXXIX': 39, 'XL': 40, 'XLI': 41, 'XLII': 42, 'XLIII': 43, 'XLIV': 44,
    'XLV': 45, 'XLVI': 46, 'XLVII': 47, 'XLVIII': 48, 'XLIX': 49, 'L': 50,
}

ROMAN_RE = r'(?:[IVXLC]+)'

# Liczebniki porządkowe słowne - "Rozdział pierwszy", "Część druga".
# Wolne Lektury używają ich zamiennie z cyframi, więc bez tego wykrywanie
# nagłówków przepuszcza całe książki (np. Doktor Dolittle).
_ONES_ORDINAL = [
    ("pierwsz", 1), ("drug", 2), ("trzec", 3), ("czwart", 4), ("piąt", 5),
    ("szóst", 6), ("siódm", 7), ("ósm", 8), ("dziewiąt", 9), ("dziesiąt", 10),
    ("jedenast", 11), ("dwunast", 12), ("trzynast", 13), ("czternast", 14),
    ("piętnast", 15), ("szesnast", 16), ("siedemnast", 17), ("osiemnast", 18),
    ("dziewiętnast", 19),
]
_TENS_ORDINAL = [("dwudziest", 20), ("trzydziest", 30), ("czterdziest", 40), ("pięćdziesiąt", 50)]
_ORDINAL_SUFFIXES = ("y", "i", "a", "e")


def _build_word_ordinals() -> Dict[str, int]:
    """Buduje słownik liczebników słownych 1-59 w rodzaju męskim i żeńskim."""
    table: Dict[str, int] = {}
    for stem, value in _ONES_ORDINAL:
        for suffix in _ORDINAL_SUFFIXES:
            table[stem + suffix] = value
    for tens_stem, tens_value in _TENS_ORDINAL:
        for suffix in _ORDINAL_SUFFIXES:
            table[tens_stem + suffix] = tens_value
        # Formy złożone: "dwudziesty pierwszy"
        for stem, value in _ONES_ORDINAL[:9]:
            for tens_suffix in _ORDINAL_SUFFIXES:
                for suffix in _ORDINAL_SUFFIXES:
                    table[f"{tens_stem}{tens_suffix} {stem}{suffix}"] = tens_value + value
    return table


WORD_ORDINALS = _build_word_ordinals()

# Najdłuższe warianty najpierw, żeby "dwudziesty pierwszy" wygrał z "dwudziesty".
WORD_ORDINAL_RE = "(?:" + "|".join(
    re.escape(k) for k in sorted(WORD_ORDINALS, key=len, reverse=True)
) + ")"

# Rozdziały bez numeru, traktowane jako pełnoprawne pozycje w audiobooku.
# "Ostatni rozdział" realnie występuje w wydaniach Wolnych Lektur zamiast numeru.
SPECIAL_HEADINGS = (
    "ostatni rozdział", "ostatni rozdzial", "rozdział ostatni", "rozdzial ostatni",
    "prolog", "epilog", "zakończenie", "zakonczenie", "posłowie", "poslowie",
)
SPECIAL_HEADING_RE = "(?P<special>" + "|".join(re.escape(s) for s in SPECIAL_HEADINGS) + ")"

# Wzorce nagłówków, od najbardziej do najmniej jednoznacznego.
# Każdy musi wystawić grupę 'num' (opcjonalnie) i 'title' (opcjonalnie).
HEADING_PATTERNS: List[Tuple[str, str]] = [
    (
        "rozdzial_named",
        r'^[ \t]*(?:'
        r'(?:rozdzia[łl]|cz[eę][śs][ćc]|ksi[eę]ga)[ \t]+'
        r'(?P<num>' + WORD_ORDINAL_RE + r'|' + ROMAN_RE + r'|\d{1,3})'
        r'|' + SPECIAL_HEADING_RE +
        r')[ \t]*[.:—–-]?[ \t]*(?P<title>[^\n]*)$'
    ),
    (
        "chapter_named",
        r'^[ \t]*chapter[ \t]+(?P<num>' + ROMAN_RE + r'|\d{1,3})'
        r'[ \t]*[.:—–-]?[ \t]*(?P<title>[^\n]*)$'
    ),
    # Nagłówki "gołe" muszą zajmować całą linię. Gdyby dopuścić tytuł w tej samej linii,
    # polskie zdanie zaczynające się od spójnika "I" albo numer strony dawałyby
    # fałszywe trafienia. Tytuł i tak wyciągamy z kolejnej linii.
    (
        "roman_bare",
        r'^[ \t]*(?P<num>' + ROMAN_RE + r'){1}[ \t]*[.:]?[ \t]*(?P<title>)$'
    ),
    (
        "arabic_bare",
        r'^[ \t]*(?P<num>\d{1,3})[ \t]*[.:]?[ \t]*(?P<title>)$'
    ),
    (
        "asterisks",
        r'^[ \t]*(?P<num>)(?P<title>(?:[*•·#—–-][ \t]*){3,})$'
    ),
]


def parse_chapter_number(token: str) -> Optional[int]:
    """Rozpoznaje numer rozdziału zapisany cyfrą, cyfrą rzymską lub słownie."""
    token = (token or "").strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    lowered = re.sub(r"\s+", " ", token.lower())
    if lowered in WORD_ORDINALS:
        return WORD_ORDINALS[lowered]
    return ROMAN_TO_INT.get(token.upper())


def normalize_with_map(text: str) -> Tuple[str, List[int]]:
    """
    Normalizuje tekst do porównań (małe litery, tylko alfanumeryczne, pojedyncze spacje)
    i zwraca mapę: pozycja w tekście znormalizowanym -> pozycja w tekście oryginalnym.
    """
    out_chars: List[str] = []
    index_map: List[int] = []
    prev_space = True  # zapobiega wiodącej spacji
    for idx, ch in enumerate(text):
        if ch.isalnum():
            out_chars.append(ch.lower())
            index_map.append(idx)
            prev_space = False
        elif ch.isspace() or not ch.isalnum():
            if not prev_space:
                out_chars.append(" ")
                index_map.append(idx)
                prev_space = True
    return "".join(out_chars), index_map


def normalize_text_for_search(text: str) -> str:
    """Wersja bez mapy indeksów - do szybkich porównań fraz."""
    return normalize_with_map(text)[0]


# --------------------------------------------------------------------------
# 1. Wykrywanie po nagłówkach
# --------------------------------------------------------------------------

def _find_headings_with_pattern(text: str, pattern: str) -> List[Dict[str, Any]]:
    regex = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    found: List[Dict[str, Any]] = []
    for m in regex.finditer(text):
        header = m.group(0).strip()
        if not header:
            continue
        num_token = (m.groupdict().get("num") or "").strip()
        title = (m.groupdict().get("title") or "").strip(" .:—–-")
        if not title:
            title = _title_from_next_line(text, m.end())
        found.append({
            "match_start": m.start(),
            "body_start": m.end(),
            "header": header if title in header else f"{header} {title}".strip(),
            "number": parse_chapter_number(num_token),
            "title": title,
        })
    return found


def _title_from_next_line(text: str, body_start: int, max_len: int = 90) -> str:
    """
    Część wydań trzyma tytuł w osobnym akapicie pod nagłówkiem
    ("Rozdział drugi.\\n\\nMiasteczko rybackie"). Wyciągamy go do etykiety,
    ale zostawiamy w treści rozdziału - lektor zwykle go czyta.
    """
    tail = text[body_start:body_start + 400]
    for line in tail.split("\n"):
        candidate = line.strip()
        if not candidate:
            continue
        # Tytuł to krótka linia bez znaków końca zdania i bez myślnika dialogowego.
        if len(candidate) <= max_len and not candidate[0] in "—–-" and not candidate.endswith(('.', '!', '?', ':')):
            return candidate
        return ""
    return ""


def _score_headings(headings: List[Dict[str, Any]], text_len: int, expected_count: int) -> float:
    """
    Ocenia jakość podziału. Preferuje: liczbę zgodną z liczbą plików audio,
    rosnącą numerację i równomierne odstępy (brak rozdziałów o zerowej długości).
    """
    if len(headings) < 2:
        return 0.0

    score = 0.0
    if expected_count > 0:
        diff = abs(len(headings) - expected_count)
        score += max(0.0, 1.0 - diff / max(1.0, expected_count)) * 3.0
    else:
        score += 1.0

    numbers = [h["number"] for h in headings if h["number"] is not None]
    if len(numbers) == len(headings) and numbers == sorted(numbers) and len(set(numbers)) == len(numbers):
        score += 1.5

    spans = [
        (headings[i + 1]["match_start"] if i + 1 < len(headings) else text_len) - headings[i]["body_start"]
        for i in range(len(headings))
    ]
    tiny = sum(1 for s in spans if s < 400)
    score -= tiny * 0.5

    avg = sum(spans) / len(spans)
    if avg > 0:
        variance = sum((s - avg) ** 2 for s in spans) / len(spans)
        cv = (variance ** 0.5) / avg
        score += max(0.0, 1.5 - cv)

    return score


def detect_chapters_by_headings(text: str, expected_count: int = 0) -> Tuple[List[Dict[str, Any]], str, float]:
    """
    Zwraca (nagłówki, nazwa_wzorca, wynik). Pusta lista gdy żaden wzorzec nie daje sensownego podziału.
    """
    best: List[Dict[str, Any]] = []
    best_name = ""
    best_score = 0.0

    for name, pattern in HEADING_PATTERNS:
        headings = _find_headings_with_pattern(text, pattern)
        if len(headings) < 2:
            continue
        score = _score_headings(headings, len(text), expected_count)
        if score > best_score:
            best, best_name, best_score = headings, name, score

    return best, best_name, round(best_score, 3)


# --------------------------------------------------------------------------
# 2. Wykrywanie po kotwicy audio
# --------------------------------------------------------------------------

def find_anchor_in_text(
    query_words: List[str],
    book_norm: str,
    index_map: List[int],
    search_from_norm: int = 0,
) -> Tuple[int, float]:
    """
    Szuka fragmentu transkrypcji w znormalizowanym tekście książki, zaczynając od
    search_from_norm (wymusza monotoniczność kolejnych rozdziałów).
    Zwraca (indeks_w_tekscie_oryginalnym, pewnosc 0..1).
    """
    words = [w for w in (query_words or []) if w.strip()]
    if len(words) < 4:
        return -1, 0.0

    query_norm = normalize_text_for_search(" ".join(words))
    if len(query_norm) < 20:
        return -1, 0.0

    haystack = book_norm[search_from_norm:]
    if not haystack:
        return -1, 0.0

    best_pos, best_score = -1, 0.0

    # Kotwice nasienne: lektor często dodaje tytuł/wstęp, więc próbujemy też
    # okien przesuniętych o kilka słów w głąb transkrypcji.
    for offset in (0, 2, 4, 6, 9, 12):
        if offset + 5 > len(words):
            break
        seed = normalize_text_for_search(" ".join(words[offset:offset + 5]))
        if len(seed) < 12:
            continue
        # Porównujemy tylko tę część transkrypcji, która zaczyna się od kotwicy -
        # pominięty wstęp lektora nie występuje w książce i zaniżałby wynik.
        tail_norm = normalize_text_for_search(" ".join(words[offset:]))
        for m in re.finditer(re.escape(seed), haystack):
            start = m.start()
            window = haystack[start:start + len(tail_norm) + 80]
            ratio = difflib.SequenceMatcher(None, tail_norm, window).ratio()
            if ratio > best_score:
                best_score, best_pos = ratio, start
        if best_score >= 0.75:
            break

    if best_pos < 0:
        # Brak dokładnego nasienia - zgrubne skanowanie oknem.
        step = max(200, len(query_norm) // 2)
        for pos in range(0, max(1, len(haystack) - len(query_norm)), step):
            window = haystack[pos:pos + len(query_norm) + 100]
            ratio = difflib.SequenceMatcher(None, query_norm, window).ratio()
            if ratio > best_score:
                best_score, best_pos = ratio, pos

    if best_pos < 0:
        return -1, 0.0

    norm_index = search_from_norm + best_pos
    orig_index = index_map[norm_index] if norm_index < len(index_map) else len(index_map)
    return orig_index, round(best_score, 3)


class ChapterMatcher:
    """Wyznacza granice rozdziałów w tekście książki na podstawie plików audio."""

    def __init__(self, transcriber: Optional[WhisperTranscriber] = None):
        self.transcriber = transcriber or WhisperTranscriber()

    def anchors_from_audio(
        self,
        audio_files: List[str],
        full_text: str,
        sample_seconds: float = 45.0,
        language: str = "pl",
        progress_cb=None,
    ) -> List[Dict[str, Any]]:
        """Dla każdego pliku audio wyznacza pozycję startu rozdziału w tekście."""
        book_norm, index_map = normalize_with_map(full_text)
        anchors: List[Dict[str, Any]] = []
        cursor_norm = 0

        for idx, audio_path in enumerate(audio_files, start=1):
            if progress_cb:
                progress_cb(
                    (idx - 1) / max(1, len(audio_files)),
                    f"Kotwica {idx}/{len(audio_files)}: {os.path.basename(audio_path)}",
                )
            words = self.transcriber.transcribe_snippet(
                audio_path, seconds=sample_seconds, language=language
            )
            pos, conf = find_anchor_in_text(words, book_norm, index_map, search_from_norm=cursor_norm)

            if pos < 0:
                pos = index_map[cursor_norm] if cursor_norm < len(index_map) else len(full_text)
                conf = 0.0
            else:
                # Kolejnego rozdziału szukamy dopiero za bieżącą kotwicą.
                # index_map jest rosnąca, więc pozycję znajdujemy binarnie.
                consumed = bisect.bisect_left(index_map, pos)
                cursor_norm = max(cursor_norm, consumed + 50)

            anchors.append({
                "chapter_num": idx,
                "audio_file": os.path.basename(audio_path),
                "audio_path": audio_path,
                "text_start": pos,
                "confidence": conf,
                "transcript_head": " ".join(words[:14]),
            })

        if progress_cb:
            progress_cb(1.0, "Kotwice wyznaczone.")
        return anchors

    def reconcile_with_audio(
        self,
        full_text: str,
        chapters: List[Dict[str, Any]],
        audio_files: List[str],
        language: str = "pl",
        progress_cb=None,
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """
        Uzgadnia listę rozdziałów z listą nagrań, gdy liczby się nie zgadzają.

        Typowy przypadek: w pliku .txt brakuje jednego nagłówka, więc dwa rozdziały
        są sklejone w jeden blok, a w audio istnieją jako osobne pliki. Wykrywamy to
        po tempie czytania (znaki tekstu na sekundę nagrania), a punkt podziału
        znajdujemy kotwicą - transkrypcją początku nadmiarowego nagrania.
        """
        from .transcriber import probe_duration

        warnings: List[str] = []
        if not chapters or not audio_files:
            return chapters, warnings

        durations = [probe_duration(p) for p in audio_files]
        total_duration = sum(durations)
        total_chars = sum(c["text_end"] - c["text_start"] for c in chapters)
        if total_duration <= 0 or total_chars <= 0:
            return chapters, warnings

        rate = total_chars / total_duration  # znaki tekstu na sekundę nagrania

        # Krok 1: przypisz nagrania do rozdziałów zachłannie, wg oczekiwanego czasu trwania.
        assignments: List[List[int]] = []
        audio_idx = 0
        for ch_idx, chapter in enumerate(chapters):
            if audio_idx >= len(audio_files):
                assignments.append([])
                continue
            expected = (chapter["text_end"] - chapter["text_start"]) / rate
            taken = [audio_idx]
            accumulated = durations[audio_idx]
            audio_idx += 1
            chapters_left = len(chapters) - ch_idx - 1
            while (
                audio_idx < len(audio_files)
                and (len(audio_files) - audio_idx) > chapters_left
                and abs(accumulated + durations[audio_idx] - expected) < abs(accumulated - expected)
            ):
                taken.append(audio_idx)
                accumulated += durations[audio_idx]
                audio_idx += 1
            assignments.append(taken)

        if audio_idx < len(audio_files):
            leftover = [os.path.basename(audio_files[i]) for i in range(audio_idx, len(audio_files))]
            warnings.append(f"Nagrania bez przypisanego rozdziału: {', '.join(leftover)}.")

        if all(len(a) <= 1 for a in assignments):
            for ch_idx, chapter in enumerate(chapters):
                taken = assignments[ch_idx]
                if taken:
                    chapter["audio_path"] = audio_files[taken[0]]
                    chapter["audio_file"] = os.path.basename(audio_files[taken[0]])
                else:
                    chapter["audio_path"] = chapter["audio_file"] = None
                    warnings.append(f"Rozdział {chapter['chapter_num']} nie ma przypisanego nagrania.")
            return chapters, warnings

        # Krok 2: rozdziały obsługiwane przez kilka nagrań trzeba rozciąć w tekście.
        book_norm, index_map = normalize_with_map(full_text)
        result: List[Dict[str, Any]] = []

        for ch_idx, chapter in enumerate(chapters):
            taken = assignments[ch_idx]
            if len(taken) <= 1:
                path = audio_files[taken[0]] if taken else None
                chapter["audio_path"] = path
                chapter["audio_file"] = os.path.basename(path) if path else None
                if not path:
                    warnings.append(f"Rozdział {chapter['chapter_num']} nie ma przypisanego nagrania.")
                result.append(chapter)
                continue

            names = ", ".join(os.path.basename(audio_files[i]) for i in taken)
            warnings.append(
                f"„{chapter['header']}” odpowiada {len(taken)} nagraniom ({names}) — "
                "w tekście prawdopodobnie brakuje nagłówka. Dzielę rozdział automatycznie."
            )

            boundaries = [chapter["text_start"]]
            for sub_idx, audio_i in enumerate(taken[1:], start=1):
                if progress_cb:
                    progress_cb(
                        ch_idx / max(1, len(chapters)),
                        f"Szukam początku {os.path.basename(audio_files[audio_i])} w tekście...",
                    )
                words = self.transcriber.transcribe_snippet(
                    audio_files[audio_i], seconds=45.0, language=language
                )
                search_from = bisect.bisect_left(index_map, boundaries[-1] + 200)
                pos, conf = find_anchor_in_text(words, book_norm, index_map, search_from_norm=search_from)

                if pos <= boundaries[-1] or pos >= chapter["text_end"] or conf < 0.45:
                    # Kotwica zawiodła - dzielimy proporcjonalnie do długości nagrań.
                    span = chapter["text_end"] - chapter["text_start"]
                    share = sum(durations[i] for i in taken[:sub_idx]) / sum(durations[i] for i in taken)
                    pos = chapter["text_start"] + int(span * share)
                    conf = 0.0
                    warnings.append(
                        f"Nie znalazłem pewnego punktu podziału dla "
                        f"{os.path.basename(audio_files[audio_i])} — użyto podziału proporcjonalnego. "
                        "Zweryfikuj granicę w kreatorze."
                    )
                boundaries.append(pos)
            boundaries.append(chapter["text_end"])

            for sub_idx, audio_i in enumerate(taken):
                path = audio_files[audio_i]
                start, end = boundaries[sub_idx], boundaries[sub_idx + 1]
                result.append({
                    **chapter,
                    "header": chapter["header"] if sub_idx == 0 else f"{chapter['header']} (cz. {sub_idx + 1})",
                    "text_start": start,
                    "text_end": end,
                    "audio_file": os.path.basename(path),
                    "audio_path": path,
                    "confidence": chapter.get("confidence", 1.0) if sub_idx == 0 else 0.6,
                    "source": chapter.get("source", "headings") if sub_idx == 0 else "headings+anchor",
                    "snippet": full_text[start:start + 160].strip(),
                })

        for idx, chapter in enumerate(result, start=1):
            chapter["chapter_num"] = idx

        return result, warnings

    def build_chapter_map(
        self,
        full_text: str,
        audio_files: List[str],
        language: str = "pl",
        allow_audio_anchor: bool = True,
        progress_cb=None,
    ) -> Dict[str, Any]:
        """
        Buduje propozycję mapy rozdziałów. Zwraca słownik z listą 'chapters',
        użytą metodą i ostrzeżeniami do pokazania w kreatorze.
        """
        audio_files = sorted(audio_files)
        warnings: List[str] = []

        headings, pattern_name, score = detect_chapters_by_headings(full_text, len(audio_files))
        use_headings = bool(headings) and score >= 1.5

        if use_headings:
            chapters = self._chapters_from_headings(full_text, headings, audio_files, pattern_name)
            method = f"headings:{pattern_name}"

            if audio_files and len(chapters) != len(audio_files):
                warnings.append(
                    f"Znaleziono {len(headings)} nagłówków w tekście, ale {len(audio_files)} nagrań — "
                    "uzgadniam podział na podstawie długości nagrań."
                )
                chapters, fix_warnings = self.reconcile_with_audio(
                    full_text, chapters, audio_files, language=language, progress_cb=progress_cb
                )
                warnings.extend(fix_warnings)
                if len(chapters) != len(headings):
                    method = f"headings:{pattern_name}+reconciled"
        elif allow_audio_anchor and audio_files:
            warnings.append(
                "Nie wykryto wiarygodnych nagłówków rozdziałów - użyto dopasowania po nagraniach (audio anchor)."
            )
            anchors = self.anchors_from_audio(
                audio_files, full_text, language=language, progress_cb=progress_cb
            )
            chapters = self._chapters_from_anchors(full_text, anchors)
            method = "anchor"
        else:
            warnings.append("Nie udało się podzielić tekstu - cała książka trafiła do jednego rozdziału.")
            chapters = [{
                "chapter_num": 1,
                "header": "Rozdział 1",
                "title": "",
                "text_start": 0,
                "text_end": len(full_text),
                "audio_file": os.path.basename(audio_files[0]) if audio_files else None,
                "audio_path": audio_files[0] if audio_files else None,
                "confidence": 0.0,
                "source": "fallback",
                "snippet": full_text[:160].strip(),
            }]
            method = "fallback"

        low_conf = [c for c in chapters if c.get("confidence", 0) < 0.5]
        if method == "anchor" and low_conf:
            warnings.append(
                f"{len(low_conf)} rozdział(ów) dopasowano z niską pewnością - zweryfikuj je ręcznie."
            )

        return {
            "method": method,
            "heading_score": score,
            "warnings": warnings,
            "chapters": chapters,
        }

    @staticmethod
    def _chapters_from_headings(
        full_text: str,
        headings: List[Dict[str, Any]],
        audio_files: List[str],
        pattern_name: str,
    ) -> List[Dict[str, Any]]:
        chapters = []
        for i, h in enumerate(headings):
            body_start = h["body_start"]
            body_end = headings[i + 1]["match_start"] if i + 1 < len(headings) else len(full_text)
            audio_path = audio_files[i] if i < len(audio_files) else None
            chapters.append({
                "chapter_num": h["number"] if h["number"] is not None else i + 1,
                "header": h["header"],
                "title": h["title"],
                "text_start": body_start,
                "text_end": body_end,
                "audio_file": os.path.basename(audio_path) if audio_path else None,
                "audio_path": audio_path,
                "confidence": 1.0,
                "source": f"headings:{pattern_name}",
                "snippet": full_text[body_start:body_start + 160].strip(),
            })
        # Numery muszą być unikalne i rosnące - inaczej pliki wyjściowe się nadpiszą.
        seen = set()
        for i, c in enumerate(chapters):
            if c["chapter_num"] in seen or c["chapter_num"] is None:
                c["chapter_num"] = i + 1
            seen.add(c["chapter_num"])
        return chapters

    @staticmethod
    def _chapters_from_anchors(full_text: str, anchors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        chapters = []
        for i, a in enumerate(anchors):
            start = a["text_start"]
            end = anchors[i + 1]["text_start"] if i + 1 < len(anchors) else len(full_text)
            if end <= start:
                end = len(full_text)
            chapters.append({
                "chapter_num": i + 1,
                "header": f"Rozdział {i + 1}",
                "title": "",
                "text_start": start,
                "text_end": end,
                "audio_file": a["audio_file"],
                "audio_path": a["audio_path"],
                "confidence": a["confidence"],
                "source": "anchor",
                "snippet": full_text[start:start + 160].strip(),
                "transcript_head": a.get("transcript_head", ""),
            })
        return chapters
