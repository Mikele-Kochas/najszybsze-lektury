"""
Testy modelu podziału na plansze (Engine/layout.py).

Nie potrzebują nagrań ani Whispera — pracują na sztucznym rozdziale zbudowanym
w pamięci, więc chodzą w sekundę i nie zależą od zawartości Data/.

    python -m pytest Tests/test_layout.py -q
"""

import os
import sys
import json

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Engine.text_parser import build_chapter
from Engine.layout import (
    Layout, LayoutError, LayoutStore,
    tokenize_chapter, render_text, tokens_hash,
    propose_layout, split_board, merge_boards, move_break, set_cut_time,
    boards_from_layout, validate_layout, word_times_from_payload, set_reviewed,
)

SAMPLE = """Dawno, dawno temu żył sobie doktor, który mieszkał w małym miasteczku
nad rzeką i leczył zwierzęta z całej okolicy.

Miał psa, świnkę, kaczkę oraz papugę, a każde z nich mówiło własnym językiem.

— Czy naprawdę uważasz, że to ma sens? — zapytała siostra.
— Owszem — odparł Doktor spokojnie.
— W takim razie odchodzę.

Na tym rozmowa się skończyła i nikt do niej nie wracał przez wiele lat.
"""


@pytest.fixture
def chapter():
    return build_chapter(number=1, raw_text=SAMPLE, header="Rozdział pierwszy")


@pytest.fixture
def tokens(chapter):
    return tokenize_chapter(chapter)


@pytest.fixture
def layout(chapter, tokens):
    return propose_layout(chapter, tokens, max_lines=11, max_chars_per_line=45)


# ---------------------------------------------------------------------------
# Tokenizacja
# ---------------------------------------------------------------------------

def test_tokenizacja_zachowuje_wszystkie_slowa(chapter, tokens):
    assert [t.word for t in tokens] == SAMPLE.split()


def test_render_odtwarza_tekst_bez_utraty_slow(tokens):
    assert render_text(tokens).split() == [t.word for t in tokens]


def test_offsety_wskazuja_wlasciwe_slowa(tokens):
    text = render_text(tokens)
    for token in tokens:
        assert text[token.start:token.end] == token.word


def test_dialogi_sa_rozpoznane(tokens):
    assert any(t.is_dialogue for t in tokens)
    assert any(not t.is_dialogue for t in tokens)


def test_hash_reaguje_na_zmiane_tekstu(chapter):
    inny = build_chapter(number=1, raw_text=SAMPLE.replace("doktor", "aptekarz"))
    assert tokens_hash(tokenize_chapter(chapter)) != tokens_hash(tokenize_chapter(inny))


# ---------------------------------------------------------------------------
# Plansze jako widok pochodny
# ---------------------------------------------------------------------------

def test_plansze_pokrywaja_caly_tekst_bez_dziur(layout, tokens):
    boards = boards_from_layout(layout, tokens)
    assert boards[0]["token_start"] == 0
    assert boards[-1]["token_end"] == len(tokens)
    for a, b in zip(boards, boards[1:]):
        assert a["token_end"] == b["token_start"]


def test_suma_slow_plansz_rowna_sie_rozdzialowi(layout, tokens):
    boards = boards_from_layout(layout, tokens)
    assert sum(len(b["text"].split()) for b in boards) == len(tokens)


def test_numeracja_plansz_jest_ciagla(layout, tokens):
    boards = boards_from_layout(layout, tokens)
    assert [b["chunk_id"] for b in boards] == list(range(1, len(boards) + 1))


def test_niezgodna_liczba_slow_jest_zglaszana(layout, tokens):
    layout.token_count += 1
    with pytest.raises(LayoutError, match="zmienił się"):
        boards_from_layout(layout, tokens)


# ---------------------------------------------------------------------------
# Operacje edycyjne
# ---------------------------------------------------------------------------

def test_podzial_zwieksza_liczbe_plansz(layout, tokens):
    before = layout.board_count
    lo, hi = layout.bounds()[0], layout.bounds()[1]
    split_board(layout, 0, (lo + hi) // 2)
    assert layout.board_count == before + 1
    boards = boards_from_layout(layout, tokens)
    assert sum(len(b["text"].split()) for b in boards) == len(tokens)


def test_podzial_poza_plansza_jest_odrzucany(layout):
    with pytest.raises(LayoutError, match="wewnątrz planszy"):
        split_board(layout, 0, layout.bounds()[1] + 1)


def test_scalenie_odwraca_podzial(chapter, tokens):
    layout = propose_layout(chapter, tokens)
    original = list(layout.breaks)
    lo, hi = layout.bounds()[0], layout.bounds()[1]
    split_board(layout, 0, (lo + hi) // 2)
    merge_boards(layout, 0)
    assert layout.breaks == original


def test_scalenie_ostatniej_planszy_jest_odrzucane(layout):
    with pytest.raises(LayoutError, match="nie ma następnej"):
        merge_boards(layout, layout.board_count - 1)


def test_przesuniecie_granicy_zmienia_przydzial_slow(layout, tokens):
    assert layout.breaks, "rozdział testowy musi mieć co najmniej dwie plansze"
    stary = layout.breaks[0]
    move_break(layout, 0, stary + 1)
    boards = boards_from_layout(layout, tokens)
    assert boards[0]["token_end"] == stary + 1
    assert boards[1]["token_start"] == stary + 1


def test_granica_nie_moze_przeskoczyc_sasiedniej(layout):
    if len(layout.breaks) < 2:
        pytest.skip("potrzebne co najmniej trzy plansze")
    with pytest.raises(LayoutError, match="można przesunąć tylko"):
        move_break(layout, 0, layout.breaks[1] + 1)


def test_granica_nie_moze_wyjsc_poza_rozdzial(layout):
    with pytest.raises(LayoutError):
        move_break(layout, 0, 0)


# ---------------------------------------------------------------------------
# Czasy cięć
# ---------------------------------------------------------------------------

def _fake_times(tokens):
    """Sekunda na słowo — czytelne czasy do sprawdzania arytmetyki."""
    return [(float(i), float(i) + 0.9) for i in range(len(tokens))]


def test_czasy_plansz_biora_sie_z_alignmentu(layout, tokens):
    boards = boards_from_layout(layout, tokens, _fake_times(tokens))
    for b in boards:
        assert b["start_time"] == float(b["token_start"])
        assert b["end_time"] == float(b["token_end"] - 1) + 0.9


def test_reczny_czas_nadpisuje_obie_sasiednie_plansze(layout, tokens):
    granica = layout.breaks[0]
    set_cut_time(layout, granica, 123.456)
    boards = boards_from_layout(layout, tokens, _fake_times(tokens))
    assert boards[0]["end_time"] == 123.456
    assert boards[1]["start_time"] == 123.456
    assert boards[0]["manual_end"] and boards[1]["manual_start"]


def test_reczny_czas_na_nieistniejacej_granicy_jest_odrzucany(layout):
    with pytest.raises(LayoutError, match="nie stoi żadna granica"):
        set_cut_time(layout, layout.breaks[0] + 1, 10.0)


def test_czas_glowy_i_ogona_jest_dozwolony(layout, tokens):
    set_cut_time(layout, 0, 1.5)
    set_cut_time(layout, layout.token_count, 999.0)
    boards = boards_from_layout(layout, tokens, _fake_times(tokens))
    assert boards[0]["start_time"] == 1.5
    assert boards[-1]["end_time"] == 999.0


def test_scalenie_kasuje_czas_znikajacej_granicy(layout, tokens):
    granica = layout.breaks[0]
    set_cut_time(layout, granica, 55.0)
    merge_boards(layout, 0)
    assert granica not in layout.overrides


def test_przesuniecie_granicy_kasuje_jej_stary_czas(layout):
    granica = layout.breaks[0]
    set_cut_time(layout, granica, 55.0)
    move_break(layout, 0, granica + 1)
    assert layout.overrides == {}


def test_niezgodna_dlugosc_czasow_jest_zglaszana(layout, tokens):
    with pytest.raises(LayoutError, match="Czasy słów"):
        boards_from_layout(layout, tokens, _fake_times(tokens)[:-1])


def test_czasy_slow_czytane_z_wyniku_pipelinu():
    payload = {"chapter_num": 1, "words": [
        {"w": "Dawno,", "s": 46.77, "e": 47.23, "m": True},
        {"w": "dawno", "s": 47.37, "e": 47.79, "m": True},
    ]}
    assert word_times_from_payload(payload) == [(46.77, 47.23), (47.37, 47.79)]


def test_wynik_bez_czasow_slow_daje_czytelny_blad():
    with pytest.raises(LayoutError, match="Przetwórz go ponownie"):
        word_times_from_payload({"chapter_num": 7, "chunks": []})


# ---------------------------------------------------------------------------
# Ostrzeżenia i zapis
# ---------------------------------------------------------------------------

def test_scalenie_wszystkiego_daje_ostrzezenie_o_dlugosci(chapter, tokens):
    layout = propose_layout(chapter, tokens)
    while layout.board_count > 1:
        merge_boards(layout, 0)

    # Limit dobrany tak, by scalona plansza musiała go przekroczyć — chodzi
    # o sprawdzenie, że ostrzeżenie w ogóle powstaje, a nie o konkretną liczbę.
    limit = 5
    scalona = boards_from_layout(layout, tokens)[0]
    assert scalona["lines_count"] > limit

    warnings = validate_layout(layout, tokens, max_lines=limit)
    assert [w["level"] for w in warnings] == ["warning"]


def test_podzial_startowy_nie_lamie_limitu(layout, tokens):
    assert [w for w in validate_layout(layout, tokens, max_lines=11) if w["level"] == "error"] == []


def test_zapis_i_odczyt_zachowuja_podzial(tmp_path, layout):
    set_cut_time(layout, layout.breaks[0], 42.5)
    store = LayoutStore(str(tmp_path))
    store.save(layout)
    wczytany = store.load(layout.chapter_num, expected_hash=layout.text_hash)
    assert wczytany is not None
    assert wczytany.breaks == layout.breaks
    assert wczytany.overrides == layout.overrides


def test_podzial_do_innej_wersji_tekstu_jest_odrzucany(tmp_path, layout):
    store = LayoutStore(str(tmp_path))
    store.save(layout)
    assert store.load(layout.chapter_num, expected_hash="inny-hash") is None


def test_klucze_overrides_przezywaja_json(tmp_path, layout):
    set_cut_time(layout, layout.breaks[0], 7.25)
    store = LayoutStore(str(tmp_path))
    store.save(layout)
    with open(store.path(layout.chapter_num), encoding="utf-8") as f:
        surowy = json.load(f)
    assert list(surowy["overrides"]) == [str(layout.breaks[0])]
    assert store.load(layout.chapter_num).overrides == {layout.breaks[0]: 7.25}


# ---------------------------------------------------------------------------
# Znacznik przejrzenia granicy
# ---------------------------------------------------------------------------

def test_granice_da_sie_oznaczyc_jako_przejrzane(layout):
    set_reviewed(layout, layout.breaks[0])
    assert layout.reviewed == [layout.breaks[0]]
    set_reviewed(layout, layout.breaks[0], False)
    assert layout.reviewed == []


def test_znacznik_przejrzenia_przezywa_zapis(tmp_path, layout):
    set_reviewed(layout, layout.breaks[0])
    store = LayoutStore(str(tmp_path))
    store.save(layout)
    assert store.load(layout.chapter_num).reviewed == layout.reviewed


def test_scalenie_kasuje_znacznik_znikajacej_granicy(layout):
    granica = layout.breaks[0]
    set_reviewed(layout, granica)
    merge_boards(layout, 0)
    assert granica not in layout.reviewed


def test_przesuniecie_granicy_kasuje_jej_znacznik(layout):
    granica = layout.breaks[0]
    set_reviewed(layout, granica)
    move_break(layout, 0, granica + 1)
    assert layout.reviewed == []


def test_znacznik_na_nieistniejacej_granicy_jest_odrzucany(layout):
    with pytest.raises(LayoutError, match="nie stoi żadna granica"):
        set_reviewed(layout, layout.breaks[0] + 1)
