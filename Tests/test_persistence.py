"""
Testy trwałości ręcznej pracy: korekt tekstu, zapisu podziału i tożsamości plansz.

Wszystkie pilnują jednej zasady: to, co użytkownik wpisał albo ustawił ręcznie, nie
może zniknąć ani trafić w niewłaściwą planszę przy zmianie granic. Każdy z tych testów
odpowiada błędowi, który realnie gubił pracę.

    python -m pytest Tests/test_persistence.py -q
"""

import os
import sys
import json

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Engine.layout import (
    Layout, LayoutStore, Token, book_cut_key, correction_key, render_text,
    set_correction, split_board, merge_boards, move_break, stale_corrections,
)
from Engine.srt_writer import srt_cue_text, generate_srt_content
from Engine.chunker import BoardChunk


def toks(n):
    return [Token(word=f"w{i}", sep=" ", block=0, is_dialogue=False, start=0, end=0)
            for i in range(n)]


@pytest.fixture
def tokens():
    return toks(100)


@pytest.fixture
def layout():
    # plansze: [0,20) [20,50) [50,100)
    return Layout(chapter_num=1, text_hash="x", token_count=100, breaks=[20, 50])


# ---------------------------------------------------------------------------
# Korekty przeżywają zmianę granic
# ---------------------------------------------------------------------------

def test_scalenie_zachowuje_korekte(layout, tokens):
    set_correction(layout, 20, 50, "POPRAWIONY TEKST")
    merge_boards(layout, 1, tokens)

    assert stale_corrections(layout) == []
    assert layout.corrections[correction_key(20, 100)].startswith("POPRAWIONY TEKST")


def test_scalenie_sklada_obie_korekty_w_kolejnosci(layout, tokens):
    set_correction(layout, 20, 50, "PIERWSZA")
    set_correction(layout, 50, 100, "DRUGA")
    merge_boards(layout, 1, tokens)

    assert layout.corrections[correction_key(20, 100)] == "PIERWSZA DRUGA"
    assert stale_corrections(layout) == []


def test_scalenie_dokleja_tekst_ksiazki_sasiada(layout, tokens):
    """Sąsiad bez korekty wnosi swój tekst oryginalny — plansza ma wyglądać tak samo."""
    set_correction(layout, 20, 50, "POPRAWIONA")
    merge_boards(layout, 1, tokens)

    scalona = layout.corrections[correction_key(20, 100)]
    assert scalona == "POPRAWIONA " + render_text(tokens, 50, 100)


def test_podzial_rozdziela_korekte_na_obie_plansze(layout, tokens):
    set_correction(layout, 20, 50, "alfa beta gamma delta epsilon zeta")
    split_board(layout, 1, 35, tokens)

    assert stale_corrections(layout) == []
    assert layout.corrections[correction_key(20, 35)] == "alfa beta gamma"
    assert layout.corrections[correction_key(35, 50)] == "delta epsilon zeta"


def test_przesuniecie_granicy_nie_rusza_tresci_korekty(layout, tokens):
    """Przy przesunięciu granicy tekst użytkownika zostaje nietknięty, zmienia się klucz."""
    set_correction(layout, 20, 50, "KRÓTKA KOREKTA")
    move_break(layout, 1, 51, tokens)

    assert stale_corrections(layout) == []
    assert layout.corrections[correction_key(20, 51)] == "KRÓTKA KOREKTA"
    assert correction_key(20, 50) not in layout.corrections


def test_operacje_nie_tworza_korekt_tam_gdzie_ich_nie_bylo(layout, tokens):
    merge_boards(layout, 1, tokens)
    split_board(layout, 0, 10, tokens)
    move_break(layout, 0, 11, tokens)
    assert layout.corrections == {}


def test_korekta_rowna_oryginalowi_nie_jest_zapisywana(layout, tokens):
    set_correction(layout, 20, 50, render_text(tokens, 20, 50))
    split_board(layout, 1, 35, tokens)
    # Obie połowy wychodzą identyczne z tekstem książki — nie ma czego poprawiać.
    assert layout.corrections == {}


def test_bez_tokenow_korekta_zostaje_nietknieta(layout):
    """Wywołanie bez tokenów nie potrafi odtworzyć tekstu — nie zgaduje, tylko zostawia."""
    set_correction(layout, 20, 50, "KOREKTA")
    merge_boards(layout, 1)
    assert layout.corrections[correction_key(20, 50)] == "KOREKTA"


def test_scalenie_nadal_kasuje_czas_znikajacej_granicy(layout, tokens):
    layout.overrides[book_cut_key(50)] = 12.5
    set_correction(layout, 20, 50, "KOREKTA")
    merge_boards(layout, 1, tokens)
    assert book_cut_key(50) not in layout.overrides


# ---------------------------------------------------------------------------
# Zapis podziału
# ---------------------------------------------------------------------------

def test_zapis_nie_zostawia_pliku_tymczasowego(tmp_path, layout):
    store = LayoutStore(str(tmp_path))
    store.save(layout)
    assert not os.path.exists(store.path(1) + ".tmp")
    assert store.load(1) is not None


def test_podzial_do_innej_wersji_tekstu_trafia_do_archiwum(tmp_path, layout):
    store = LayoutStore(str(tmp_path))
    layout.text_hash = "stary"
    store.save(layout)

    assert store.load(1, expected_hash="nowy") is None
    odlozone = store.archived(1)
    assert len(odlozone) == 1, "podział musi zostać odłożony, a nie zniknąć"

    with open(odlozone[0], "r", encoding="utf-8") as f:
        assert json.load(f)["breaks"] == [20, 50]


def test_uszkodzony_podzial_trafia_do_archiwum(tmp_path, layout):
    store = LayoutStore(str(tmp_path))
    store.save(layout)
    with open(store.path(1), "w", encoding="utf-8") as f:
        f.write('{"chapter_num": 1, "breaks": [20,')  # przerwany zapis

    assert store.load(1) is None
    assert len(store.archived(1)) == 1


def test_archiwum_nie_gubi_wczesniejszych_wersji(tmp_path, layout):
    store = LayoutStore(str(tmp_path))
    for wersja in ("a", "b"):
        layout.text_hash = wersja
        store.save(layout)
        assert store.load(1, expected_hash="inny") is None
    assert len(store.archived(1)) == 2


# ---------------------------------------------------------------------------
# SRT
# ---------------------------------------------------------------------------

def test_pusta_linia_w_korekcie_nie_rozbija_srt():
    """Pusta linia kończy napis w formacie SRT — w treści planszy nie może jej być."""
    tekst = "Pierwszy akapit.\n\n\nDrugi akapit."
    assert srt_cue_text(tekst) == "Pierwszy akapit.\nDrugi akapit."

    srt = generate_srt_content([
        BoardChunk(chunk_id=1, text=tekst, chunk_type="narration",
                   lines_count=2, start_time=0.0, end_time=1.0),
        BoardChunk(chunk_id=2, text="Trzeci.", chunk_type="narration",
                   lines_count=1, start_time=1.0, end_time=2.0),
    ])
    # Dwa napisy to dokładnie dwa bloki rozdzielone pustą linią.
    bloki = [b for b in srt.strip().split("\n\n") if b.strip()]
    assert len(bloki) == 2
    assert bloki[0].splitlines()[0] == "1"
    assert bloki[1].splitlines()[0] == "2"
