"""
Testy tożsamości plansz: która plansza dostaje poprawkę.

Sedno naprawionego błędu: `chunk_id` to wyłącznie pozycja na liście. Po scaleniu albo
podziale ta sama pozycja wskazuje inną planszę, a poprawka wpisana w studiu trafiała
w sąsiedni fragment tekstu — z zielonym komunikatem „zapisano". Stabilny jest `cut_key`,
i to on musi decydować o miejscu zapisu.

    python -m pytest Tests/test_board_identity.py -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Engine.layout import Layout, Token, merge_boards, split_board, set_correction
from Engine.timing import build_units, compute_cuts, boards_from_units

from test_timing import make_analysis, make_tokens, evenly, SPANS
from test_inserts import extra


@pytest.fixture
def analysis():
    return make_analysis(SPANS)


@pytest.fixture
def words():
    return evenly(9, SPANS)


@pytest.fixture
def layout():
    return Layout(chapter_num=1, text_hash="x", token_count=9, breaks=[3, 6])


def boards(layout, words, analysis, extras=()):
    tokens = make_tokens(9)
    units = build_units(layout, tokens, words, list(extras))
    return boards_from_units(units, compute_cuts(layout, units, analysis))


def find(boards_list, cut_key):
    return next((b for b in boards_list if b["cut_key"] == cut_key), None)


# ---------------------------------------------------------------------------
# cut_key jest stabilny, chunk_id nie
# ---------------------------------------------------------------------------

def test_kazda_plansza_ma_klucz_ciecia(layout, words, analysis):
    for b in boards(layout, words, analysis):
        assert b["cut_key"], "plansza bez cut_key nie da się bezpiecznie zaadresować"


def test_klucze_ciec_sa_unikalne(layout, words, analysis):
    ext = [extra(0, 0.0, 0.4, "wstep lektora")]
    keys = [b["cut_key"] for b in boards(layout, words, analysis, ext)]
    assert len(keys) == len(set(keys))


def test_numer_planszy_przesuwa_sie_po_wstawce_a_klucz_nie(layout, words, analysis):
    """Wstawka wchodzi przed plansze książki i przesuwa całą numerację o jeden."""
    bez = boards(layout, words, analysis)
    ze = boards(layout, words, analysis, [extra(0, 0.0, 0.4, "wstep lektora")])

    pierwsza = find(bez, "b0")
    assert pierwsza["chunk_id"] == 1
    assert find(ze, "b0")["chunk_id"] == 2, "numer się przesunął..."
    assert find(ze, "b0")["text"] == pierwsza["text"], "...ale klucz wskazuje ten sam tekst"


def test_scalenie_zmienia_znaczenie_numeru_ale_nie_klucza(layout, words, analysis):
    przed = boards(layout, words, analysis)
    tekst_drugiej = find(przed, "b3")["text"]

    merge_boards(layout, 0, make_tokens(9))  # scala plansze [0,3) i [3,6)
    po = boards(layout, words, analysis)

    # Numer 2 wskazuje teraz zupełnie inny fragment...
    assert po[1]["text"] != tekst_drugiej
    # ...a klucz b3 przestał istnieć, zamiast po cichu wskazać sąsiada.
    assert find(po, "b3") is None
    assert find(po, "b0") is not None


def test_podzial_tworzy_nowy_klucz_zachowujac_stary(layout, words, analysis):
    split_board(layout, 0, 2, make_tokens(9))
    po = boards(layout, words, analysis)
    assert find(po, "b0") is not None
    assert find(po, "b2") is not None


# ---------------------------------------------------------------------------
# Korekta trafia tam, gdzie wskazał użytkownik
# ---------------------------------------------------------------------------

def test_korekta_po_kluczu_trafia_we_wlasciwa_plansze(layout, words, analysis):
    """Odtworzenie realnego scenariusza: scalenie, potem poprawka drugiej planszy."""
    tokens = make_tokens(9)
    merge_boards(layout, 0, tokens)          # plansze: [0,6) [6,9)
    po_scaleniu = boards(layout, words, analysis)

    cel = po_scaleniu[1]                      # plansza [6,9), klucz b6
    set_correction(layout, cel["token_start"], cel["token_end"], "POPRAWIONY TEKST")

    wynik = boards(layout, words, analysis)
    assert find(wynik, cel["cut_key"])["text"] == "POPRAWIONY TEKST"
    assert find(wynik, "b0")["text"] != "POPRAWIONY TEKST", "sąsiad nie może dostać cudzej poprawki"


def test_korekta_przezywa_kolejne_scalenie(layout, words, analysis):
    tokens = make_tokens(9)
    set_correction(layout, 3, 6, "POPRAWKA")
    assert find(boards(layout, words, analysis), "b3")["text"] == "POPRAWKA"

    merge_boards(layout, 1, tokens)           # scala [3,6) z [6,9)
    scalona = find(boards(layout, words, analysis), "b3")
    assert scalona is not None
    assert scalona["text"].startswith("POPRAWKA")
    assert scalona["corrected"] is True


def test_plansza_wstawki_ma_wlasny_klucz_nie_klucz_ksiazki(layout, words, analysis):
    ext = [extra(0, 0.0, 0.4, "wstep lektora")]
    wstawka = boards(layout, words, analysis, ext)[0]
    assert wstawka["kind"] == "insert"
    assert wstawka["cut_key"] == "i0.0"
    assert wstawka["cut_key"] != "b0", (
        "wstawka i pierwsza plansza książki stoją w tym samym miejscu tekstu — "
        "muszą mieć różne klucze, inaczej edycja jednej nadpisuje drugą"
    )
