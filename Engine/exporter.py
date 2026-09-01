import os
import re
import shutil
import zipfile
import subprocess
from typing import List, Dict, Any, Optional, Callable, Tuple

from .chunker import BoardChunk
from .srt_writer import write_srt_file
from .project import safe_component, ILLEGAL_NAME_CHARS

ProgressCb = Optional[Callable[[float, str], None]]

PLACEHOLDER_LABEL = "BRAK TEKSTU W ŹRÓDLE"
LEADING_PUNCT = r'^[—–\-"\'„“\s]+'
# Znacznik dopisywany do plansz z wtrąceniem lektora. W etykiecie i nazwie pliku
# liczy się to, co lektor faktycznie powiedział, więc znacznik zdejmujemy.
NO_SOURCE_MARKER_RE = re.compile(r'\(brak tekstu[^)]*\)', re.IGNORECASE)


def strip_marker(text: str) -> str:
    return NO_SOURCE_MARKER_RE.sub('', text or '').strip()


def sanitize_filename(text: str, max_words: int = 5) -> str:
    """Bierze pierwsze N słów i usuwa znaki niedozwolone w nazwach plików."""
    cleaned = re.sub(LEADING_PUNCT, '', strip_marker(text)).strip()
    words = cleaned.split()
    name_str = " ".join(words[:max_words]) if words else "fragment"
    safe_name = "".join(ch for ch in name_str if ch not in ILLEGAL_NAME_CHARS)
    safe_name = re.sub(r'\s+', ' ', safe_name).strip(' .')
    return safe_name if safe_name else "audio_fragment"


def generate_slashed_text(chunks: List[Dict[str, Any]]) -> str:
    """Ciągły tekst rozdziału z separatorem /// na granicach plansz."""
    texts = [c.get("text", "").strip() for c in chunks]
    return "\n///\n".join(t for t in texts if t)


def generate_audacity_labels(chunks: List[Dict[str, Any]]) -> str:
    """Etykiety w formacie Audacity Label Track: start\\tend\\tETYKIETA."""
    lines = []
    for c in chunks:
        start = float(c.get("start_time", 0.0))
        end = float(c.get("end_time", 0.0))
        text = c.get("text", "").strip()

        words = re.sub(LEADING_PUNCT, '', strip_marker(text)).split()[:5]
        if words:
            label = " ".join(words).upper()
        elif c.get("chunk_type") == "intro_outro":
            label = PLACEHOLDER_LABEL
        else:
            label = f"PLANSZA {c.get('chunk_id', 1)}"

        label = re.sub(r'[\t\r\n]+', ' ', label).strip()
        lines.append(f"{start:.6f}\t{end:.6f}\t{label}")

    return "\n".join(lines)


def slice_audio_with_ffmpeg(
    source_audio_path: str,
    chunks: List[Dict[str, Any]],
    output_audio_dir: str,
    progress_cb: ProgressCb = None,
    failures: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    """
    Tnie nagranie źródłowe na osobne MP3 per plansza.

    Plansze, których nie udało się wyciąć, dopisujemy do `failures`. Bez tego brakujący
    plik w paczce był widoczny wyłącznie w logu serwera, a eksport meldował sukces.
    """
    os.makedirs(output_audio_dir, exist_ok=True)
    generated_files: List[str] = []
    total = len(chunks)

    for idx, c in enumerate(chunks, start=1):
        start = float(c.get("start_time", 0.0))
        end = float(c.get("end_time", 0.0))
        duration = max(0.1, end - start)
        out_filename = f"{idx:03d} - {sanitize_filename(c.get('text', ''), max_words=5)}.mp3"
        out_path = os.path.join(output_audio_dir, out_filename)

        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", f"{start:.3f}",
            "-i", source_audio_path,
            "-t", f"{duration:.3f}",
            "-c:a", "libmp3lame", "-q:a", "2",
            out_path,
        ]
        try:
            subprocess.run(cmd, capture_output=True, check=True)
            generated_files.append(out_path)
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or b"").decode("utf-8", errors="replace").strip()[:200]
            print(f"[Exporter] Nie udało się wyciąć planszy #{idx} ({out_filename}): {detail}")
            if failures is not None:
                failures.append({"chunk_id": c.get("chunk_id", idx),
                                 "file": out_filename, "reason": detail})
        except FileNotFoundError:
            raise RuntimeError(
                "Nie znaleziono ffmpeg. Zainstaluj ffmpeg i dodaj go do PATH, "
                "albo wyłącz cięcie audio przy eksporcie."
            )

        if progress_cb and total:
            progress_cb(idx / total, f"Cięcie audio {idx}/{total}")

    return generated_files


def export_chapter_package(
    chapter_data: Dict[str, Any],
    book_output_dir: str,
    slice_audio: bool = True,
    clean: bool = True,
    progress_cb: ProgressCb = None,
    failures: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Eksportuje jeden rozdział do struktury:
      Książka X/Rozdział NN/{Teksty/*.txt,*.srt, Audio/*.mp3}
    """
    ch_num = chapter_data.get("chapter_num", 1)
    ch_dir = os.path.join(book_output_dir, f"Rozdział {ch_num:02d}")
    texts_dir = os.path.join(ch_dir, "Teksty")
    audio_dir = os.path.join(ch_dir, "Audio")

    # Bez czyszczenia w paczce zostają pliki z poprzedniego przebiegu, gdy liczba
    # plansz zmalała (np. po scaleniu plansz w montażu). Czyścimy też przy eksporcie
    # bez cięcia audio — inaczej w paczce zostawały MP3 z nieaktualnego podziału,
    # nazwane tekstem, którego już nie ma na żadnej planszy.
    if clean and os.path.isdir(audio_dir):
        shutil.rmtree(audio_dir, ignore_errors=True)

    os.makedirs(texts_dir, exist_ok=True)
    os.makedirs(audio_dir, exist_ok=True)

    chunks = chapter_data.get("chunks", [])

    slashed_path = os.path.join(texts_dir, "Tekst_zrodlowy_slashe.txt")
    with open(slashed_path, 'w', encoding='utf-8') as f:
        f.write(generate_slashed_text(chunks))

    srt_path = os.path.join(texts_dir, f"Rozdzial_{ch_num:02d}.srt")
    write_srt_file(
        [
            BoardChunk(
                chunk_id=c["chunk_id"],
                text=c["text"],
                chunk_type=c["chunk_type"],
                lines_count=c["lines_count"],
                start_time=c["start_time"],
                end_time=c["end_time"],
            )
            for c in chunks
        ],
        srt_path,
    )

    audacity_path = os.path.join(texts_dir, "Etykiety_Audacity.txt")
    with open(audacity_path, 'w', encoding='utf-8') as f:
        f.write(generate_audacity_labels(chunks))

    sliced_count = 0
    source_audio = chapter_data.get("audio_path")
    if slice_audio and source_audio and os.path.exists(source_audio):
        sliced_count = len(slice_audio_with_ffmpeg(
            source_audio, chunks, audio_dir, progress_cb, failures))
    elif slice_audio:
        print(f"[Exporter] Rozdział {ch_num}: brak pliku źródłowego audio, pomijam cięcie.")
        if failures is not None:
            failures.append({"chunk_id": None, "file": f"Rozdział {ch_num:02d}",
                             "reason": "brak pliku źródłowego audio"})

    return {
        "chapter_num": ch_num,
        "chapter_dir": ch_dir,
        "texts_dir": texts_dir,
        "audio_dir": audio_dir,
        "slashed_file": slashed_path,
        "srt_file": srt_path,
        "audacity_file": audacity_path,
        "sliced_audio_count": sliced_count,
    }


def create_book_zip_package(
    book_name: str,
    packages_base_dir: str,
    processed_chapters: List[Dict[str, Any]],
    slice_audio: bool = True,
    progress_cb: ProgressCb = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Buduje pełną strukturę katalogów książki i pakuje ją do {book_name}.zip.

    Zwraca (ścieżka_zip, nieudane_cięcia).
    """
    safe_book_name = safe_component(book_name, "Ksiazka")
    book_dir = os.path.join(packages_base_dir, safe_book_name)

    # Katalog paczki jest w całości odtwarzalny, więc budujemy go od zera. Bez tego
    # zostawały w nim rozdziały z poprzedniego przebiegu — po skróceniu książki
    # trafiały do ZIP-a rozdziały, których w projekcie już nie ma.
    if os.path.isdir(book_dir):
        shutil.rmtree(book_dir, ignore_errors=True)
    os.makedirs(book_dir, exist_ok=True)

    failures: List[Dict[str, Any]] = []
    total = max(1, len(processed_chapters))
    for idx, ch_data in enumerate(processed_chapters):
        base = idx / total
        span = 0.9 / total
        if progress_cb:
            progress_cb(base, f"Rozdział {ch_data.get('chapter_num')}: zapis tekstów...")
        export_chapter_package(
            ch_data,
            book_dir,
            slice_audio=slice_audio,
            progress_cb=(lambda p, m, _b=base, _s=span: progress_cb(_b + _s * p, m)) if progress_cb else None,
            failures=failures,
        )

    if progress_cb:
        progress_cb(0.92, "Pakowanie archiwum ZIP...")

    zip_output_path = os.path.join(packages_base_dir, f"{safe_book_name}.zip")
    with zipfile.ZipFile(zip_output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, _dirs, files in os.walk(book_dir):
            for file in files:
                file_path = os.path.join(root, file)
                zipf.write(file_path, os.path.relpath(file_path, packages_base_dir))

    size_mb = os.path.getsize(zip_output_path) / 1_048_576
    print(f"[Exporter] Paczka ZIP: {zip_output_path} ({size_mb:.1f} MB)")
    if failures:
        print(f"[Exporter] Nie wycięto {len(failures)} plansz — paczka jest niepełna.")
    if progress_cb:
        progress_cb(1.0, f"Zapakowano {size_mb:.1f} MB")
    return zip_output_path, failures
