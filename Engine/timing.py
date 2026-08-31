"""
Wyznaczanie timestampów dla zatwierdzonego podziału na plansze.

Tu spinają się trzy wcześniejsze warstwy:

    layout.py          — gdzie w tekście stoją granice (decyzja użytkownika)
    chapter_*.json     — czasy pojedynczych słów z alignmentu
    audio_analysis.py  — gdzie w nagraniu jest cisza

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
import sys
import json
import glob
import argparse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .text_parser import Chapter, estimate_line_count
from .layout import (
    Layout, LayoutError, LayoutStore, Token,
    tokenize_chapter, render_text, word_times_from_payload, load_chapters,
)
from .audio_analysis import (
    AudioAnalysis, Envelope, Silence, analyze_audio, resolve_audio_path,
    snap_boundary, snap_head, snap_tail, cut_safety,
    DEFAULT_SEARCH_WINDOW, DEFAULT_MIN_SILENCE, DEFAULT_HEAD_PAD, DEFAULT_TAIL_PAD,
    SAFE_MARGIN,
)

# Wtrącenie lektora dostaje tekst, który faktycznie słychać w nagraniu, plus znacznik
# mówiący, że w pliku .txt tego fragmentu nie ma. Sam znacznik zamiast treści zmuszał
# do odsłuchiwania każdej takiej planszy, żeby w ogóle wiedzieć, co się w niej dzieje —
# a nazwa wyeksportowanego MP3 brzmiała „001 - (brak tekstu…)”.
NO_SOURCE_MARKER = "(brak tekstu w pliku txt)"
MIN_BOARD_SECONDS = 0.20


@dataclass
class Cut:
    """Jeden punkt cięcia wraz z tym, co pozwala ocenić jego wiarygodność."""
    index: int              # 0 = początek rozdziału, ostatni = koniec
    token: int              # indeks tokenu, na którym stoi granica
    time: float
    source: str             # 'manual' | 'silence' | 'words' | 'edge'
    confidence: float = 0.0
    safety: float = 0.0     # ile ciszy zostaje po obu stronach cięcia
    silence_start: float = 0.0
    silence_end: float = 0.0
    word_end: float = 0.0   # koniec ostatniego słowa przed granicą
    word_start: float = 0.0 # początek pierwszego słowa po granicy
    no_pause: bool = False  # lektor czyta przez tę granicę bez przerwy
    segment_text: str = ""  # wypowiedź Whispera, przez którą przechodzi granica
    clamped: bool = False   # czas trzeba było skorygować, by zachować kolejność

    @property
    def needs_attention(self) -> bool:
        """Granice, które warto obejrzeć w edytorze fali, zanim pójdą do eksportu."""
        return self.no_pause or self.source == "words" or self.safety < SAFE_MARGIN

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index, "token": self.token, "time": round(self.time, 3),
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
    word_times: Sequence[Tuple[float, float]],
    analysis: AudioAnalysis,
    segments: Sequence[Dict[str, Any]] = (),
    search: float = DEFAULT_SEARCH_WINDOW,
    min_silence: float = DEFAULT_MIN_SILENCE,
    bias: float = 0.5,
    head_pad: float = DEFAULT_HEAD_PAD,
    tail_pad: float = DEFAULT_TAIL_PAD,
) -> List[Cut]:
    """
    Wyznacza punkty cięcia dla wszystkich granic podziału, wraz z brzegami rozdziału.

    Zwraca len(breaks) + 2 pozycji: początek rozdziału, każda granica, koniec rozdziału.
    """
    if layout.token_count != len(word_times):
        raise LayoutError(
            f"Podział opisuje {layout.token_count} słów, a czasów jest {len(word_times)}."
        )
    if not word_times:
        return []

    env = analysis.envelope
    bounds = layout.bounds()
    cuts: List[Cut] = []

    for i, token in enumerate(bounds):
        first = i == 0
        last = i == len(bounds) - 1
        word_end = word_times[token - 1][1] if token > 0 else 0.0
        word_start = word_times[token][0] if token < len(word_times) else env.duration

        if token in layout.overrides:
            time = float(layout.overrides[token])
            cut = Cut(index=i, token=token, time=time, source="manual", confidence=1.0,
                      safety=cut_safety(env, time), word_end=word_end, word_start=word_start)
        elif first:
            time = snap_head(env, word_start, head_pad)
            cut = Cut(index=i, token=token, time=time, source="edge",
                      confidence=1.0, safety=cut_safety(env, time),
                      word_end=0.0, word_start=word_start)
        elif last:
            time = snap_tail(env, word_end, tail_pad)
            cut = Cut(index=i, token=token, time=time, source="edge",
                      confidence=1.0, safety=cut_safety(env, time),
                      word_end=word_end, word_start=env.duration)
        else:
            snap = snap_boundary(env, word_end, word_start, analysis.silences,
                                 search=search, min_silence=min_silence, bias=bias, index=i)
            cut = Cut(
                index=i, token=token, time=snap.cut,
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


def boards_from_cuts(
    layout: Layout,
    tokens: Sequence[Token],
    cuts: Sequence[Cut],
    max_chars_per_line: int = 45,
) -> List[Dict[str, Any]]:
    """Plansze z tekstem i czasami — format przyjmowany przez exporter."""
    bounds = layout.bounds()
    boards: List[Dict[str, Any]] = []

    for i in range(len(bounds) - 1):
        lo, hi = bounds[i], bounds[i + 1]
        text = render_text(tokens, lo, hi)
        dialogue = hi > lo and all(tokens[k].is_dialogue for k in range(lo, hi))
        start, end = cuts[i].time, cuts[i + 1].time
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
        })
    return boards


def assemble_chapter(
    payload: Dict[str, Any],
    boards: List[Dict[str, Any]],
    cuts: Sequence[Cut],
) -> List[Dict[str, Any]]:
    """
    Dokłada wtrącenia lektora przed pierwszą i po ostatniej planszy.

    Bez tego wstęp czytany przez lektora zniknąłby z paczki — nie ma go w tekście
    książki, więc nie ma go też w podziale.
    """
    extras = payload.get("extras") or []
    segments = payload.get("whisper_segments") or []
    intro = next((e for e in extras if e.get("position") == "intro"), None)
    outro = next((e for e in extras if e.get("position") == "outro"), None)
    out: List[Dict[str, Any]] = []

    if intro and cuts:
        start, end = float(intro["start_time"]), min(float(intro["end_time"]), cuts[0].time)
        if end - start >= MIN_BOARD_SECONDS:
            out.append(_placeholder_board(start, end, heard_text(segments, start, end)))

    out.extend(boards)

    if outro and cuts:
        start, end = max(float(outro["start_time"]), cuts[-1].time), float(outro["end_time"])
        if end - start >= MIN_BOARD_SECONDS:
            out.append(_placeholder_board(start, end, heard_text(segments, start, end)))

    for idx, board in enumerate(out, 1):
        board["chunk_id"] = idx
    return out


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


def _placeholder_board(start: float, end: float, heard: str = "") -> Dict[str, Any]:
    text = extra_board_text(heard)
    return {
        "chunk_id": 0,
        "text": text,
        "chunk_type": "intro_outro",
        "lines_count": estimate_line_count(text, 45),
        "start_time": round(start, 3),
        "end_time": round(end, 3),
        "duration": round(end - start, 3),
    }


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
    """Pełny wynik dla jednego rozdziału: cięcia, plansze i podsumowanie jakości."""
    from .layout import propose_layout

    tokens = tokenize_chapter(chapter) if tokens is None else tokens
    layout = layout or propose_layout(chapter, tokens, max_lines, max_chars_per_line)
    word_times = word_times_from_payload(payload)

    cuts = compute_cuts(layout, word_times, analysis,
                        payload.get("whisper_segments", []), **snap_kwargs)
    boards = boards_from_cuts(layout, tokens, cuts, max_chars_per_line)
    full = assemble_chapter(payload, boards, cuts)

    interior = [c for c in cuts if c.source not in ("edge",)] or cuts
    return {
        "chapter_num": chapter.number,
        "header": chapter.header,
        "audio_file": payload.get("audio_file"),
        "duration": analysis.envelope.duration,
        "cuts": cuts,
        "boards": boards,
        "chunks": full,
        "summary": {
            "boards": len(boards),
            "cuts": len(cuts),
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
    print(f"  {'granica':>8} {'czas':>8} {'źródło':>8} {'pewn.':>6} {'zapas':>7} "
          f"{'słowo→':>8} {'→słowo':>8}  uwagi")
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
        print(f"  {c.index:>8} {c.time:8.2f} {c.source:>8} {c.confidence:6.2f} "
              f"{c.safety * 1000:6.0f}ms {c.word_end:8.2f} {c.word_start:8.2f}  "
              f"{', '.join(marks)}")


def main(argv: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(description="Wyznaczanie timestampów z zatwierdzonego podziału (etap 5).")
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

    totals = {"boards": 0, "cuts": 0, "attention": 0, "no_pause": 0,
              "from_silence": 0, "from_words": 0, "manual": 0, "clamped": 0, "over_limit": 0}
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
                  f"({s['boards']} plansz, {s['cuts']} cięć, {s['attention']} do obejrzenia)")
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
