"""
Testy wyznaczania timestampów (Engine/timing.py).

Obwiednia nagrania jest budowana syntetycznie — testy nie potrzebują ani plików MP3,
ani ffmpeg, ani Whispera, więc chodzą w ułamku sekundy i nie zależą od zawartości Data/.

    python -m pytest Tests/test_timing.py -q
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Engine.audio_analysis import AudioAnalysis, Envelope, find_silences
from Engine.layout import Layout, LayoutError, Token
from Engine.timing import (
    MIN_BOARD_SECONDS, NO_SOURCE_MARKER, Cut,
    compute_cuts, boards_from_cuts, assemble_chapter, heard_text, extra_board_text,
)

HOP = 0.01
LOUD = 0.30
QUIET = 0.001


def make_analysis(spans, duration=20.0):
    """
    Buduje obwiednię z listy przedziałów mowy [(start, koniec), ...].
    Wszystko poza nimi jest ciszą.
    """
    frames = int(duration / HOP)
    rms = np.full(frames, QUIET, dtype=np.float32)
    for start, end in spans:
        rms[int(start / HOP):int(end / HOP)] = LOUD

    env = Envelope(rms=rms, hop_s=HOP, duration=duration,
                   noise_floor=QUIET, speech_level=LOUD, threshold=LOUD * 0.2)
    return AudioAnalysis(audio_path="test.mp3", envelope=env,
                         silences=find_silences(env, 0.10),
                         peaks_min=np.zeros(0, dtype=np.int8),
                         peaks_max=np.zeros(0, dtype=np.int8),
                         buckets_per_sec=400)


def make_tokens(n):
    return [Token(word=f"slowo{i}", sep=" ", block=0, is_dialogue=False, start=0, end=0)
            for i in range(n)]


def evenly(n, spans):
    """Rozkłada n słów równomiernie wewnątrz przedziałów mowy."""
    times = []
    per = n // len(spans)
    for idx, (start, end) in enumerate(spans):
        count = per if idx < len(spans) - 1 else n - per * (len(spans) - 1)
        step = (end - start) / count
        for k in range(count):
            times.append((round(start + k * step, 3), round(start + (k + 1) * step - 0.02, 3)))
    return times


# Mowa 1–3 s, przerwa 0,5 s, mowa 3,5–6 s, przerwa 0,6 s, mowa 6,6–9 s.
SPANS = [(1.0, 3.0), (3.5, 6.0), (6.6, 9.0)]


@pytest.fixture
def analysis():
    return make_analysis(SPANS)


@pytest.fixture
def words():
    return evenly(9, SPANS)


@pytest.fixture
def layout():
    return Layout(chapter_num=1, text_hash="x", token_count=9, breaks=[3, 6])


# ---------------------------------------------------------------------------
# Skąd bierze się czas cięcia
# ---------------------------------------------------------------------------

def test_ciecie_laduje_w_srodku_ciszy(layout, words, analysis):
    cuts = compute_cuts(layout, words, analysis)
    srodkowe = cuts[1]
    assert srodkowe.source == "silence"
    assert 3.0 < srodkowe.time < 3.5
    assert abs(srodkowe.time - 3.25) < 0.1


def test_reczne_ustawienie_ma_pierwszenstwo(layout, words, analysis):
    layout.overrides[3] = 3.11
    cuts = compute_cuts(layout, words, analysis)
    assert cuts[1].source == "manual"
    assert cuts[1].time == 3.11


def test_brak_ciszy_daje_zrodlo_words(analysis):
    """Granica w środku ciągłej mowy — nie ma czego dosunąć."""
    ciagla = make_analysis([(1.0, 9.0)])
    layout = Layout(chapter_num=1, text_hash="x", token_count=9, breaks=[4])
    cuts = compute_cuts(layout, evenly(9, [(1.0, 9.0)]), ciagla)
    assert cuts[1].source == "words"
    assert cuts[1].confidence == 0.0
    assert cuts[1].needs_attention


def test_brzegi_obejmuja_cisze_wokol_mowy(layout, words, analysis):
    cuts = compute_cuts(layout, words, analysis)
    assert cuts[0].source == "edge"
    assert cuts[0].time < words[0][0]
    assert cuts[-1].source == "edge"
    assert cuts[-1].time > words[-1][1]


# ---------------------------------------------------------------------------
# Kolejność i spójność
# ---------------------------------------------------------------------------

def test_czasy_ciec_rosna(layout, words, analysis):
    cuts = compute_cuts(layout, words, analysis)
    assert [c.time for c in cuts] == sorted(c.time for c in cuts)


def test_reczne_ustawienie_lamiace_kolejnosc_jest_korygowane(layout, words, analysis):
    layout.overrides[3] = 3.30
    layout.overrides[6] = 1.00          # przed poprzednim cięciem
    cuts = compute_cuts(layout, words, analysis)
    assert cuts[2].clamped
    assert cuts[2].time >= cuts[1].time + MIN_BOARD_SECONDS


def test_korekta_kolejnosci_jest_odnotowana_a_nie_cicha(layout, words, analysis):
    layout.overrides[6] = 0.5
    cuts = compute_cuts(layout, words, analysis)
    assert any(c.clamped for c in cuts), "korekta musi być widoczna w danych"


def test_niezgodna_liczba_czasow_jest_zglaszana(layout, analysis):
    with pytest.raises(LayoutError, match="czasów jest"):
        compute_cuts(layout, evenly(5, SPANS), analysis)


# ---------------------------------------------------------------------------
# Plansze
# ---------------------------------------------------------------------------

def test_plansze_przylegaja_do_siebie(layout, words, analysis):
    cuts = compute_cuts(layout, words, analysis)
    boards = boards_from_cuts(layout, make_tokens(9), cuts)
    assert len(boards) == 3
    for a, b in zip(boards, boards[1:]):
        assert a["end_time"] == b["start_time"], "między planszami nie może ginąć nagranie"


def test_plansze_pokrywaja_caly_tekst(layout, words, analysis):
    cuts = compute_cuts(layout, words, analysis)
    boards = boards_from_cuts(layout, make_tokens(9), cuts)
    assert boards[0]["token_start"] == 0
    assert boards[-1]["token_end"] == 9
    assert sum(len(b["text"].split()) for b in boards) == 9


def test_kazda_plansza_ma_dodatni_czas(layout, words, analysis):
    layout.overrides[6] = 0.1
    cuts = compute_cuts(layout, words, analysis)
    boards = boards_from_cuts(layout, make_tokens(9), cuts)
    assert all(b["duration"] > 0 for b in boards)


# ---------------------------------------------------------------------------
# Brak pauzy u lektora
# ---------------------------------------------------------------------------

def test_flaga_braku_pauzy_gdy_ciecie_wypada_w_srodku_wypowiedzi(layout, words, analysis):
    segments = [{"start": 1.0, "end": 6.0, "text": "Jedna długa wypowiedź bez przerwy."}]
    cuts = compute_cuts(layout, words, analysis, segments)
    assert cuts[1].no_pause
    assert cuts[1].segment_text == "Jedna długa wypowiedź bez przerwy."
    assert cuts[1].needs_attention


def test_brak_flagi_gdy_ciecie_trafia_w_przerwe_miedzy_wypowiedziami(layout, words, analysis):
    segments = [{"start": 1.0, "end": 3.0, "text": "Pierwsza."},
                {"start": 3.5, "end": 6.0, "text": "Druga."}]
    cuts = compute_cuts(layout, words, analysis, segments)
    assert not cuts[1].no_pause


def test_brzegi_nie_dostaja_flagi_braku_pauzy(layout, words, analysis):
    segments = [{"start": 0.0, "end": 20.0, "text": "Wszystko jedną wypowiedzią."}]
    cuts = compute_cuts(layout, words, analysis, segments)
    assert not cuts[0].no_pause
    assert not cuts[-1].no_pause


# ---------------------------------------------------------------------------
# Wtrącenia lektora
# ---------------------------------------------------------------------------

def test_intro_dokladane_przed_pierwsza_plansza(layout, words, analysis):
    cuts = compute_cuts(layout, words, analysis)
    boards = boards_from_cuts(layout, make_tokens(9), cuts)
    payload = {"extras": [{"position": "intro", "start_time": 0.0, "end_time": 0.9}]}
    full = assemble_chapter(payload, boards, cuts)
    assert full[0]["chunk_type"] == "intro_outro"
    assert len(full) == len(boards) + 1
    assert [b["chunk_id"] for b in full] == list(range(1, len(full) + 1))


def test_intro_nie_nachodzi_na_pierwsza_plansze(layout, words, analysis):
    cuts = compute_cuts(layout, words, analysis)
    boards = boards_from_cuts(layout, make_tokens(9), cuts)
    payload = {"extras": [{"position": "intro", "start_time": 0.0, "end_time": 5.0}]}
    full = assemble_chapter(payload, boards, cuts)
    assert full[0]["end_time"] <= full[1]["start_time"]


def test_zbyt_krotkie_wtracenie_jest_pomijane(layout, words, analysis):
    cuts = compute_cuts(layout, words, analysis)
    boards = boards_from_cuts(layout, make_tokens(9), cuts)
    payload = {"extras": [{"position": "intro", "start_time": 0.0, "end_time": 0.05}]}
    assert len(assemble_chapter(payload, boards, cuts)) == len(boards)


def test_bez_wtracen_plansze_zostaja_bez_zmian(layout, words, analysis):
    cuts = compute_cuts(layout, words, analysis)
    boards = boards_from_cuts(layout, make_tokens(9), cuts)
    assert assemble_chapter({}, boards, cuts) == boards


# ---------------------------------------------------------------------------
# Format wyjściowy
# ---------------------------------------------------------------------------

def test_ciecie_serializuje_sie_do_slownika(layout, words, analysis):
    d = compute_cuts(layout, words, analysis)[1].to_dict()
    assert set(d) >= {"index", "token", "time", "source", "confidence",
                      "safety", "no_pause", "needs_attention"}
    assert isinstance(d["no_pause"], bool)


def test_plansza_ma_klucze_wymagane_przez_exporter(layout, words, analysis):
    cuts = compute_cuts(layout, words, analysis)
    board = boards_from_cuts(layout, make_tokens(9), cuts)[0]
    assert set(board) >= {"chunk_id", "text", "chunk_type", "lines_count",
                          "start_time", "end_time", "duration"}


# ---------------------------------------------------------------------------
# Tekst wtrąceń lektora
# ---------------------------------------------------------------------------

SEGMENTS = [
    {"start": 0.2, "end": 0.9, "text": " Rozdział pierwszy. "},
    {"start": 1.0, "end": 3.0, "text": " Dawno, dawno temu. "},
]


def test_wtracenie_dostaje_tekst_uslyszany_przez_whispera(layout, words, analysis):
    cuts = compute_cuts(layout, words, analysis)
    boards = boards_from_cuts(layout, make_tokens(9), cuts)
    payload = {"extras": [{"position": "intro", "start_time": 0.0, "end_time": 0.95}],
               "whisper_segments": SEGMENTS}
    intro = assemble_chapter(payload, boards, cuts)[0]
    assert intro["text"].startswith("Rozdział pierwszy.")
    assert intro["text"].endswith(NO_SOURCE_MARKER)


def test_wtracenie_bez_mowy_zostaje_samym_znacznikiem(layout, words, analysis):
    cuts = compute_cuts(layout, words, analysis)
    boards = boards_from_cuts(layout, make_tokens(9), cuts)
    payload = {"extras": [{"position": "intro", "start_time": 0.0, "end_time": 0.95}],
               "whisper_segments": []}
    assert assemble_chapter(payload, boards, cuts)[0]["text"] == NO_SOURCE_MARKER


def test_wtracenie_nie_zbiera_mowy_spoza_swojego_zakresu():
    assert heard_text(SEGMENTS, 0.0, 0.95) == "Rozdział pierwszy."
    assert heard_text(SEGMENTS, 1.0, 3.0) == "Dawno, dawno temu."
    assert heard_text(SEGMENTS, 5.0, 6.0) == ""


def test_znacznik_jest_w_osobnej_linii():
    assert extra_board_text("Coś słychać").split("\n") == ["Coś słychać", NO_SOURCE_MARKER]


def test_eksport_bierze_nazwe_pliku_z_uslyszanego_tekstu():
    from Engine.exporter import sanitize_filename, generate_audacity_labels
    text = extra_board_text("Rozdział pierwszy. Puddleby")
    assert sanitize_filename(text) == "Rozdział pierwszy. Puddleby"
    label = generate_audacity_labels(
        [{"start_time": 0, "end_time": 1, "text": text, "chunk_type": "intro_outro", "chunk_id": 1}])
    assert "ROZDZIAŁ PIERWSZY" in label
    assert "BRAK TEKSTU" not in label
