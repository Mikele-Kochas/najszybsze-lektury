"""
Wyznaczanie timestampów dla zatwierdzonego podziału na plansze.

Tu spinają się trzy wcześniejsze warstwy:

    layout.py          — gdzie w tekście stoją granice (decyzja użytkownika)
    chapter_*.json     — czasy pojedynczych słów z alignmentu
    audio_analysis.py  — gdzie w nagraniu jest cisza

Rozdział jest ciągiem **jednostek**. Jednostka to plansza tekstu z książki albo
plansza wstawki — fragmentu, który słychać w nagraniu, ale którego nie ma w pliku
.txt (wstęp lektora, stopka, brakujący akapit). Obie mają to samo: tekst i czasy
swoich słów. Dzięki temu wstawka nie jest wyjątkiem doklejanym na końcu, tylko
zwykłą planszą — da się ją pociąć i dostać dla niej cięcia tą samą drogą.

Każda granica dostaje jeden punkt cięcia i komplet danych, na podstawie których
człowiek może ocenić, czy mu ufa: skąd czas pochodzi, ile ciszy zostaje po obu
stronach i czy lektor w ogóle zrobił tam przerwę. Plansze przylegają do siebie —
jedna sekunda nagrania należy do dokładnie jednej planszy.

Kolejność pierwszeństwa przy ustalaniu czasu cięcia:

    1. ręczne ustawienie z edytora fali   (source='manual')
    2. środek najbliższej ciszy           (source='silence')
    3. środek luki między słowami         (source='words')  — kandydat do poprawki

    python -m Engine.timing --chapter 21
    python -m Engine.timing --all --quiet
"""

from __future__ import annotations

import os
import re
import sys
import json
import difflib
import argparse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .text_parser import Chapter, estimate_line_count
from .aligner import normalize_word
from .layout import (
    Layout, LayoutError, LayoutStore, Token,
    tokenize_chapter, render_text, word_times_from_payload, load_chapters,
    book_cut_key, insert_cut_key, correction_key, insert_state,
)
from .audio_analysis import (
    AudioAnalysis, analyze_audio, resolve_audio_path,
    snap_boundary, snap_head, snap_tail, cut_safety,
    DEFAULT_SEARCH_WINDOW, DEFAULT_MIN_SILENCE, DEFAULT_HEAD_PAD, DEFAULT_TAIL_PAD,
    SAFE_MARGIN,
)

# Wtrącenie lektora dostaje tekst, który faktycznie słychać w nagraniu, plus znacznik
# mówiący, że w pliku .txt tego fragmentu nie ma. Sam znacznik zamiast treści zmuszał
# do odsłuchiwania każdej takiej planszy, żeby w ogóle wiedzieć, co się w niej dzieje.
NO_SOURCE_MARKER = "(brak tekstu w pliku txt)"
MIN_BOARD_SECONDS = 0.20
END_CUT_KEY = "end"


# ---------------------------------------------------------------------------
# Wstawki: tekst i czasy słów
# ---------------------------------------------------------------------------

def heard_text(segments: Sequence[Dict[str, Any]], start: float, end: float) -> str:
    """Tekst usłyszany przez Whispera w podanym przedziale, sklejony w jedną całość."""
    parts = []
    for seg in segments:
        if float(seg["end"]) > start + 0.1 and float(seg["start"]) < end - 0.1:
            text = str(seg.get("text", "")).strip()
            if text:
                parts.append(text)
    return " ".join(parts).strip()


def extra_board_text(heard: str) -> str:
    """Treść planszy wtrącenia: to, co słychać, plus znacznik braku w pliku źródłowym."""
    return heard + "\n" + NO_SOURCE_MARKER if heard else NO_SOURCE_MARKER


def strip_marker(text: str) -> str:
    """Tekst wstawki bez znacznika — do liczenia słów i dopasowania do nagrania."""
    return re.sub(r"\(brak tekstu[^)]*\)", "", text or "").strip()


def align_free_text(
    words: Sequence[str],
    whisper_words: Sequence[Dict[str, Any]],
    start: float,
    end: float,
) -> List[Tuple[float, float]]:
    """
    Czasy słów tekstu wpisanego ręcznie, przez dopasowanie do słów Whispera.

    Użytkownik przepisuje wstawkę ze słuchu i prawie nigdy nie trafi w to samo
    brzmienie co Whisper — przepisze poprawnie to, co model przekręcił, doda
    interpunkcję, rozwinie skrót. Dlatego dopasowujemy sekwencje, a słowa bez pary
    dostają czasy z interpolacji między najbliższymi trafieniami. To ta sama zasada,
    która działa dla tekstu książki, tylko na krótszym odcinku.
    """
    if not words:
        return []
    if not whisper_words:
        step = (end - start) / len(words)
        return [(round(start + i * step, 3), round(start + (i + 1) * step, 3))
                for i in range(len(words))]

    user_norm = [normalize_word(w) for w in words]
    heard_norm = [normalize_word(str(w.get("w", ""))) for w in whisper_words]

    times: List[Optional[Tuple[float, float]]] = [None] * len(words)
    matcher = difflib.SequenceMatcher(None, heard_norm, user_norm, autojunk=False)
    for h_start, u_start, length in matcher.get_matching_blocks():
        for k in range(length):
            h, u = h_start + k, u_start + k
            if h < len(whisper_words) and u < len(words):
                times[u] = (float(whisper_words[h]["s"]), float(whisper_words[h]["e"]))

    return _interpolate(times, start, end)


def _interpolate(times: List[Optional[Tuple[float, float]]],
                 start: float, end: float) -> List[Tuple[float, float]]:
    """Uzupełnia luki między dopasowanymi słowami, rozkładając je równomiernie."""
    n = len(times)
    anchors = [i for i, t in enumerate(times) if t is not None]
    if not anchors:
        step = (end - start) / n
        return [(round(start + i * step, 3), round(start + (i + 1) * step, 3)) for i in range(n)]

    def fill(lo: int, hi: int, t0: float, t1: float) -> None:
        count = hi - lo
        if count <= 0:
            return
        step = max(0.02, (t1 - t0) / (count + 1))
        for k in range(count):
            a = t0 + k * step
            times[lo + k] = (round(a, 3), round(a + step, 3))

    fill(0, anchors[0], start, times[anchors[0]][0])
    for a, b in zip(anchors, anchors[1:]):
        fill(a + 1, b, times[a][1], times[b][0])
    fill(anchors[-1] + 1, n, times[anchors[-1]][1], end)

    return [t if t is not None else (start, end) for t in times]


# ---------------------------------------------------------------------------
# Jednostki rozdziału
# ---------------------------------------------------------------------------

@dataclass
class Unit:
    """Jedna plansza: tekst z książki albo plansza wstawki."""
    key: str                                   # identyfikator cięcia otwierającego
    kind: str                                  # 'book' | 'insert'
    text: str
    times: List[Tuple[float, float]]
    dialogue: bool = False
    token_start: int = -1                      # tylko dla 'book'
    token_end: int = -1
    anchor: int = -1                           # tylko dla 'insert'
    board: int = -1
    edited: bool = False
    corrected: bool = False


def _extras_by_anchor(extras: Sequence[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    return {int(e.get("anchor", 0)): e for e in extras or []}


def _insert_units(
    layout: Layout,
    extra: Dict[str, Any],
    segments: Sequence[Dict[str, Any]] = (),
) -> List[Unit]:
    """
    Plansze jednej wstawki wraz z czasami jej słów.

    Wyniki przetworzone przed wprowadzeniem wstawek niosą sam zakres czasu, bez tekstu
    ani słów Whispera. Zamiast pokazywać wtedy pustą planszę, odczytujemy treść
    z transkrypcji rozdziału — czasy wychodzą z rozłożenia równomiernego, więc
    ponowne przetworzenie rozdziału nadal je poprawi.
    """
    anchor = int(extra.get("anchor", 0))
    state = insert_state(layout, anchor)
    start, end = float(extra["start_time"]), float(extra["end_time"])

    fallback = extra.get("heard") or heard_text(segments, start, end)
    raw = state.get("text") if state.get("edited") else extra.get("text")
    text = raw if raw is not None else extra_board_text(fallback)
    if not strip_marker(text) and fallback:
        text = extra_board_text(fallback)

    body = strip_marker(text)
    words = body.split()

    if not words:
        # Nic nie wpisano i nic nie słychać — zostaje sam znacznik na całym odcinku.
        return [Unit(key=insert_cut_key(anchor, 0), kind="insert", text=text,
                     times=[(start, end)], anchor=anchor, board=0,
                     edited=bool(state.get("edited")))]

    times = align_free_text(words, extra.get("words") or [], start, end)
    breaks = [b for b in (state.get("breaks") or []) if 0 < b < len(words)]
    bounds = [0] + sorted(set(breaks)) + [len(words)]

    units: List[Unit] = []
    for i in range(len(bounds) - 1):
        lo, hi = bounds[i], bounds[i + 1]
        piece = " ".join(words[lo:hi])
        # Znacznik trafia tylko na ostatnią planszę wstawki, żeby nie powtarzał się
        # przy każdym kawałku długiego wpisanego fragmentu.
        if i == len(bounds) - 2:
            piece = piece + "\n" + NO_SOURCE_MARKER
        units.append(Unit(
            key=insert_cut_key(anchor, i), kind="insert", text=piece,
            times=times[lo:hi], anchor=anchor, board=i,
            edited=bool(state.get("edited")),
        ))
    return units


def build_units(
    layout: Layout,
    tokens: Sequence[Token],
    word_times: Sequence[Tuple[float, float]],
    extras: Sequence[Dict[str, Any]] = (),
    segments: Sequence[Dict[str, Any]] = (),
) -> List[Unit]:
    """
    Cały rozdział jako uporządkowany ciąg plansz.

    Wstawka o kotwicy N wchodzi tuż przed planszę zaczynającą się słowem N; kotwica
    równa liczbie słów rozdziału stawia ją na końcu. Kolejność wynika z kotwic,
    więc brakujący akapit w środku rozdziału trafia na swoje miejsce sam.
    """
    if layout.token_count != len(word_times):
        raise LayoutError(
            f"Podział opisuje {layout.token_count} słów, a czasów jest {len(word_times)}."
        )

    by_anchor = _extras_by_anchor(extras)
    bounds = layout.bounds()
    units: List[Unit] = []

    for i in range(len(bounds) - 1):
        lo, hi = bounds[i], bounds[i + 1]
        for anchor in sorted(a for a in by_anchor if lo <= a < hi and a == lo):
            units.extend(_insert_units(layout, by_anchor[anchor], segments))

        original = render_text(tokens, lo, hi)
        fix = layout.corrections.get(correction_key(lo, hi))
        units.append(Unit(
            key=book_cut_key(lo), kind="book",
            text=fix if fix else original,
            times=list(word_times[lo:hi]),
            dialogue=hi > lo and all(tokens[k].is_dialogue for k in range(lo, hi)),
            token_start=lo, token_end=hi, corrected=bool(fix),
        ))

    for anchor in sorted(a for a in by_anchor if a >= layout.token_count):
        units.extend(_insert_units(layout, by_anchor[anchor], segments))

    return units


# ---------------------------------------------------------------------------
# Cięcia
# ---------------------------------------------------------------------------

@dataclass
class Cut:
    """Jeden punkt cięcia wraz z tym, co pozwala ocenić jego wiarygodność."""
    index: int
    key: str                # identyfikator granicy; END_CUT_KEY zamyka rozdział
    time: float
    source: str             # 'manual' | 'silence' | 'words' | 'edge'
    confidence: float = 0.0
    safety: float = 0.0
    silence_start: float = 0.0
    silence_end: float = 0.0
    word_end: float = 0.0
    word_start: float = 0.0
    no_pause: bool = False
    segment_text: str = ""
    clamped: bool = False

    @property
    def needs_attention(self) -> bool:
        """Granice, które warto obejrzeć w edytorze fali, zanim pójdą do eksportu."""
        return self.no_pause or self.source == "words" or self.safety < SAFE_MARGIN

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index, "key": self.key, "time": round(self.time, 3),
            "source": self.source, "confidence": round(self.confidence, 3),
            "safety": round(self.safety, 3),
            "silence_start": round(self.silence_start, 3),
            "silence_end": round(self.silence_end, 3),
            "word_end": round(self.word_end, 3), "word_start": round(self.word_start, 3),
            "no_pause": self.no_pause, "segment_text": self.segment_text,
            "clamped": self.clamped, "needs_attention": self.needs_attention,
        }


def _segment_at(segments: Sequence[Dict[str, Any]], t: float, margin: float = 0.05) -> Optional[str]:
    """Tekst wypowiedzi, w której środku leży chwila t. None, gdy t wypada w przerwie."""
    for seg in segments:
        if float(seg["start"]) + margin < t < float(seg["end"]) - margin:
            return str(seg.get("text", "")).strip()
    return None


def compute_cuts(
    layout: Layout,
    units: Sequence[Unit],
    analysis: AudioAnalysis,
    segments: Sequence[Dict[str, Any]] = (),
    search: float = DEFAULT_SEARCH_WINDOW,
    min_silence: float = DEFAULT_MIN_SILENCE,
    bias: float = 0.5,
    head_pad: float = DEFAULT_HEAD_PAD,
    tail_pad: float = DEFAULT_TAIL_PAD,
) -> List[Cut]:
    """
    Punkty cięcia między kolejnymi planszami, wraz z brzegami rozdziału.

    Zwraca len(units) + 1 pozycji: początek, każda granica, koniec.
    """
    if not units:
        return []

    env = analysis.envelope
    cuts: List[Cut] = []

    for i in range(len(units) + 1):
        first, last = i == 0, i == len(units)
        key = units[i].key if not last else END_CUT_KEY
        word_end = units[i - 1].times[-1][1] if i > 0 and units[i - 1].times else 0.0
        word_start = units[i].times[0][0] if not last and units[i].times else env.duration

        if key in layout.overrides:
            time = float(layout.overrides[key])
            cut = Cut(index=i, key=key, time=time, source="manual", confidence=1.0,
                      safety=cut_safety(env, time), word_end=word_end, word_start=word_start)
        elif first:
            time = snap_head(env, word_start, head_pad)
            cut = Cut(index=i, key=key, time=time, source="edge", confidence=1.0,
                      safety=cut_safety(env, time), word_start=word_start)
        elif last:
            time = snap_tail(env, word_end, tail_pad)
            cut = Cut(index=i, key=key, time=time, source="edge", confidence=1.0,
                      safety=cut_safety(env, time), word_end=word_end,
                      word_start=env.duration)
        else:
            snap = snap_boundary(env, word_end, word_start, analysis.silences,
                                 search=search, min_silence=min_silence, bias=bias, index=i)
            cut = Cut(
                index=i, key=key, time=snap.cut,
                source="silence" if snap.method == "silence" else "words",
                confidence=snap.confidence, safety=snap.safety_after,
                silence_start=snap.silence_start, silence_end=snap.silence_end,
                word_end=word_end, word_start=word_start,
            )
        cuts.append(cut)

    _enforce_order(cuts, env.duration)

    # Flagę liczymy na ostatecznej pozycji cięcia, nie na pozycji słowa: pytanie
    # brzmi „czy tak pocięte nagranie zabrzmi jak przerwanie w pół myśli". Whisper
    # trzyma w jednym segmencie całą wypowiedź wraz z drobnymi oddechami, więc
    # cięcie w takim oddechu nadal wypada w środku zdania.
    for cut in cuts[1:-1]:
        seg = _segment_at(segments, cut.time)
        if seg is not None:
            cut.no_pause = True
            cut.segment_text = seg

    return cuts


def _enforce_order(cuts: List[Cut], duration: float) -> None:
    """
    Pilnuje rosnącej kolejności cięć.

    Ręczne ustawienie sąsiada albo dosunięcie do odległej ciszy może wywrócić
    kolejność; plansza o ujemnym czasie trwania wysypałaby ffmpeg przy eksporcie.
    Korekta jest odnotowana w `clamped`, żeby interfejs mógł ją pokazać zamiast
    po cichu przesuwać cięcie ustawione ręcznie.
    """
    for i in range(1, len(cuts)):
        floor = cuts[i - 1].time + MIN_BOARD_SECONDS
        if cuts[i].time < floor:
            cuts[i].time = round(min(floor, duration), 3)
            cuts[i].clamped = True


def boards_from_units(
    units: Sequence[Unit],
    cuts: Sequence[Cut],
    max_chars_per_line: int = 45,
) -> List[Dict[str, Any]]:
    """Plansze w formacie przyjmowanym przez exporter."""
    boards: List[Dict[str, Any]] = []
    for i, unit in enumerate(units):
        start, end = cuts[i].time, cuts[i + 1].time
        boards.append({
            "chunk_id": i + 1,
            "text": unit.text,
            "chunk_type": ("intro_outro" if unit.kind == "insert"
                           else ("dialogue" if unit.dialogue else "narration")),
            "lines_count": estimate_line_count(unit.text, max_chars_per_line),
            "start_time": round(start, 3),
            "end_time": round(end, 3),
            "duration": round(end - start, 3),
            "cut_key": unit.key,
            "kind": unit.kind,
            "token_start": unit.token_start,
            "token_end": unit.token_end,
            "anchor": unit.anchor,
            "insert_board": unit.board,
            "edited": unit.edited,
            "corrected": unit.corrected,
        })
    return boards


def compute_chapter_timing(
    chapter: Chapter,
    payload: Dict[str, Any],
    analysis: AudioAnalysis,
    layout: Optional[Layout] = None,
    tokens: Optional[List[Token]] = None,
    max_lines: int = 11,
    max_chars_per_line: int = 45,
    **snap_kwargs: Any,
) -> Dict[str, Any]:
    """Pełny wynik dla jednego rozdziału: jednostki, cięcia, plansze i podsumowanie."""
    from .layout import propose_layout

    tokens = tokenize_chapter(chapter) if tokens is None else tokens
    layout = layout or propose_layout(chapter, tokens, max_lines, max_chars_per_line)
    word_times = word_times_from_payload(payload)
    extras = payload.get("extras") or []

    units = build_units(layout, tokens, word_times, extras,
                        payload.get('whisper_segments', []))
    cuts = compute_cuts(layout, units, analysis, payload.get("whisper_segments", []), **snap_kwargs)
    boards = boards_from_units(units, cuts, max_chars_per_line)

    return {
        "chapter_num": chapter.number,
        "header": chapter.header,
        "audio_file": payload.get("audio_file"),
        "duration": analysis.envelope.duration,
        "units": units,
        "cuts": cuts,
        "boards": boards,
        "chunks": boards,
        "summary": {
            "boards": len(boards),
            "cuts": len(cuts),
            "inserts": sum(1 for u in units if u.kind == "insert"),
            "inserts_edited": sum(1 for u in units if u.kind == "insert" and u.edited),
            "corrections": sum(1 for u in units if u.corrected),
            "attention": sum(1 for c in cuts if c.needs_attention),
            "no_pause": sum(1 for c in cuts if c.no_pause),
            "from_silence": sum(1 for c in cuts if c.source == "silence"),
            "from_words": sum(1 for c in cuts if c.source == "words"),
            "manual": sum(1 for c in cuts if c.source == "manual"),
            "clamped": sum(1 for c in cuts if c.clamped),
            "over_limit": sum(1 for b in boards if b["lines_count"] > max_lines),
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_table(result: Dict[str, Any]) -> None:
    print(f"  {'granica':>10} {'czas':>8} {'źródło':>8} {'pewn.':>6} {'zapas':>7}  uwagi")
    for c in result["cuts"]:
        marks = []
        if c.no_pause:
            marks.append("BRAK PAUZY")
        if c.clamped:
            marks.append("skorygowane")
        if c.source == "words":
            marks.append("bez ciszy")
        elif c.safety < SAFE_MARGIN and c.source != "edge":
            marks.append("mały zapas")
        print(f"  {c.key:>10} {c.time:8.2f} {c.source:>8} {c.confidence:6.2f} "
              f"{c.safety * 1000:6.0f}ms  {', '.join(marks)}")


def main(argv: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(description="Wyznaczanie timestampów z zatwierdzonego podziału.")
    parser.add_argument("--chapter", type=int, action="append")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--data-dir", default=os.path.join(base, "Data"))
    parser.add_argument("--bias", type=float, default=0.5)
    parser.add_argument("--search", type=float, default=DEFAULT_SEARCH_WINDOW)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--json", help="Zapisz wynik do pliku JSON")
    args = parser.parse_args(argv)

    chapters, settings = load_chapters(args.data_dir)
    if args.chapter:
        wanted = set(args.chapter)
        chapters = [c for c in chapters if c.number in wanted]
    elif not args.all:
        parser.error("Podaj --chapter N albo --all.")

    processed_dir = os.path.join(args.data_dir, "Processed_JSON")
    audio_dir = os.path.join(args.data_dir, "Audio")
    cache_dir = os.path.join(args.data_dir, "Cache_Audio_Analysis")
    store = LayoutStore(args.data_dir)

    totals = {k: 0 for k in ("boards", "cuts", "inserts", "inserts_edited", "corrections",
                             "attention", "no_pause", "from_silence", "from_words",
                             "manual", "clamped", "over_limit")}
    dump: List[Dict[str, Any]] = []

    for chapter in chapters:
        path = os.path.join(processed_dir, f"chapter_{chapter.number:03d}.json")
        if not os.path.exists(path):
            print(f"Rozdział {chapter.number}: brak wyniku przetwarzania, pomijam.")
            continue
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        audio_path = resolve_audio_path(payload, audio_dir)
        if not audio_path:
            print(f"Rozdział {chapter.number}: brak pliku audio, pomijam.")
            continue

        analysis = analyze_audio(audio_path, cache_dir=cache_dir)
        layout, tokens, _fresh = store.load_or_propose(
            chapter, max_lines=settings["max_lines_per_board"],
            max_chars_per_line=settings["max_chars_per_line"])

        result = compute_chapter_timing(
            chapter, payload, analysis, layout, tokens,
            settings["max_lines_per_board"], settings["max_chars_per_line"],
            bias=args.bias, search=args.search,
        )
        s = result["summary"]
        for k in totals:
            totals[k] += s[k]

        if not args.quiet:
            print(f"\nRozdział {chapter.number}: {chapter.header}  "
                  f"({s['boards']} plansz, {s['inserts']} wstawek, {s['attention']} do obejrzenia)")
            _print_table(result)

        dump.append({
            "chapter_num": chapter.number, "header": chapter.header,
            "audio_file": result["audio_file"], "duration": result["duration"],
            "summary": s,
            "cuts": [c.to_dict() for c in result["cuts"]],
            "boards": result["boards"],
        })

    print("\n" + "=" * 74)
    print(f"TIMESTAMPY — {len(dump)} rozdz., {totals['boards']} plansz, {totals['cuts']} cięć")
    print(f"  wstawek (brak w .txt):     {totals['inserts']}  "
          f"(wpisanych ręcznie: {totals['inserts_edited']})")
    print(f"  poprawionych tekstów:      {totals['corrections']}")
    print(f"  z ciszy w nagraniu:        {totals['from_silence']}")
    print(f"  ze środka luki (bez ciszy):{totals['from_words']:>4}")
    print(f"  ustawionych ręcznie:       {totals['manual']}")
    print(f"  skorygowanych kolejnością: {totals['clamped']}")
    print(f"  DO OBEJRZENIA:             {totals['attention']}  "
          f"(w tym {totals['no_pause']} bez przerwy lektora)")
    print("=" * 74)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(dump, f, ensure_ascii=False, indent=1)
        print(f"Zapisano: {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
