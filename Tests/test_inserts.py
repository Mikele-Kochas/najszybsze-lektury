"""
Testy wstawek i korekt tekstu.

Wstawka to fragment słyszalny w nagraniu, którego nie ma w pliku .txt — wstęp lektora,
stopka albo akapit brakujący w źródle. Ma własny tekst, własny podział na plansze
i własne cięcia, więc długi wpisany fragment da się pociąć jak zwykły tekst książki.

Korekta zmienia wyłącznie tekst planszy książkowej; czasy i podział zostają.

    python -m pytest Tests/test_inserts.py -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Engine.layout import (
    Layout, LayoutError, book_cut_key, insert_cut_key, stale_corrections,
    set_correction, set_insert_text, split_insert, merge_insert, move_insert, insert_state,
)
from Engine.timing import (
    NO_SOURCE_MARKER, END_CUT_KEY,
    build_units, compute_cuts, boards_from_units, align_free_text,
)

from test_timing import make_analysis, make_tokens, evenly, SPANS


@pytest.fixture
def analysis():
    return make_analysis(SPANS)


@pytest.fixture
def words():
    return evenly(9, SPANS)


@pytest.fixture
def layout():
    return Layout(chapter_num=1, text_hash="x", token_count=9, breaks=[3, 6])


def extra(anchor, start, end, heard):
    """Wstawka w takiej postaci, w jakiej produkuje ją aligner."""
    said = heard.split()
    step = (end - start) / max(1, len(said))
    return {
        "position": "intro" if anchor == 0 else "inline",
        "anchor": anchor, "start_time": start, "end_time": end,
        "word_count": len(said), "heard": heard,
        "text": heard + "\n" + NO_SOURCE_MARKER, "edited": False,
        "words": [
            {"w": w, "s": round(start + i * step, 3), "e": round(start + (i + 1) * step, 3)}
            for i, w in enumerate(said)
        ],
    }


# ---------------------------------------------------------------------------
# Miejsce wstawki w rozdziale
# ---------------------------------------------------------------------------

def test_wstawka_na_poczatku_wyprzedza_pierwsza_plansze(layout, words):
    units = build_units(layout, make_tokens(9), words, [extra(0, 0.1, 0.9, "Rozdział pierwszy")])
    assert [u.kind for u in units[:2]] == ["insert", "book"]
    assert NO_SOURCE_MARKER in units[0].text


def test_wstawka_w_srodku_trafia_na_swoje_miejsce(layout, words):
    """Kotwica 3 znaczy: przed planszą zaczynającą się czwartym słowem rozdziału."""
    units = build_units(layout, make_tokens(9), words, [extra(3, 3.05, 3.45, "Brakujący akapit")])
    assert [u.kind for u in units] == ["book", "insert", "book", "book"]
    assert units[1].anchor == 3
    assert units[2].token_start == 3


def test_wstawka_na_koncu_zamyka_rozdzial(layout, words):
    units = build_units(layout, make_tokens(9), words, [extra(9, 9.2, 9.8, "Koniec nagrania")])
    assert units[-1].kind == "insert"


def test_bez_wstawek_zostaja_same_plansze_ksiazki(layout, words):
    units = build_units(layout, make_tokens(9), words, [])
    assert all(u.kind == "book" for u in units)
    assert len(units) == 3


# ---------------------------------------------------------------------------
# Czasy i cięcia wstawek
# ---------------------------------------------------------------------------

def test_wstawka_dostaje_wlasne_ciecia(layout, words, analysis):
    units = build_units(layout, make_tokens(9), words, [extra(3, 3.05, 3.45, "Brakujący akapit")])
    cuts = compute_cuts(layout, units, analysis)
    assert len(cuts) == len(units) + 1

    boards = boards_from_units(units, cuts)
    for a, b in zip(boards, boards[1:]):
        assert a["end_time"] == b["start_time"], "między planszami nie może ginąć nagranie"
    assert all(b["duration"] > 0 for b in boards)


def test_czasy_slow_wstawki_rosna(layout, words):
    units = build_units(layout, make_tokens(9), words, [extra(0, 0.1, 0.9, "raz dwa trzy")])
    for u in units:
        assert [t[0] for t in u.times] == sorted(t[0] for t in u.times)


def test_ostatnie_ciecie_ma_klucz_konca(layout, words, analysis):
    units = build_units(layout, make_tokens(9), words, [])
    assert compute_cuts(layout, units, analysis)[-1].key == END_CUT_KEY


# ---------------------------------------------------------------------------
# Wpisywanie i cięcie wstawki — sedno zgłoszonego braku
# ---------------------------------------------------------------------------

def test_wpisany_tekst_zastepuje_ten_z_whispera(layout, words):
    set_insert_text(layout, 0, "Rozdział pierwszy. Puddleby.")
    units = build_units(layout, make_tokens(9), words, [extra(0, 0.1, 0.9, "rozdzial pierwszy")])
    assert units[0].text.startswith("Rozdział pierwszy. Puddleby.")
    assert units[0].edited


def test_dlugi_wpisany_fragment_da_sie_pociac_na_plansze(layout, words, analysis):
    e = extra(0, 0.1, 0.95, "raz dwa trzy cztery")
    set_insert_text(layout, 0, "Raz dwa trzy cztery pięć sześć siedem osiem")
    split_insert(layout, 0, 0, 4, word_count=8)

    units = build_units(layout, make_tokens(9), words, [e])
    inserts = [u for u in units if u.kind == "insert"]
    assert len(inserts) == 2
    assert inserts[0].text == "Raz dwa trzy cztery"
    assert inserts[1].text.startswith("pięć sześć siedem osiem")

    cuts = compute_cuts(layout, units, analysis)
    boards = boards_from_units(units, cuts)
    assert boards[0]["end_time"] == boards[1]["start_time"]
    assert all(b["duration"] > 0 for b in boards)


def test_znacznik_tylko_na_ostatniej_planszy_wstawki(layout, words):
    """Inaczej przy długim fragmencie powtarzałby się na każdym kawałku."""
    e = extra(0, 0.1, 0.95, "raz dwa trzy cztery")
    set_insert_text(layout, 0, "Raz dwa trzy cztery pięć sześć")
    split_insert(layout, 0, 0, 3, word_count=6)
    inserts = [u for u in build_units(layout, make_tokens(9), words, [e]) if u.kind == "insert"]
    assert NO_SOURCE_MARKER not in inserts[0].text
    assert NO_SOURCE_MARKER in inserts[-1].text


def test_scalenie_plansz_wstawki_odwraca_podzial(layout):
    set_insert_text(layout, 0, "raz dwa trzy cztery")
    split_insert(layout, 0, 0, 2, word_count=4)
    merge_insert(layout, 0, 0)
    assert insert_state(layout, 0)["breaks"] == []


def test_podzial_wstawki_poza_zakresem_jest_odrzucany(layout):
    set_insert_text(layout, 0, "raz dwa trzy")
    with pytest.raises(LayoutError, match="wewnątrz planszy wstawki"):
        split_insert(layout, 0, 0, 9, word_count=3)


def test_zmiana_tekstu_kasuje_podzial_wstawki(layout):
    set_insert_text(layout, 0, "raz dwa trzy cztery")
    split_insert(layout, 0, 0, 2, word_count=4)
    assert insert_state(layout, 0)["breaks"] == [2]
    set_insert_text(layout, 0, "zupełnie inny tekst")
    assert insert_state(layout, 0)["breaks"] == []


def test_zmiana_tekstu_kasuje_reczne_czasy_wstawki(layout):
    layout.overrides[insert_cut_key(0, 1)] = 5.0
    layout.overrides[book_cut_key(3)] = 9.0
    set_insert_text(layout, 0, "nowy tekst")
    assert insert_cut_key(0, 1) not in layout.overrides
    assert layout.overrides[book_cut_key(3)] == 9.0, "cięcia tekstu książki nie mogą ucierpieć"


# ---------------------------------------------------------------------------
# Dopasowanie wpisanego tekstu do nagrania
# ---------------------------------------------------------------------------

def test_dopasowanie_trafia_w_slowa_uslyszane():
    heard = [{"w": "raz", "s": 1.0, "e": 1.4}, {"w": "dwa", "s": 1.5, "e": 1.9}]
    assert align_free_text(["raz", "dwa"], heard, 1.0, 2.0) == [(1.0, 1.4), (1.5, 1.9)]


def test_dopasowanie_interpoluje_slowa_dopisane():
    heard = [{"w": "raz", "s": 1.0, "e": 1.4}, {"w": "cztery", "s": 2.6, "e": 3.0}]
    times = align_free_text(["raz", "dwa", "trzy", "cztery"], heard, 1.0, 3.0)
    assert times[0] == (1.0, 1.4)
    assert times[-1] == (2.6, 3.0)
    assert times[0][1] <= times[1][0] and times[1][1] <= times[2][0]


def test_dopasowanie_bez_slow_whispera_rozklada_rownomiernie():
    times = align_free_text(["a", "b", "c", "d"], [], 0.0, 4.0)
    assert len(times) == 4
    assert times[0][0] == 0.0
    assert abs(times[-1][1] - 4.0) < 0.01


def test_dopasowanie_dziala_gdy_tekst_zupelnie_inny():
    heard = [{"w": "kompletnie", "s": 1.0, "e": 1.5}, {"w": "inne", "s": 1.6, "e": 2.0}]
    times = align_free_text(["zupełnie", "przepisany", "fragment"], heard, 1.0, 2.0)
    assert len(times) == 3
    assert all(t[1] >= t[0] for t in times)


# ---------------------------------------------------------------------------
# Korekty tekstu plansz książkowych
# ---------------------------------------------------------------------------

def test_korekta_zmienia_tekst_nie_ruszajac_czasow(layout, words, analysis):
    przed = compute_cuts(layout, build_units(layout, make_tokens(9), words, []), analysis)
    set_correction(layout, 0, 3, "Poprawiony tekst planszy.")
    units = build_units(layout, make_tokens(9), words, [])
    po = compute_cuts(layout, units, analysis)

    assert units[0].text == "Poprawiony tekst planszy."
    assert units[0].corrected
    assert [c.time for c in po] == [c.time for c in przed]


def test_korekta_do_nieistniejacego_zakresu_jest_sierota(layout):
    set_correction(layout, 0, 3, "Poprawka")
    assert stale_corrections(layout) == []
    set_correction(layout, 0, 99, "Poprawka do zakresu, którego nie ma")
    assert "0-99" in stale_corrections(layout)


def test_pusta_korekta_kasuje_poprawke(layout):
    set_correction(layout, 0, 3, "Poprawka")
    set_correction(layout, 0, 3, None)
    assert layout.corrections == {}


def test_korekta_trafia_do_plansz_wyjsciowych(layout, words, analysis):
    set_correction(layout, 0, 3, "Poprawiony tekst.")
    units = build_units(layout, make_tokens(9), words, [])
    boards = boards_from_units(units, compute_cuts(layout, units, analysis))
    assert boards[0]["text"] == "Poprawiony tekst."
    assert boards[0]["corrected"] is True


# ---------------------------------------------------------------------------
# Przesuwanie granic wewnątrz wstawki
# ---------------------------------------------------------------------------

def test_granice_wstawki_da_sie_przesuwac(layout):
    split_insert(layout, 0, 0, 4, 10)
    move_insert(layout, 0, 0, 5, 10)
    assert insert_state(layout, 0)["breaks"] == [5]


def test_przesuniecie_granicy_wstawki_nie_moze_przeskoczyc_sasiedniej(layout):
    split_insert(layout, 0, 0, 3, 10)
    split_insert(layout, 0, 1, 6, 10)
    with pytest.raises(LayoutError, match="można przesunąć tylko"):
        move_insert(layout, 0, 0, 6, 10)


def test_przesuniecie_granicy_wstawki_nie_moze_wyjsc_poza_wstawke(layout):
    split_insert(layout, 0, 0, 4, 10)
    with pytest.raises(LayoutError):
        move_insert(layout, 0, 0, 0, 10)
    with pytest.raises(LayoutError):
        move_insert(layout, 0, 0, 10, 10)


def test_nieistniejaca_granica_wstawki_jest_odrzucana(layout):
    with pytest.raises(LayoutError, match="nie ma granicy"):
        move_insert(layout, 0, 0, 3, 10)


def test_przesuniecie_kasuje_czas_tylko_swojej_granicy(layout):
    """Cięcia pozostałych plansz wstawki mają przetrwać — inaczej ruch granicy
    kasowałby ręczną pracę na całej wstawce."""
    split_insert(layout, 0, 0, 3, 12)
    split_insert(layout, 0, 1, 7, 12)
    layout.overrides[insert_cut_key(0, 1)] = 5.0
    layout.overrides[insert_cut_key(0, 2)] = 9.0

    move_insert(layout, 0, 0, 4, 12)          # granica 0 otwiera planszę 1

    assert insert_cut_key(0, 1) not in layout.overrides
    assert layout.overrides[insert_cut_key(0, 2)] == 9.0
