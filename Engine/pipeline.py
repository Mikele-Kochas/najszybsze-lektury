import os
import re
import glob
import json
import threading
from dataclasses import asdict
from typing import List, Dict, Any, Optional, Callable

from .text_parser import Chapter, chapters_from_map, parse_book
from .chunker import create_chunks_for_chapter
from .transcriber import WhisperTranscriber, probe_duration
from .aligner import SequenceAligner
from .srt_writer import write_srt_file
from .chapter_matcher import ChapterMatcher
from .project import ProjectStore, Project, DEFAULT_SETTINGS
from .exporter import export_chapter_package, create_book_zip_package
from .layout import (
    Layout, LayoutError, LayoutStore, Token,
    tokenize_chapter, render_text, word_times_from_payload,
    split_board, merge_boards, move_break, set_cut_time, set_reviewed, validate_layout,
)
from .audio_analysis import AudioAnalysis, analyze_audio, resolve_audio_path
from .timing import compute_chapter_timing

ProgressCb = Optional[Callable[[float, str], None]]


def _noop(_progress: float, _message: str) -> None:
    pass


class PipelineManager:
    """
    Orkiestrator jednego aktywnego projektu.

    Przepływ: upload (.txt + N x .mp3) -> mapa rozdziałów (kreator) -> przetwarzanie
    rozdziałów (Whisper + alignment + plansze) -> eksport paczki.
    """

    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = base_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.data_dir = os.path.join(self.base_dir, "Data")
        self.store = ProjectStore(self.data_dir)

        self.text_dir = self.store.text_dir
        self.audio_dir = self.store.audio_dir
        self.processed_json_dir = self.store.processed_dir
        self.packages_dir = self.store.packages_dir
        self.output_srt_dir = os.path.join(self.data_dir, "Output_SRT")
        os.makedirs(self.output_srt_dir, exist_ok=True)
        self.audio_cache_dir = os.path.join(self.data_dir, "Cache_Audio_Analysis")
        self.layout_store = LayoutStore(self.data_dir)

        self._lock = threading.RLock()
        self._analysis_cache: Dict[int, AudioAnalysis] = {}
        self._timing_cache: Dict[int, Dict[str, Any]] = {}
        self._project: Optional[Project] = None
        self._transcriber: Optional[WhisperTranscriber] = None
        self._transcriber_signature: Optional[tuple] = None
        self._cached_chapters: Optional[List[Chapter]] = None
        self._cached_text: Optional[str] = None

        self.aligner = SequenceAligner()

    # ------------------------------------------------------------------
    # Projekt i ustawienia
    # ------------------------------------------------------------------

    @property
    def project(self) -> Project:
        with self._lock:
            if self._project is None:
                self._project = self.store.load()
            return self._project

    def save_project(self) -> Project:
        with self._lock:
            return self.store.save(self.project)

    def invalidate(self, drop_text: bool = True) -> None:
        """Zrzuca cache po zmianie źródeł lub mapy rozdziałów."""
        with self._lock:
            self._project = None
            self._cached_chapters = None
            self._timing_cache.clear()
            self._analysis_cache.clear()
            if drop_text:
                self._cached_text = None

    @property
    def settings(self) -> Dict[str, Any]:
        merged = dict(DEFAULT_SETTINGS)
        merged.update(self.project.settings or {})
        return merged

    def update_settings(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        allowed = set(DEFAULT_SETTINGS.keys())
        project = self.project
        for key, value in (changes or {}).items():
            if key in allowed and value is not None:
                project.settings[key] = value
        self.save_project()
        # Zmiana modelu/urządzenia wymusza rebuild transkrybera, a zmiana łamania
        # linii unieważnia podział na plansze.
        with self._lock:
            self._transcriber = None
            self._transcriber_signature = None
            self._cached_chapters = None
            self._timing_cache.clear()
        return self.settings

    @property
    def transcriber(self) -> WhisperTranscriber:
        s = self.settings
        signature = (s["model_size"], s["device"], s["compute_type"])
        with self._lock:
            if self._transcriber is None or self._transcriber_signature != signature:
                self._transcriber = WhisperTranscriber(
                    model_size=s["model_size"],
                    device=s["device"],
                    compute_type=s["compute_type"],
                    cache_dir=self.store.cache_dir,
                )
                self._transcriber_signature = signature
            return self._transcriber

    # ------------------------------------------------------------------
    # Źródła
    # ------------------------------------------------------------------

    def full_text(self) -> str:
        with self._lock:
            if self._cached_text is None:
                self._cached_text = self.store.read_source_text(self.project)
            return self._cached_text

    def get_audio_files(self) -> List[str]:
        project = self.project
        existing = [p for p in project.audio_files if os.path.exists(p)]
        if existing:
            return sorted(existing)
        return sorted(glob.glob(os.path.join(self.audio_dir, "*.mp3")))

    def find_audio_for_chapter(self, chapter_num: int) -> Optional[str]:
        """Ścieżka audio z zatwierdzonej mapy rozdziałów."""
        for entry in self.project.chapter_map:
            if int(entry.get("chapter_num", -1)) == chapter_num:
                path = entry.get("audio_path")
                if path and os.path.exists(path):
                    return path
                # Mapa mogła powstać przed przeniesieniem plików - dopasuj po nazwie.
                name = entry.get("audio_file")
                if name:
                    candidate = os.path.join(self.audio_dir, name)
                    if os.path.exists(candidate):
                        return candidate
                return None

        # Brak mapy: materiały skopiowane ręcznie do Data/ nadal mają działać,
        # więc dopasowujemy plik po numerze w nazwie, a w ostateczności po kolejności.
        return self._match_audio_by_filename(chapter_num)

    def _match_audio_by_filename(self, chapter_num: int) -> Optional[str]:
        audio_files = self.get_audio_files()
        if not audio_files:
            return None
        pattern = re.compile(rf'_{chapter_num:03d}_|rozdzial[-_]{chapter_num}\b', re.IGNORECASE)
        for path in audio_files:
            if pattern.search(os.path.basename(path)):
                return path
        if 1 <= chapter_num <= len(audio_files):
            return audio_files[chapter_num - 1]
        return None

    # ------------------------------------------------------------------
    # Mapa rozdziałów
    # ------------------------------------------------------------------

    def propose_chapter_map(self, progress_cb: ProgressCb = None) -> Dict[str, Any]:
        """Buduje propozycję podziału do zatwierdzenia w kreatorze (nie zapisuje jej)."""
        cb = progress_cb or _noop
        text = self.full_text()
        audio_files = self.get_audio_files()
        if not audio_files:
            raise FileNotFoundError("Brak plików .mp3 w projekcie. Wgraj nagrania rozdziałów.")

        cb(0.05, f"Analiza tekstu ({len(text)} znaków) i {len(audio_files)} nagrań...")
        matcher = ChapterMatcher(transcriber=self.transcriber)
        proposal = matcher.build_chapter_map(
            full_text=text,
            audio_files=audio_files,
            language=self.settings["language"],
            progress_cb=lambda p, m: cb(0.05 + 0.9 * p, m),
        )

        for entry in proposal["chapters"]:
            entry["audio_duration"] = probe_duration(entry["audio_path"]) if entry.get("audio_path") else 0.0
            entry["text_length"] = max(0, int(entry.get("text_end", 0)) - int(entry.get("text_start", 0)))

        proposal["audio_files"] = [
            {"file": os.path.basename(p), "path": p, "duration": probe_duration(p)}
            for p in audio_files
        ]
        cb(1.0, f"Zaproponowano {len(proposal['chapters'])} rozdziałów (metoda: {proposal['method']}).")
        return proposal

    def save_chapter_map(self, chapters: List[Dict[str, Any]]) -> Project:
        """Zapisuje mapę zatwierdzoną przez użytkownika i unieważnia wyniki pochodne."""
        text_len = len(self.full_text())
        audio_by_name = {os.path.basename(p): p for p in self.get_audio_files()}

        cleaned: List[Dict[str, Any]] = []
        for idx, entry in enumerate(chapters or [], start=1):
            start = max(0, min(text_len, int(entry.get("text_start", 0))))
            end = max(start, min(text_len, int(entry.get("text_end", text_len))))
            audio_file = entry.get("audio_file")
            audio_path = entry.get("audio_path") or audio_by_name.get(audio_file or "")
            cleaned.append({
                "chapter_num": int(entry.get("chapter_num", idx)),
                "header": (entry.get("header") or f"Rozdział {idx}").strip(),
                "title": (entry.get("title") or "").strip(),
                "text_start": start,
                "text_end": end,
                "audio_file": audio_file,
                "audio_path": audio_path,
                "confidence": float(entry.get("confidence", 1.0)),
                "source": entry.get("source", "manual"),
            })

        cleaned.sort(key=lambda c: c["text_start"])
        seen_numbers = set()
        for idx, entry in enumerate(cleaned, start=1):
            if entry["chapter_num"] in seen_numbers:
                entry["chapter_num"] = idx
            seen_numbers.add(entry["chapter_num"])

        project = self.project
        project.chapter_map = cleaned
        project.status["mapped"] = bool(cleaned)
        project.status["processed"] = False
        self.save_project()
        self.store.clear_derived()
        with self._lock:
            self._cached_chapters = None
        return project

    def get_chapters(self) -> List[Chapter]:
        """Rozdziały wg zatwierdzonej mapy; awaryjnie stary parser nagłówków."""
        with self._lock:
            if self._cached_chapters is not None:
                return self._cached_chapters

        s = self.settings
        project = self.project
        if project.chapter_map:
            chapters = chapters_from_map(self.full_text(), project.chapter_map, s["max_chars_per_line"])
        else:
            if not project.text_file or not os.path.exists(project.text_file):
                raise FileNotFoundError("Brak pliku .txt w projekcie. Wgraj tekst książki.")
            chapters = parse_book(project.text_file, s["max_chars_per_line"])

        with self._lock:
            self._cached_chapters = chapters
        return chapters

    # ------------------------------------------------------------------
    # Przetwarzanie
    # ------------------------------------------------------------------

    def chapter_json_path(self, chapter_num: int) -> str:
        return os.path.join(self.processed_json_dir, f"chapter_{chapter_num:03d}.json")

    def is_processed(self, chapter_num: int) -> bool:
        return os.path.exists(self.chapter_json_path(chapter_num))

    def process_chapter(
        self,
        chapter_num: int,
        use_cache: bool = True,
        save_json: bool = True,
        progress_cb: ProgressCb = None,
    ) -> Dict[str, Any]:
        """Przetwarza jeden rozdział: Whisper -> alignment -> plansze -> SRT."""
        cb = progress_cb or _noop
        s = self.settings

        chapters = self.get_chapters()
        target_ch = next((c for c in chapters if c.number == chapter_num), None)
        if not target_ch:
            available = ", ".join(str(c.number) for c in chapters) or "brak"
            raise ValueError(f"Nie znaleziono rozdziału {chapter_num} (dostępne: {available}).")

        audio_file = self.find_audio_for_chapter(chapter_num)
        if not audio_file or not os.path.exists(audio_file):
            raise FileNotFoundError(
                f"Rozdział {chapter_num} nie ma przypisanego pliku audio. Popraw mapowanie w kreatorze."
            )

        print(f"[Pipeline] Rozdział {chapter_num}: {target_ch.header} <- {os.path.basename(audio_file)}")

        cb(0.05, f"Rozdział {chapter_num}: podział na plansze...")
        initial_chunks = create_chunks_for_chapter(
            target_ch.blocks,
            max_lines=s["max_lines_per_board"],
            max_chars_per_line=s["max_chars_per_line"],
        )

        trans_res = self.transcriber.transcribe(
            audio_file,
            language=s["language"],
            use_cache=use_cache,
            progress_cb=lambda p, m: cb(0.05 + 0.75 * p, m),
        )

        cb(0.85, f"Rozdział {chapter_num}: dopasowanie {len(initial_chunks)} plansz do nagrania...")
        aligned_chunks, report, word_times = self.aligner.align_chapter(
            chunks=initial_chunks, transcription=trans_res
        )

        base_audio_name = os.path.splitext(os.path.basename(audio_file))[0]
        srt_path = os.path.join(self.output_srt_dir, f"{base_audio_name}.srt")
        write_srt_file(aligned_chunks, srt_path)

        payload = {
            "chapter_num": chapter_num,
            "chapter_title": target_ch.title,
            "chapter_header": target_ch.header,
            "audio_file": os.path.basename(audio_file),
            "audio_path": audio_file,
            "srt_path": srt_path,
            "duration": trans_res.duration,
            "report": asdict(report),
            "chunks": [
                {
                    "chunk_id": c.chunk_id,
                    "text": c.text,
                    "chunk_type": c.chunk_type,
                    "lines_count": c.lines_count,
                    "start_time": c.start_time,
                    "end_time": c.end_time,
                    "duration": round(c.end_time - c.start_time, 3),
                }
                for c in aligned_chunks
            ],
            "whisper_segments": [
                {"id": s_.id, "start": s_.start, "end": s_.end, "text": s_.text}
                for s_ in trans_res.segments
            ],
            # Czasy słów książki w kolejności czytania — podstawa pod dowolny podział
            # na plansze. Klucze są jednoliterowe, bo tej listy są tysiące pozycji
            # na rozdział i pełne nazwy potrajałyby rozmiar pliku.
            "words": [
                {"w": aw.original_word, "s": aw.start, "e": aw.end, "m": aw.matched}
                for aw in word_times
            ],
            # Wtrącenia lektora nie mają odpowiednika w tekście, więc nie mieszczą się
            # w liście słów; bez osobnego zapisu ginęłyby przy odtwarzaniu plansz.
            "extras": [
                {
                    "position": "intro" if idx == 0 else "outro",
                    "start_time": c.start_time,
                    "end_time": c.end_time,
                }
                for idx, c in enumerate(aligned_chunks)
                if c.chunk_type == "intro_outro"
            ],
        }

        if save_json:
            with open(self.chapter_json_path(chapter_num), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            # Nowe czasy słów unieważniają policzone wcześniej cięcia.
            self._forget_timing(chapter_num)

        cb(1.0, f"Rozdział {chapter_num} gotowy — trafność {report.match_percentage}%.")
        print(f"[Pipeline] Trafność {report.match_percentage}% ({report.matched_words}/{report.total_book_words} słów)")
        return payload

    def process_chapters(
        self,
        chapter_nums: Optional[List[int]] = None,
        use_cache: bool = True,
        progress_cb: ProgressCb = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """Przetwarza wiele rozdziałów sekwencyjnie, raportując łączny postęp."""
        cb = progress_cb or _noop
        chapters = self.get_chapters()
        targets = chapter_nums or [c.number for c in chapters]
        total = len(targets)
        done: List[int] = []
        failed: List[Dict[str, Any]] = []

        for idx, num in enumerate(targets):
            if should_cancel and should_cancel():
                break
            base = idx / max(1, total)
            span = 1.0 / max(1, total)
            try:
                self.process_chapter(
                    num,
                    use_cache=use_cache,
                    progress_cb=lambda p, m, _b=base, _s=span: cb(_b + _s * p, m),
                )
                done.append(num)
            except Exception as exc:
                # Anulowanie sygnalizowane jest wyjątkiem z callbacku postępu.
                # Bez tego zostałoby zapisane jako błąd rozdziału, a zadanie
                # dokończyłoby pozostałe pozycje mimo żądania przerwania.
                if should_cancel and should_cancel():
                    raise
                message = f"Rozdział {num}: {type(exc).__name__}: {exc}"
                print(f"[Pipeline] {message}")
                failed.append({"chapter_num": num, "error": str(exc)})
                cb(base + span, message)

        project = self.project
        project.status["processed"] = bool(done)
        self.save_project()

        # Gdy nie udał się ani jeden rozdział, zadanie musi zgłosić błąd.
        # Status "zakończone" przy zerowym wyniku wygląda w interfejsie jak sukces,
        # a użytkownik ogląda wtedy dane z poprzedniego przebiegu.
        if failed and not done:
            raise RuntimeError(
                "Nie udało się przetworzyć żadnego rozdziału. "
                + "; ".join(f"rozdz. {f['chapter_num']}: {f['error']}" for f in failed[:3])
            )

        cb(1.0, f"Przetworzono {len(done)}/{total} rozdziałów.")
        return {"processed": done, "failed": failed, "total": total}

    # ------------------------------------------------------------------
    # Granice plansz i punkty cięcia
    # ------------------------------------------------------------------

    def chapter_by_num(self, chapter_num: int) -> Chapter:
        chapter = next((c for c in self.get_chapters() if c.number == chapter_num), None)
        if not chapter:
            available = ", ".join(str(c.number) for c in self.get_chapters()) or "brak"
            raise ValueError(f"Nie znaleziono rozdziału {chapter_num} (dostępne: {available}).")
        return chapter

    def chapter_payload(self, chapter_num: int) -> Dict[str, Any]:
        path = self.chapter_json_path(chapter_num)
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"Rozdział {chapter_num} nie został jeszcze przetworzony."
            )
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def chapter_analysis(self, chapter_num: int) -> AudioAnalysis:
        """Obwiednia, cisze i peaki nagrania. Trzymane w pamięci — dekodowanie jest drogie."""
        with self._lock:
            cached = self._analysis_cache.get(chapter_num)
        if cached is not None:
            return cached

        payload = self.chapter_payload(chapter_num)
        audio_path = resolve_audio_path(payload, self.audio_dir)
        if not audio_path:
            raise FileNotFoundError(
                f"Nie znaleziono nagrania rozdziału {chapter_num} ({payload.get('audio_file')})."
            )
        analysis = analyze_audio(audio_path, cache_dir=self.audio_cache_dir)
        with self._lock:
            self._analysis_cache[chapter_num] = analysis
        return analysis

    def load_layout(self, chapter_num: int) -> tuple:
        """Zwraca (layout, tokens, chapter). Brak zapisanego podziału => propozycja chunkera."""
        s = self.settings
        chapter = self.chapter_by_num(chapter_num)
        layout, tokens, fresh = self.layout_store.load_or_propose(
            chapter,
            max_lines=s["max_lines_per_board"],
            max_chars_per_line=s["max_chars_per_line"],
        )
        return layout, tokens, chapter, fresh

    def chapter_timing(self, chapter_num: int) -> Dict[str, Any]:
        """Cięcia i plansze dla zatwierdzonego podziału. Wynik trzymany w pamięci."""
        with self._lock:
            cached = self._timing_cache.get(chapter_num)
        if cached is not None:
            return cached

        s = self.settings
        layout, tokens, chapter, _fresh = self.load_layout(chapter_num)
        result = compute_chapter_timing(
            chapter,
            self.chapter_payload(chapter_num),
            self.chapter_analysis(chapter_num),
            layout, tokens,
            s["max_lines_per_board"], s["max_chars_per_line"],
        )
        with self._lock:
            self._timing_cache[chapter_num] = result
        return result

    def layout_state(self, chapter_num: int) -> Dict[str, Any]:
        """
        Komplet danych dla ekranu granic: słowa, granice i ostrzeżenia.

        Flaga „lektor nie robi tu przerwy" pochodzi z tego samego wyliczenia co ekran
        cięć — jedna definicja dla obu widoków, żeby nie pokazywały różnych liczb.
        """
        s = self.settings
        layout, tokens, chapter, fresh = self.load_layout(chapter_num)

        flags: Dict[str, str] = {}
        try:
            for cut in self.chapter_timing(chapter_num)["cuts"]:
                if cut.no_pause:
                    flags[str(cut.token)] = cut.segment_text
        except (FileNotFoundError, LayoutError) as exc:
            print(f"[Pipeline] Rozdz. {chapter_num}: brak flag braku pauzy ({exc}).")

        return {
            "chapter_num": chapter_num,
            "header": chapter.header,
            "title": chapter.title,
            "saved": not fresh,
            "text_hash": layout.text_hash,
            "token_count": layout.token_count,
            "tokens": [[t.word, t.sep] for t in tokens],
            "breaks": list(layout.breaks),
            "overrides": {str(k): v for k, v in layout.overrides.items()},
            "reviewed": sorted(layout.reviewed),
            "flags": flags,
            "max_lines": s["max_lines_per_board"],
            "max_chars": s["max_chars_per_line"],
            "warnings": validate_layout(layout, tokens,
                                        s["max_lines_per_board"], s["max_chars_per_line"]),
        }

    def layout_operation(self, chapter_num: int, op: str, **kwargs: Any) -> Dict[str, Any]:
        """Wykonuje jedną operację na podziale i zapisuje wynik."""
        s = self.settings
        layout, tokens, chapter, _fresh = self.load_layout(chapter_num)

        if op == "split":
            split_board(layout, int(kwargs["board"]), int(kwargs["token"]))
        elif op == "merge":
            merge_boards(layout, int(kwargs["board"]))
        elif op == "move":
            move_break(layout, int(kwargs["boundary"]), int(kwargs["token"]))
        elif op == "cut_time":
            set_cut_time(layout, int(kwargs["token"]), kwargs.get("time"))
        elif op == "reviewed":
            set_reviewed(layout, int(kwargs["token"]), bool(kwargs.get("flag", True)))
        elif op == "reset":
            self.layout_store.delete(chapter_num)
            self._forget_timing(chapter_num)
            return self.layout_state(chapter_num)
        else:
            raise LayoutError(f"Nieznana operacja podziału: {op}.")

        self.layout_store.save(layout)
        self._forget_timing(chapter_num)
        return self.layout_state(chapter_num)

    def _forget_timing(self, chapter_num: int) -> None:
        """Podział się zmienił — plansze i cięcia trzeba policzyć od nowa."""
        with self._lock:
            self._timing_cache.pop(chapter_num, None)

    def cuts_state(self, chapter_num: int) -> Dict[str, Any]:
        """Komplet danych dla ekranu cięć: cięcia, plansze, cisze i wypowiedzi lektora."""
        result = self.chapter_timing(chapter_num)
        payload = self.chapter_payload(chapter_num)
        analysis = self.chapter_analysis(chapter_num)
        layout, _tokens, _chapter, _fresh = self.load_layout(chapter_num)
        reviewed = set(layout.reviewed)

        return {
            "chapter_num": chapter_num,
            "header": result["header"],
            "audio_file": result["audio_file"],
            "duration": round(analysis.envelope.duration, 3),
            "cuts": [dict(c.to_dict(), reviewed=c.token in reviewed) for c in result["cuts"]],
            "boards": [
                {
                    "id": b["chunk_id"], "text": b["text"], "type": b["chunk_type"],
                    "lines": b["lines_count"], "token_start": b["token_start"],
                    "token_end": b["token_end"],
                }
                for b in result["boards"]
            ],
            "silences": [[round(s.start, 3), round(s.end, 3)] for s in analysis.silences],
            "segments": [
                {"s": round(g["start"], 3), "e": round(g["end"], 3), "t": (g.get("text") or "").strip()}
                for g in payload.get("whisper_segments", [])
            ],
            "words": [[round(w["s"], 3), round(w["e"], 3)] for w in payload.get("words", [])],
            "summary": result["summary"],
        }

    def peaks_payload(self, chapter_num: int) -> Dict[str, Any]:
        """Fala do rysowania: min/max w kubełkach, zakodowane base64."""
        import base64

        analysis = self.chapter_analysis(chapter_num)
        return {
            "chapter_num": chapter_num,
            "duration": round(analysis.envelope.duration, 3),
            "rate": analysis.buckets_per_sec,
            "min": base64.b64encode(analysis.peaks_min.tobytes()).decode("ascii"),
            "max": base64.b64encode(analysis.peaks_max.tobytes()).decode("ascii"),
        }

    def layout_overview(self) -> List[Dict[str, Any]]:
        """Stan wszystkich rozdziałów dla nagłówków obu ekranów."""
        out: List[Dict[str, Any]] = []
        for chapter in self.get_chapters():
            entry = {"chapter_num": chapter.number, "header": chapter.header,
                     "title": chapter.title, "processed": self.is_processed(chapter.number),
                     "saved_layout": os.path.exists(self.layout_store.path(chapter.number)),
                     "boards": None, "attention": None, "error": None}
            if entry["processed"]:
                try:
                    summary = self.chapter_timing(chapter.number)["summary"]
                    layout, _t, _c, _f = self.load_layout(chapter.number)
                    entry["boards"] = summary["boards"]
                    entry["attention"] = summary["attention"]
                    entry["reviewed"] = len(layout.reviewed)
                except Exception as exc:  # rozdział nie może wywrócić listy pozostałych
                    entry["error"] = f"{type(exc).__name__}: {exc}"
            out.append(entry)
        return out

    def load_processed_chapters(self) -> List[Dict[str, Any]]:
        """Wczytuje wszystkie zapisane wyniki rozdziałów, posortowane po numerze."""
        payloads: List[Dict[str, Any]] = []
        for filename in sorted(os.listdir(self.processed_json_dir)):
            if filename.startswith("chapter_") and filename.endswith(".json"):
                path = os.path.join(self.processed_json_dir, filename)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        payload = json.load(f)
                except (json.JSONDecodeError, OSError) as e:
                    print(f"[Pipeline] Pomijam uszkodzony plik {filename}: {e}")
                    continue

                # Wyniki mogły powstać na innym komputerze albo poza kontenerem,
                # więc zapisana ścieżka audio bywa nieaktualna - odtwarzamy ją po nazwie.
                audio_path = payload.get("audio_path")
                if not audio_path or not os.path.exists(audio_path):
                    name = payload.get("audio_file") or (
                        os.path.basename(audio_path) if audio_path else None
                    )
                    if name:
                        payload["audio_path"] = os.path.join(self.audio_dir, name)
                payloads.append(payload)
        payloads.sort(key=lambda p: p.get("chapter_num", 0))
        return payloads

    # ------------------------------------------------------------------
    # Eksport
    # ------------------------------------------------------------------

    def export_chapters(self) -> List[Dict[str, Any]]:
        """
        Rozdziały gotowe do eksportu, z planszami wg zatwierdzonych granic i cięć.

        Gdy rozdziału nie da się przeliczyć (brak nagrania, podział do innej wersji
        tekstu), wracamy do plansz zapisanych przez pipeline. Lepiej wyeksportować
        paczkę ze starszymi czasami niż przerwać eksport całej książki.
        """
        out: List[Dict[str, Any]] = []
        for payload in self.load_processed_chapters():
            num = int(payload.get("chapter_num", 0))
            enriched = dict(payload)
            try:
                enriched["chunks"] = self.chapter_timing(num)["chunks"]
                enriched["cut_source"] = "layout"
            except Exception as exc:
                enriched["cut_source"] = "legacy"
                print(f"[Pipeline] Rozdz. {num}: eksport z zapisanych plansz "
                      f"({type(exc).__name__}: {exc}).")
            out.append(enriched)
        return out

    def export_package(
        self,
        book_name: Optional[str] = None,
        slice_audio: bool = True,
        progress_cb: ProgressCb = None,
    ) -> Dict[str, Any]:
        cb = progress_cb or _noop
        processed = self.export_chapters()
        if not processed:
            raise ValueError("Żaden rozdział nie został jeszcze przetworzony.")

        title = book_name or self.project.title or "Ksiazka"
        cb(0.02, f"Eksport paczki „{title}” ({len(processed)} rozdz., cięcie audio: {'tak' if slice_audio else 'nie'})...")

        zip_path = create_book_zip_package(
            book_name=title,
            packages_base_dir=self.packages_dir,
            processed_chapters=processed,
            slice_audio=slice_audio,
            progress_cb=lambda p, m: cb(0.02 + 0.97 * p, m),
        )

        project = self.project
        project.status["exported"] = True
        self.save_project()

        cb(1.0, f"Paczka gotowa: {os.path.basename(zip_path)}")
        return {
            "zip_path": zip_path,
            "zip_name": os.path.basename(zip_path),
            "size_bytes": os.path.getsize(zip_path) if os.path.exists(zip_path) else 0,
            "chapters": len(processed),
        }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generowanie paczek lektur z audio i tekstu książki.")
    parser.add_argument("--chapter", type=int, help="Numer rozdziału do przetworzenia")
    parser.add_argument("--all", action="store_true", help="Przetwórz wszystkie rozdziały")
    parser.add_argument("--map", action="store_true", help="Wykryj i zapisz mapę rozdziałów")
    parser.add_argument("--export", action="store_true", help="Zbuduj paczkę ZIP")
    parser.add_argument("--no-audio", action="store_true", help="Eksport bez cięcia MP3")
    parser.add_argument("--no-cache", action="store_true", help="Pomiń cache transkrypcji")
    parser.add_argument("--model", type=str, help="Rozmiar modelu Whisper (small, medium, large-v3)")
    args = parser.parse_args()

    pipeline = PipelineManager()
    if args.model:
        pipeline.update_settings({"model_size": args.model})

    log = lambda p, m: print(f"  [{p * 100:5.1f}%] {m}")

    if args.map:
        proposal = pipeline.propose_chapter_map(progress_cb=log)
        pipeline.save_chapter_map(proposal["chapters"])
        for warning in proposal["warnings"]:
            print(f"  ! {warning}")
        print(f"Zapisano mapę: {len(proposal['chapters'])} rozdziałów (metoda: {proposal['method']}).")

    if args.all:
        pipeline.process_chapters(use_cache=not args.no_cache, progress_cb=log)
    elif args.chapter:
        pipeline.process_chapter(args.chapter, use_cache=not args.no_cache, progress_cb=log)

    if args.export:
        result = pipeline.export_package(slice_audio=not args.no_audio, progress_cb=log)
        print(f"ZIP: {result['zip_path']} ({result['size_bytes'] / 1_048_576:.1f} MB)")
