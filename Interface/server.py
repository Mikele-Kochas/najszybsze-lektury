import os
import sys
import json
import random
import threading
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

# Katalog główny projektu w sys.path, aby działały importy pakietu Engine.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from Engine.pipeline import PipelineManager
from Engine.jobs import JobManager

app = FastAPI(title="Najszybsze Lektury — Studio Produkcyjne")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
REVIEWS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reviews.json")

ALLOWED_AUDIO_EXT = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".mp4", ".aac"}

pipeline = PipelineManager(base_dir=BASE_DIR)
jobs = JobManager()

# Ostatnia propozycja mapy rozdziałów — kreator pobiera ją po zakończeniu zadania detekcji.
_last_proposal: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Modele żądań
# ---------------------------------------------------------------------------

class ReviewItem(BaseModel):
    chapter_num: int
    chunk_id: int
    # Ocena dotyczy konkretnej planszy, a numer chunk_id zmienia znaczenie po każdym
    # scaleniu czy podziale. Klucz cięcia przeżywa te operacje.
    cut_key: Optional[str] = None
    is_correct: bool
    status: str  # 'ok' | 'mismatch' | 'missing_source' | 'timing_issue'
    comment: Optional[str] = ""
    chunk_text: Optional[str] = ""
    start_time: Optional[float] = 0.0
    end_time: Optional[float] = 0.0


class ChunkEditItem(BaseModel):
    chapter_num: int
    text: str
    # Stabilny identyfikator planszy („b<slowo>" / „i<kotwica>.<plansza>"). Numer
    # chunk_id to tylko pozycja w liscie i po scaleniu plansz wskazuje juz co innego —
    # zostaje wylacznie dla starszych klientow.
    cut_key: Optional[str] = None
    chunk_id: Optional[int] = None


class ChapterMapEntry(BaseModel):
    chapter_num: int
    header: Optional[str] = ""
    title: Optional[str] = ""
    text_start: int
    text_end: int
    audio_file: Optional[str] = None
    audio_path: Optional[str] = None
    confidence: Optional[float] = 1.0
    source: Optional[str] = "manual"


class ChapterMapPayload(BaseModel):
    chapters: List[ChapterMapEntry]


class LayoutOp(BaseModel):
    op: str          # split|merge|move|reset|reviewed|cut_time|correction|insert_*
    board: Optional[int] = None          # split, merge, insert_* — numer planszy (od 0)
    boundary: Optional[int] = None       # move — numer granicy (od 0)
    token: Optional[int] = None          # split, move — indeks słowa
    key: Optional[str] = None            # reviewed, cut_time — identyfikator cięcia
    flag: Optional[bool] = None          # reviewed — oznacz / odznacz
    time: Optional[float] = None         # cut_time — czas cięcia
    text: Optional[str] = None           # correction, insert_text — treść
    token_start: Optional[int] = None    # correction — zakres słów planszy
    token_end: Optional[int] = None
    anchor: Optional[int] = None         # insert_* — kotwica wstawki
    word: Optional[int] = None           # insert_split — słowo podziału
    word_count: Optional[int] = None     # insert_split — długość wstawki w słowach


class CutTime(BaseModel):
    key: str                             # identyfikator cięcia ("b123", "i0.1", "end")
    time: Optional[float] = None         # null przywraca czas z automatu


class SettingsPayload(BaseModel):
    model_size: Optional[str] = None
    device: Optional[str] = None
    compute_type: Optional[str] = None
    language: Optional[str] = None
    max_lines_per_board: Optional[int] = None
    max_chars_per_line: Optional[int] = None


class ProcessPayload(BaseModel):
    chapters: Optional[List[int]] = None
    use_cache: bool = True


class ExportPayload(BaseModel):
    book_name: Optional[str] = None
    slice_audio: bool = True


class ProjectMetaPayload(BaseModel):
    title: Optional[str] = None
    author: Optional[str] = None


# ---------------------------------------------------------------------------
# Pomocnicze
# ---------------------------------------------------------------------------

def load_reviews() -> List[Dict[str, Any]]:
    if os.path.exists(REVIEWS_FILE):
        try:
            with open(REVIEWS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


# RLock, bo add_review trzyma go wokół całego odczytu-zmiany-zapisu, a save_reviews
# bierze go jeszcze raz w środku.
_reviews_lock = threading.RLock()


def save_reviews(reviews: List[Dict[str, Any]]) -> None:
    """
    Zapis atomowy pod blokadą.

    Endpointy lecą w puli wątków, więc dwie oceny zapisane naraz potrafiły nadpisać
    się nawzajem, a przerwany zapis zostawiał obcięty JSON, który `load_reviews()`
    po cichu zamienia na pustą listę — czyli kasuje całą historię ocen.
    """
    tmp_path = REVIEWS_FILE + ".tmp"
    with _reviews_lock:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(reviews, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, REVIEWS_FILE)


def _same_board(review: Dict[str, Any], item: "ReviewItem") -> bool:
    """Czy zapisana ocena dotyczy tej samej planszy co nowa."""
    if review.get("chapter_num") != item.chapter_num:
        return False
    if item.cut_key and review.get("cut_key"):
        return review["cut_key"] == item.cut_key
    return review.get("chunk_id") == item.chunk_id


def require_no_active_job() -> None:
    active = jobs.active()
    if active:
        raise HTTPException(
            status_code=409,
            detail=f"Trwa już zadanie: {active['label']}. Poczekaj na zakończenie lub anuluj je.",
        )


def project_summary() -> Dict[str, Any]:
    project = pipeline.project
    audio_files = pipeline.get_audio_files()
    return {
        "title": project.title,
        "author": project.author,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "text_file": os.path.basename(project.text_file) if project.text_file else None,
        "text_present": bool(project.text_file and os.path.exists(project.text_file)),
        "audio_files": [os.path.basename(p) for p in audio_files],
        "audio_count": len(audio_files),
        "audio_mode": project.audio_mode,
        "settings": pipeline.settings,
        "status": project.status,
        "chapter_map_count": len(project.chapter_map),
        "processed_count": sum(
            1 for entry in project.chapter_map if pipeline.is_processed(int(entry["chapter_num"]))
        ),
        "device": pipeline.transcriber.describe_device(),
        "active_job": jobs.active(),
        "archived": pipeline.store.archived_project(),
    }


# ---------------------------------------------------------------------------
# Projekt: upload i konfiguracja
# ---------------------------------------------------------------------------

@app.get("/api/project")
def get_project():
    return project_summary()


@app.post("/api/project/meta")
def set_project_meta(payload: ProjectMetaPayload):
    project = pipeline.project
    if payload.title is not None:
        project.title = payload.title.strip()
    if payload.author is not None:
        project.author = payload.author.strip()
    pipeline.save_project()
    return project_summary()


@app.post("/api/upload")
async def upload_sources(
    text_file: UploadFile = File(..., description="Plik .txt z tekstem książki"),
    audio_files: List[UploadFile] = File(..., description="Pliki audio — jeden na rozdział"),
    title: str = Form(""),
    author: str = Form(""),
):
    """
    Wgrywa komplet źródeł jednego projektu: 1 plik .txt i N plików audio.
    Zastępuje poprzedni projekt wraz ze wszystkimi wynikami przetwarzania.
    """
    require_no_active_job()

    if not text_file.filename or not text_file.filename.lower().endswith(".txt"):
        raise HTTPException(status_code=400, detail="Plik tekstowy musi mieć rozszerzenie .txt")

    valid_audio = [
        f for f in audio_files
        if f.filename and os.path.splitext(f.filename)[1].lower() in ALLOWED_AUDIO_EXT
    ]
    if not valid_audio:
        raise HTTPException(
            status_code=400,
            detail=f"Wgraj przynajmniej jeden plik audio ({', '.join(sorted(ALLOWED_AUDIO_EXT))}).",
        )

    store = pipeline.store
    store.clear_sources()

    text_path = store.store_text(text_file.filename, text_file.file)
    audio_paths = sorted(store.store_audio(f.filename, f.file) for f in valid_audio)

    project = pipeline.project
    project.title = (title or os.path.splitext(os.path.basename(text_path))[0]).strip()
    project.author = (author or "").strip()
    project.text_file = text_path
    project.audio_files = audio_paths
    project.audio_mode = "multi" if len(audio_paths) > 1 else "single"
    project.chapter_map = []
    project.transcript = {}
    project.status = {"uploaded": True, "transcribed": False, "mapped": False,
                      "processed": False, "exported": False}
    pipeline.save_project()
    pipeline.invalidate()

    return {
        "success": True,
        "text_file": os.path.basename(text_path),
        "audio_files": [os.path.basename(p) for p in audio_paths],
        "project": project_summary(),
    }


@app.post("/api/project/reset")
def reset_project():
    """Usuwa źródła i wszystkie wyniki — czysty start."""
    require_no_active_job()
    pipeline.store.clear_sources()
    if os.path.exists(pipeline.store.manifest_path):
        os.remove(pipeline.store.manifest_path)
    pipeline.invalidate()
    return {"success": True, "project": project_summary()}


@app.post("/api/settings")
def update_settings(payload: SettingsPayload):
    require_no_active_job()
    settings = pipeline.update_settings(payload.model_dump(exclude_none=True))
    return {"success": True, "settings": settings, "device": pipeline.transcriber.describe_device()}


# ---------------------------------------------------------------------------
# Kreator mapowania rozdziałów
# ---------------------------------------------------------------------------

@app.post("/api/chapters/detect")
def detect_chapters():
    """Uruchamia w tle wykrywanie podziału na rozdziały (nagłówki lub kotwice audio)."""
    require_no_active_job()

    def run(handle):
        global _last_proposal
        proposal = pipeline.propose_chapter_map(
            progress_cb=lambda p, m: handle.progress(p, m)
        )
        _last_proposal = proposal
        return {
            "method": proposal["method"],
            "warnings": proposal["warnings"],
            "chapters_count": len(proposal["chapters"]),
        }

    job = jobs.submit("detect_chapters", "Wykrywanie rozdziałów", run)
    return {"success": True, "job": job.to_dict()}


@app.get("/api/chapters/proposal")
def get_chapter_proposal():
    """Zwraca ostatnią propozycję mapy rozdziałów do zatwierdzenia w kreatorze."""
    if _last_proposal is None:
        raise HTTPException(
            status_code=404,
            detail="Brak propozycji. Uruchom najpierw wykrywanie rozdziałów.",
        )
    return _last_proposal


@app.get("/api/text/slice")
def get_text_slice(start: int = Query(0, ge=0), end: int = Query(0, ge=0)):
    """Fragment tekstu źródłowego — kreator pokazuje podgląd przy korekcie granic."""
    text = pipeline.full_text()
    end = end or min(len(text), start + 1200)
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    return {"start": start, "end": end, "total": len(text), "text": text[start:end]}


@app.post("/api/chapters/map")
def save_chapter_map(payload: ChapterMapPayload):
    """Zapisuje mapę zatwierdzoną w kreatorze i kasuje nieaktualne wyniki."""
    require_no_active_job()
    if not payload.chapters:
        raise HTTPException(status_code=400, detail="Mapa rozdziałów jest pusta.")
    pipeline.save_chapter_map([c.model_dump() for c in payload.chapters])
    return {"success": True, "project": project_summary()}


# ---------------------------------------------------------------------------
# Rozdziały i przetwarzanie
# ---------------------------------------------------------------------------

@app.get("/api/chapters")
def get_chapters():
    """Lista rozdziałów wraz ze stanem przetworzenia i statystykami dopasowania."""
    try:
        chapters = pipeline.get_chapters()
    except FileNotFoundError:
        return {"chapters": [], "needs_setup": True}

    result = []
    for c in chapters:
        json_path = pipeline.chapter_json_path(c.number)
        is_processed = os.path.exists(json_path)
        audio_file = pipeline.find_audio_for_chapter(c.number)

        info = {
            "number": c.number,
            "roman": c.roman,
            "title": c.title,
            "header": c.header,
            "blocks_count": len(c.blocks),
            "audio_file": os.path.basename(audio_file) if audio_file else None,
            "is_processed": is_processed,
            "duration": None,
            "chunks_count": 0,
            "match_rate": None,
        }

        if is_processed:
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                info["duration"] = data.get("duration")
                info["chunks_count"] = len(data.get("chunks", []))
                info["match_rate"] = data.get("report", {}).get("match_percentage")
            except (json.JSONDecodeError, OSError):
                pass

        result.append(info)

    return {"chapters": result, "needs_setup": False}


@app.post("/api/process")
def process_chapters(payload: ProcessPayload):
    """Przetwarza wskazane rozdziały (lub wszystkie) jako zadanie w tle."""
    require_no_active_job()
    targets = payload.chapters
    label = (
        f"Przetwarzanie rozdziałów: {', '.join(map(str, targets))}"
        if targets else "Przetwarzanie wszystkich rozdziałów"
    )

    def run(handle):
        return pipeline.process_chapters(
            chapter_nums=targets,
            use_cache=payload.use_cache,
            progress_cb=lambda p, m: handle.progress(p, m),
            should_cancel=lambda: handle.cancelled,
        )

    job = jobs.submit("process", label, run)
    return {"success": True, "job": job.to_dict()}


@app.get("/api/chapter/{chapter_num}")
def get_chapter_data(chapter_num: int):
    """
    Wynik rozdziału z planszami wyliczonymi z zatwierdzonego podziału.

    Plansze pochodzą stąd, skąd bierze je montaż i eksport — jedna lista dla całej
    aplikacji. Wcześniej ten endpoint zwracał `chunks` zapisane przez aligner, więc
    studio weryfikacji numerowało plansze inaczej niż zapis poprawek.
    """
    return _guard(pipeline.chapter_view, chapter_num)


@app.post("/api/chunk/edit")
def edit_chunk(item: ChunkEditItem):
    """
    Zmienia tekst planszy — akceptacja tego, co usłyszał Whisper, albo własna poprawka.

    Zapis idzie do podziału rozdziału, nie do wyniku przetwarzania: plansze są z niego
    wyliczane, więc tylko tam poprawka przetrwa do eksportu paczki.
    """
    if not item.cut_key and item.chunk_id is None:
        raise HTTPException(status_code=400,
                            detail="Wskaż planszę przez cut_key albo chunk_id.")
    board = _guard(pipeline.edit_board_text, item.chapter_num, item.text,
                   cut_key=item.cut_key, chunk_id=item.chunk_id)
    return {"success": True, "updated_chunk": board}


@app.get("/api/audio/{chapter_num}")
def get_audio_file(chapter_num: int):
    audio_path = pipeline.find_audio_for_chapter(chapter_num)
    if not audio_path or not os.path.exists(audio_path):
        raise HTTPException(status_code=404, detail="Nie znaleziono pliku audio dla tego rozdziału.")
    return FileResponse(audio_path, media_type="audio/mpeg")


# ---------------------------------------------------------------------------
# Granice plansz i punkty cięcia
# ---------------------------------------------------------------------------

def _guard(fn, *args, **kwargs):
    """
    Mapuje wyjątki silnika na kody HTTP.

    Komunikaty z LayoutError są pisane pod użytkownika („Podział musi wypaść
    wewnątrz planszy 3…”), więc idą wprost do interfejsu zamiast tonąć w 500.
    """
    from Engine.layout import LayoutError

    try:
        return fn(*args, **kwargs)
    except LayoutError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/layout/overview")
def layout_overview():
    """Stan granic i cięć we wszystkich rozdziałach — pasek nawigacji obu ekranów."""
    return {"chapters": _guard(pipeline.layout_overview)}


@app.get("/api/layout/{chapter_num}")
def get_layout(chapter_num: int):
    return _guard(pipeline.layout_state, chapter_num)


@app.post("/api/layout/{chapter_num}")
def edit_layout(chapter_num: int, item: LayoutOp):
    """Podział, scalenie, przesunięcie granicy albo powrót do propozycji automatu."""
    params = {k: v for k, v in item.model_dump().items() if k != "op" and v is not None}
    return _guard(pipeline.layout_operation, chapter_num, item.op, **params)


@app.get("/api/cuts/{chapter_num}")
def get_cuts(chapter_num: int):
    return _guard(pipeline.cuts_state, chapter_num)


@app.post("/api/cuts/{chapter_num}")
def set_cut(chapter_num: int, item: CutTime):
    """Ręczne ustawienie czasu cięcia. time=null przywraca wartość z automatu."""
    _guard(pipeline.layout_operation, chapter_num, "cut_time",
           key=item.key, time=item.time)
    return _guard(pipeline.cuts_state, chapter_num)


@app.get("/api/peaks/{chapter_num}")
def get_peaks(chapter_num: int):
    """Fala do rysowania — min/max w kubełkach, base64. Liczona raz i cache'owana."""
    return _guard(pipeline.peaks_payload, chapter_num)


# ---------------------------------------------------------------------------
# Weryfikacja jakości
# ---------------------------------------------------------------------------

@app.get("/api/sample")
def get_random_sample(
    chapter_num: Optional[int] = Query(None),
    exclude_reviewed: bool = Query(False),
):
    """Losowa plansza do weryfikacji na ślepo."""
    reviews = load_reviews()
    # Ocena zapisana przed wprowadzeniem cut_key ma tylko numer planszy — bierzemy
    # oba klucze, żeby stara historia nadal wykluczała obejrzane plansze.
    reviewed_cut_keys = {(r["chapter_num"], r["cut_key"]) for r in reviews if r.get("cut_key")}
    reviewed_ids = {(r["chapter_num"], r["chunk_id"]) for r in reviews}

    available = [
        int(fn.split("_")[1].split(".")[0])
        for fn in os.listdir(pipeline.processed_json_dir)
        if fn.startswith("chapter_") and fn.endswith(".json") and fn.split("_")[1].split(".")[0].isdigit()
    ]
    if not available:
        raise HTTPException(
            status_code=400,
            detail="Żaden rozdział nie został jeszcze przetworzony.",
        )

    # Prośba o konkretny rozdział musi dostać ten rozdział albo jasną odmowę.
    # Podstawianie losowego innego wyglądało jak wynik dla wybranego i prowadziło
    # do oceniania plansz z zupełnie innego miejsca książki.
    if chapter_num is not None and chapter_num not in available:
        raise HTTPException(
            status_code=404,
            detail=f"Rozdział {chapter_num} nie został jeszcze przetworzony. "
                   f"Przetworzone: {', '.join(map(str, sorted(available)))}.",
        )
    target_ch = chapter_num if chapter_num is not None else random.choice(available)

    data = _guard(pipeline.chapter_view, target_ch)

    chunks = data.get("chunks", [])
    if not chunks:
        raise HTTPException(status_code=404, detail="Rozdział nie zawiera plansz.")

    def seen(c: Dict[str, Any]) -> bool:
        if c.get("cut_key") and (target_ch, c["cut_key"]) in reviewed_cut_keys:
            return True
        return (target_ch, c["chunk_id"]) in reviewed_ids

    eligible = chunks
    if exclude_reviewed:
        eligible = [c for c in chunks if not seen(c)] or chunks

    selected_chunk = random.choice(eligible)
    actual_idx = chunks.index(selected_chunk)

    start_t, end_t = selected_chunk["start_time"], selected_chunk["end_time"]
    whisper_text = " ".join(
        seg["text"] for seg in data.get("whisper_segments", [])
        if not (seg["end"] < start_t or seg["start"] > end_t)
    )

    return {
        "chapter_num": target_ch,
        "chapter_header": data.get("chapter_header"),
        "chapter_title": data.get("chapter_title"),
        "audio_file": data.get("audio_file"),
        "chunk": selected_chunk,
        "prev_chunk": chunks[actual_idx - 1] if actual_idx > 0 else None,
        "next_chunk": chunks[actual_idx + 1] if actual_idx < len(chunks) - 1 else None,
        "whisper_text": whisper_text,
        "total_chunks_in_chapter": len(chunks),
        "existing_review": next(
            (r for r in reviews
             if r["chapter_num"] == target_ch and (
                 (selected_chunk.get("cut_key") and r.get("cut_key") == selected_chunk["cut_key"])
                 or (not r.get("cut_key") and r["chunk_id"] == selected_chunk["chunk_id"])
             )),
            None,
        ),
    }


@app.post("/api/review")
def add_review(item: ReviewItem):
    # Odczyt i zapis pod jedną blokadą — inaczej dwie oceny zapisane naraz gubiły
    # jedną z siebie, bo obie startowały od tej samej listy.
    with _reviews_lock:
        reviews = [r for r in load_reviews() if not _same_board(r, item)]
        reviews.append({
            **item.model_dump(),
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        save_reviews(reviews)
    return {"success": True, "total_reviews": len(reviews)}


@app.get("/api/stats")
def get_stats():
    reviews = load_reviews()
    total = len(reviews)
    correct = sum(1 for r in reviews if r.get("is_correct", False))

    status_counts: Dict[str, int] = {}
    for r in reviews:
        key = r.get("status", "unknown")
        status_counts[key] = status_counts.get(key, 0) + 1

    return {
        "total": total,
        "correct": correct,
        "incorrect": total - correct,
        "accuracy_pct": round((correct / total * 100.0) if total else 100.0, 1),
        "status_counts": status_counts,
        "recent_reviews": list(reversed(reviews[-10:])),
    }


# ---------------------------------------------------------------------------
# Eksport
# ---------------------------------------------------------------------------

@app.post("/api/export")
def start_export(payload: ExportPayload):
    """Buduje paczkę książki (teksty + pocięte audio + ZIP) w tle."""
    require_no_active_job()

    def run(handle):
        return pipeline.export_package(
            book_name=payload.book_name,
            slice_audio=payload.slice_audio,
            progress_cb=lambda p, m: handle.progress(p, m),
        )

    job = jobs.submit("export", "Eksport paczki ZIP", run)
    return {"success": True, "job": job.to_dict()}


@app.get("/api/export/download")
def download_export(book_name: Optional[str] = Query(None)):
    """Pobiera gotową paczkę ZIP zbudowaną wcześniej przez /api/export."""
    from Engine.project import safe_component

    name = safe_component(book_name or pipeline.project.title or "Ksiazka", "Ksiazka")
    zip_path = os.path.join(pipeline.packages_dir, f"{name}.zip")
    if not os.path.exists(zip_path):
        raise HTTPException(
            status_code=404,
            detail="Paczka nie została jeszcze zbudowana. Uruchom najpierw eksport.",
        )
    return FileResponse(zip_path, media_type="application/zip", filename=os.path.basename(zip_path))


# ---------------------------------------------------------------------------
# Zadania w tle
# ---------------------------------------------------------------------------

@app.get("/api/jobs")
def list_jobs():
    return {"jobs": jobs.list(), "active": jobs.active()}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Nie znaleziono zadania.")
    return job.to_dict()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    if not jobs.cancel(job_id):
        raise HTTPException(status_code=400, detail="Zadania nie da się już anulować.")
    return {"success": True, "job": jobs.get(job_id).to_dict()}


class RewalidowaneStaticFiles(StaticFiles):
    """
    Pliki statyczne, o które przeglądarka pyta przy każdym wejściu.

    StaticFiles wysyła `last-modified` i `etag`, ale nie wysyła `Cache-Control`.
    Bez tego nagłówka przeglądarka cachuje heurystycznie — trzyma kopię i NIE pyta
    serwera, czy jest nowsza. Efekt: poprawka w CSS albo JS nie dociera do ekranu,
    dopóki ktoś nie zrobi twardego odświeżenia. W narzędziu, które się na bieżąco
    modyfikuje, to gubi godziny na ściganiu błędów naprawionych dawno temu.

    `no-cache` nie wyłącza cache — pozwala trzymać kopię, ale wymusza rewalidację.
    Przy niezmienionym pliku serwer odpowiada 304 bez treści, więc koszt jest zerowy.
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


# Frontend montowany na końcu, aby nie przechwytywał ścieżek /api/*.
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/", RewalidowaneStaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    reload = os.environ.get("RELOAD", "").strip().lower() in ("1", "true", "tak", "yes")

    if reload:
        # Przeladowanie wymaga sciezki importu, a nie obiektu aplikacji - uvicorn
        # importuje modul na nowo w podprocesie.
        #
        # Obserwujemy WYLACZNIE katalogi z kodem. Domyslnie uvicorn pilnowalby calego
        # drzewa razem z Data/, gdzie w trakcie przetwarzania powstaja pliki cache,
        # wyniki rozdzialow i podzialy - kazdy zapis restartowalby serwer w kolko,
        # przerywajac wlasnie trwajaca transkrypcje.
        uvicorn.run(
            "Interface.server:app",
            host=host, port=port, reload=True,
            # Katalog glowny projektu trafia na sys.path podprocesu. Bez tego import
            # "Interface.server" zalezalby od tego, z ktorego katalogu uruchomiono
            # polecenie, i przeladowanie wywracalo by sie z ModuleNotFoundError.
            app_dir=BASE_DIR,
            reload_dirs=[os.path.join(BASE_DIR, "Engine"),
                         os.path.dirname(os.path.abspath(__file__))],
        )
    else:
        uvicorn.run(app, host=host, port=port)
