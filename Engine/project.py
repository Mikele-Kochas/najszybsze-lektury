"""
Zarzadzanie pojedynczym aktywnym projektem (jedna ksiazka naraz).

Projekt = katalog Data/ zawierajacy zrodlowy .txt, zrodlowe .mp3 oraz
manifest project.json opisujacy stan przetwarzania.
"""
import os
import re
import json
import shutil
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple

MANIFEST_NAME = "project.json"

DEFAULT_SETTINGS = {
    "model_size": "small",
    "device": "auto",
    "compute_type": "auto",
    "language": "pl",
    "max_lines_per_board": 11,
    "max_chars_per_line": 45,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Kodowania spotykane w polskich plikach .txt, w kolejnosci prawdopodobienstwa.
TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "cp1250", "iso-8859-2", "cp852")
POLISH_CHARS = set("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")


def decode_text_file(path: str) -> str:
    """
    Odczytuje plik tekstowy probujac kolejnych kodowan. Samo powodzenie dekodowania
    nie wystarcza - cp1250 i iso-8859-2 zdekoduja niemal kazdy bajt, wiec przy
    niepewnosci wybieramy wariant z najwieksza liczba poprawnych polskich znakow.
    """
    with open(path, "rb") as f:
        raw = f.read()

    best_text: Optional[str] = None
    best_score = -1.0

    for encoding in TEXT_ENCODINGS:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue

        # UTF-8 jest samowalidujace: gdy zdekodowalo sie bez bledu, to jest wlasciwe kodowanie.
        if encoding in ("utf-8-sig", "utf-8"):
            return normalize_newlines(text)

        polish = sum(1 for ch in text if ch in POLISH_CHARS)
        replacement = text.count("�")
        score = polish - replacement * 10
        if score > best_score:
            best_score, best_text = score, text

    if best_text is None:
        best_text = raw.decode("utf-8", errors="replace")
    return normalize_newlines(best_text)


# Znaczniki stopki wydawniczej. Lektor ich nie czyta, wiec gdyby zostaly w tekscie,
# trafialyby jako plansze bez pokrycia w audio - z bledna trafnoscia dopasowania,
# znacznikami czasu poza koncem nagrania i smieciowymi plikami mp3 w paczce.
BOILERPLATE_MARKERS = (
    "\n-----",
    "ta lektura, podobnie jak tysi",
    "wszystkie zasoby wolnych lektur",
    "tekst opracowany na podstawie:",
    "ten utwór jest udostępniony na licencji",
    "wolnelektury.pl/info/zasady-wykorzystania",
)

# Stopki szukamy tylko w koncowce dokumentu - w srodku ksiazki linia myslnikow
# bywa zwyklym separatorem scen i nie wolno na niej uciac tresci.
BOILERPLATE_TAIL_FRACTION = 0.85


def strip_publisher_boilerplate(text: str) -> Tuple[str, int]:
    """
    Odcina stopke wydawnicza z konca pliku. Zwraca (tekst, liczba_odcietych_znakow).
    """
    if not text:
        return text, 0

    search_from = int(len(text) * BOILERPLATE_TAIL_FRACTION)
    lowered = text.lower()

    cut_at = len(text)
    for marker in BOILERPLATE_MARKERS:
        found = lowered.find(marker, search_from)
        if found != -1:
            cut_at = min(cut_at, found)

    if cut_at >= len(text):
        return text, 0

    trimmed = text[:cut_at].rstrip()
    return trimmed, len(text) - len(trimmed)


def normalize_newlines(text: str) -> str:
    """
    Ujednolica konce linii do \\n. Bez tego znaki \\r zostaja w tytulach rozdzialow
    i w tresci plansz, a stamtad trafiaja do plikow SRT i etykiet Audacity.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


# Znaki niedozwolone w nazwach plikow na Windows, plus separatory sciezek.
# Zapisane jako zbior, a nie klasa znakow w regexie - w regexie backslash latwo
# zgubic przy edycji, a wtedy tytul z "\" utworzylby zagniezdzone katalogi.
ILLEGAL_NAME_CHARS = set('\\/:*?"<>|\r\n\t')


def safe_component(text: str, fallback: str = "projekt") -> str:
    """Czysci nazwe tak, by nadawala sie na nazwe pliku/katalogu na Windows i Linux."""
    cleaned = unicodedata.normalize("NFC", (text or "").strip())
    cleaned = "".join(ch for ch in cleaned if ch not in ILLEGAL_NAME_CHARS)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or fallback


@dataclass
class ProjectStatus:
    uploaded: bool = False
    transcribed: bool = False
    mapped: bool = False
    processed: bool = False
    exported: bool = False


@dataclass
class Project:
    title: str = ""
    author: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    text_file: Optional[str] = None
    audio_files: List[str] = field(default_factory=list)
    audio_mode: str = "multi"           # 'multi' = jeden mp3 na rozdzial (domyslnie), 'single' = jeden plik na calosc
    settings: Dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    status: Dict[str, bool] = field(default_factory=lambda: asdict(ProjectStatus()))
    transcript: Dict[str, Any] = field(default_factory=dict)
    chapter_map: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Project":
        settings = dict(DEFAULT_SETTINGS)
        settings.update(data.get("settings") or {})
        status = asdict(ProjectStatus())
        status.update(data.get("status") or {})
        return cls(
            title=data.get("title", ""),
            author=data.get("author", ""),
            created_at=data.get("created_at", utc_now()),
            updated_at=data.get("updated_at", utc_now()),
            text_file=data.get("text_file"),
            audio_files=list(data.get("audio_files") or []),
            audio_mode=data.get("audio_mode", "multi"),
            settings=settings,
            status=status,
            transcript=data.get("transcript") or {},
            chapter_map=list(data.get("chapter_map") or []),
        )


class ProjectStore:
    """Odczyt/zapis manifestu oraz operacje na plikach zrodlowych projektu."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.text_dir = os.path.join(data_dir, "Text")
        self.audio_dir = os.path.join(data_dir, "Audio")
        self.cache_dir = os.path.join(data_dir, "Cache_Transcripts")
        self.processed_dir = os.path.join(data_dir, "Processed_JSON")
        self.packages_dir = os.path.join(data_dir, "Output_Packages")
        self.archive_dir = os.path.join(data_dir, "Poprzedni_projekt")
        self.manifest_path = os.path.join(data_dir, MANIFEST_NAME)
        for d in (self.text_dir, self.audio_dir, self.cache_dir,
                  self.processed_dir, self.packages_dir):
            os.makedirs(d, exist_ok=True)

    # ---------- manifest ----------

    def load(self) -> Project:
        if os.path.exists(self.manifest_path):
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    return self._rebase_paths(Project.from_dict(json.load(f)))
            except (json.JSONDecodeError, OSError) as e:
                print(f"[ProjectStore] Uszkodzony manifest ({e}), odtwarzam ze stanu katalogow.")
        return self._infer_from_disk()

    def save(self, project: Project) -> Project:
        project.updated_at = utc_now()
        data = project.to_dict()

        # W manifescie zapisujemy same nazwy plikow. Sciezki bezwzgledne nie przetrwaly by
        # przeniesienia projektu na inny komputer ani montowania katalogu Data pod inna
        # sciezka w kontenerze - a katalogi Text/ i Audio/ i tak sa stale.
        if data.get("text_file"):
            data["text_file"] = os.path.basename(data["text_file"])
        data["audio_files"] = [os.path.basename(p) for p in data.get("audio_files") or []]
        for entry in data.get("chapter_map") or []:
            entry.pop("audio_path", None)

        tmp_path = self.manifest_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.manifest_path)
        return project

    def _rebase_paths(self, project: Project) -> Project:
        """
        Odtwarza pelne sciezki wzgledem biezacego katalogu Data.
        Obsluguje tez stare manifesty ze sciezkami bezwzglednymi - liczy sie tylko nazwa pliku.
        """
        if project.text_file:
            project.text_file = os.path.join(self.text_dir, os.path.basename(project.text_file))
        project.audio_files = [
            os.path.join(self.audio_dir, os.path.basename(p)) for p in project.audio_files
        ]
        for entry in project.chapter_map:
            name = entry.get("audio_file") or (
                os.path.basename(entry["audio_path"]) if entry.get("audio_path") else None
            )
            entry["audio_path"] = os.path.join(self.audio_dir, name) if name else None
        return project

    def _infer_from_disk(self) -> Project:
        """Tworzy manifest dla danych wgranych recznie do Data/ (kompatybilnosc wsteczna)."""
        texts = sorted(f for f in os.listdir(self.text_dir) if f.lower().endswith(".txt"))
        audios = sorted(f for f in os.listdir(self.audio_dir) if f.lower().endswith(".mp3"))
        project = Project()
        if texts:
            project.text_file = os.path.join(self.text_dir, texts[0])
            project.title = os.path.splitext(texts[0])[0]
        if audios:
            project.audio_files = [os.path.join(self.audio_dir, a) for a in audios]
            project.audio_mode = "multi" if len(audios) > 1 else "single"
        project.status["uploaded"] = bool(texts and audios)
        if texts and audios:
            self.save(project)
        return project

    # ---------- pliki zrodlowe ----------

    def clear_sources(self, archive: bool = True) -> None:
        """
        Przygotowuje projekt na nowe materialy.

        Pliki zrodlowe (.txt i audio) sa PRZENOSZONE do Data/Poprzedni_projekt/,
        a nie kasowane - wgranie nowej ksiazki nie moze bezpowrotnie niszczyc
        materialow, ktorych uzytkownik moze nie miec nigdzie indziej.
        Trzymamy jeden poziom cofniecia; wyniki pochodne (cache, JSON) sa usuwane,
        bo odtwarza je ponowne przetworzenie.
        """
        if archive and (self._has_files(self.text_dir) or self._has_files(self.audio_dir)):
            if os.path.isdir(self.archive_dir):
                shutil.rmtree(self.archive_dir, ignore_errors=True)
            os.makedirs(self.archive_dir, exist_ok=True)
            for directory in (self.text_dir, self.audio_dir):
                if os.path.isdir(directory):
                    shutil.move(directory, os.path.join(self.archive_dir, os.path.basename(directory)))
            if os.path.exists(self.manifest_path):
                shutil.copy2(self.manifest_path, os.path.join(self.archive_dir, MANIFEST_NAME))
            print(f"[ProjectStore] Poprzednie zrodla przeniesiono do {self.archive_dir}")
        else:
            for directory in (self.text_dir, self.audio_dir):
                if os.path.isdir(directory):
                    shutil.rmtree(directory, ignore_errors=True)

        for directory in (self.cache_dir, self.processed_dir):
            if os.path.isdir(directory):
                shutil.rmtree(directory, ignore_errors=True)

        for directory in (self.text_dir, self.audio_dir, self.cache_dir, self.processed_dir):
            os.makedirs(directory, exist_ok=True)

    @staticmethod
    def _has_files(directory: str) -> bool:
        return os.path.isdir(directory) and any(os.scandir(directory))

    def archived_project(self) -> Optional[Dict[str, Any]]:
        """Opis zarchiwizowanego projektu, jesli jakis czeka na przywrocenie."""
        if not self._has_files(os.path.join(self.archive_dir, "Text")):
            return None
        manifest = os.path.join(self.archive_dir, MANIFEST_NAME)
        title = ""
        if os.path.exists(manifest):
            try:
                with open(manifest, "r", encoding="utf-8") as f:
                    title = json.load(f).get("title", "")
            except (json.JSONDecodeError, OSError):
                pass
        audio_dir = os.path.join(self.archive_dir, "Audio")
        return {
            "title": title,
            "path": self.archive_dir,
            "audio_count": len(os.listdir(audio_dir)) if os.path.isdir(audio_dir) else 0,
        }

    def clear_derived(self) -> None:
        """Usuwa wylacznie wyniki przetwarzania, zostawiajac zrodla i cache transkrypcji."""
        if os.path.isdir(self.processed_dir):
            shutil.rmtree(self.processed_dir, ignore_errors=True)
        os.makedirs(self.processed_dir, exist_ok=True)

    @staticmethod
    def _write_stream(path: str, source) -> int:
        """Zapisuje strumien na dysk bez wciagania calego pliku do pamieci."""
        written = 0
        with open(path, "wb") as out:
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                out.write(block)
                written += len(block)
        return written

    def store_text(self, filename: str, source) -> str:
        name = safe_component(os.path.basename(filename), "ksiazka.txt")
        if not name.lower().endswith(".txt"):
            name += ".txt"
        path = os.path.join(self.text_dir, name)
        self._write_stream(path, source)
        return path

    def store_audio(self, filename: str, source) -> str:
        name = safe_component(os.path.basename(filename), "audio.mp3")
        if not os.path.splitext(name)[1]:
            name += ".mp3"
        path = os.path.join(self.audio_dir, name)
        self._write_stream(path, source)
        return path

    def read_source_text(self, project: Project) -> str:
        """
        Tekst zrodlowy po dekodowaniu i odcieciu stopki wydawniczej.

        To jedyne miejsce wczytywania tekstu, wiec wszystkie indeksy znakowe
        w mapie rozdzialow odnosza sie do tej samej, przycietej wersji.
        """
        text, _removed = self.read_source_text_with_info(project)
        return text

    def read_source_text_with_info(self, project: Project) -> Tuple[str, int]:
        if not project.text_file or not os.path.exists(project.text_file):
            raise FileNotFoundError("Brak pliku tekstowego w projekcie. Wgraj plik .txt.")
        text, removed = strip_publisher_boilerplate(decode_text_file(project.text_file))
        if removed:
            print(f"[ProjectStore] Odcieto {removed} znakow stopki wydawniczej.")
        return text, removed
