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
from Engine.layout import (
    Layout, LayoutError, Token, book_cut_key, insert_cut_key,
    set_correction, set_insert_text, split_insert,
)
from Engine.timing import (
    MIN_BOARD_SECONDS, NO_SOURCE_MARKER, END_CUT_KEY, Cut,
    build_units, compute_cuts, boards_from_units, heard_text, extra_board_text,
    align_free_text,
)


def units_and_cuts(layout, words, analysis, extras=(), segments=()):
    """Skrót dla testów: jednostki rozdziału i policzone dla nich cięcia."""
    units = build_units(layout, make_tokens(len(words)), words, extras)
    return units, compute_cuts(layout, units, analysis, segments)

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
    units, cuts = units_and_cuts(layout, words, analysis)
    srodkowe = cuts[1]
    assert srodkowe.source == "silence"
    assert 3.0 < srodkowe.time < 3.5
    assert abs(srodkowe.time - 3.25) < 0.1


def test_reczne_ustawienie_ma_pierwszenstwo(layout, words, analysis):
    layout.overrides[book_cut_key(3)] = 3.11
    units, cuts = units_and_cuts(layout, words, analysis)
    assert cuts[1].source == "manual"
    assert cuts[1].time == 3.11


def test_brak_ciszy_daje_zrodlo_words(analysis):
    """Granica w środku ciągłej mowy — nie ma czego dosunąć."""
    ciagla = make_analysis([(1.0, 9.0)])
    layout = Layout(chapter_num=1, text_hash="x", token_count=9, breaks=[4])
    w = evenly(9, [(1.0, 9.0)])
    units, cuts = units_and_cuts(layout, w, ciagla)
    assert cuts[1].source == "words"
    assert cuts[1].confidence == 0.0
    assert cuts[1].needs_attention


def test_brzegi_obejmuja_cisze_wokol_mowy(layout, words, analysis):
    units, cuts = units_and_cuts(layout, words, analysis)
    assert cuts[0].source == "edge"
    assert cuts[0].time < words[0][0]
    assert cuts[-1].source == "edge"
    assert cuts[-1].time > words[-1][1]


# ---------------------------------------------------------------------------
# Kolejność i spójność
# ---------------------------------------------------------------------------

def test_czasy_ciec_rosna(layout, words, analysis):
    units, cuts = units_and_cuts(layout, words, analysis)
    assert [c.time for c in cuts] == sorted(c.time for c in cuts)


def test_reczne_ustawienie_lamiace_kolejnosc_jest_korygowane(layout, words, analysis):
    layout.overrides[book_cut_key(3)] = 3.30
    layout.overrides[book_cut_key(6)] = 1.00          # przed poprzednim cięciem
    units, cuts = units_and_cuts(layout, words, analysis)
    assert cuts[2].clamped
    assert cuts[2].time >= cuts[1].time + MIN_BOARD_SECONDS


def test_korekta_kolejnosci_jest_odnotowana_a_nie_cicha(layout, words, analysis):
    layout.overrides[book_cut_key(6)] = 0.5
    units, cuts = units_and_cuts(layout, words, analysis)
    assert any(c.clamped for c in cuts), "korekta musi być widoczna w danych"


def test_niezgodna_liczba_czasow_jest_zglaszana(layout, analysis):
    with pytest.raises(LayoutError, match="czasów jest"):
        build_units(layout, make_tokens(5), evenly(5, SPANS))


# ---------------------------------------------------------------------------
# Plansze
# ---------------------------------------------------------------------------

def test_plansze_przylegaja_do_siebie(layout, words, analysis):
    units, cuts = units_and_cuts(layout, words, analysis)
    boards = boards_from_units(units, cuts)
    assert len(boards) == 3
    for a, b in zip(boards, boards[1:]):
        assert a["end_time"] == b["start_time"], "między planszami nie może ginąć nagranie"


def test_plansze_pokrywaja_caly_tekst(layout, words, analysis):
    units, cuts = units_and_cuts(layout, words, analysis)
    boards = boards_from_units(units, cuts)
    assert boards[0]["token_start"] == 0
    assert boards[-1]["token_end"] == 9
    assert sum(len(b["text"].split()) for b in boards) == 9


def test_kazda_plansza_ma_dodatni_czas(layout, words, analysis):
    layout.overrides[book_cut_key(6)] = 0.1
    units, cuts = units_and_cuts(layout, words, analysis)
    boards = boards_from_units(units, cuts)
    assert all(b["duration"] > 0 for b in boards)


# ---------------------------------------------------------------------------
# Brak pauzy u lektora
# ---------------------------------------------------------------------------

def test_flaga_braku_pauzy_gdy_ciecie_wypada_w_srodku_wypowiedzi(layout, words, analysis):
    segments = [{"start": 1.0, "end": 6.0, "text": "Jedna długa wypowiedź bez przerwy."}]
    units, cuts = units_and_cuts(layout, words, analysis, (), segments)
    assert cuts[1].no_pause
    assert cuts[1].segment_text == "Jedna długa wypowiedź bez przerwy."
    assert cuts[1].needs_attention


def test_brak_flagi_gdy_ciecie_trafia_w_przerwe_miedzy_wypowiedziami(layout, words, analysis):
    segments = [{"start": 1.0, "end": 3.0, "text": "Pierwsza."},
                {"start": 3.5, "end": 6.0, "text": "Druga."}]
    units, cuts = units_and_cuts(layout, words, analysis, (), segments)
    assert not cuts[1].no_pause


def test_brzegi_nie_dostaja_flagi_braku_pauzy(layout, words, analysis):
    segments = [{"start": 0.0, "end": 20.0, "text": "Wszystko jedną wypowiedzią."}]
    units, cuts = units_and_cuts(layout, words, analysis, (), segments)
    assert not cuts[0].no_pause
    assert not cuts[-1].no_pause
