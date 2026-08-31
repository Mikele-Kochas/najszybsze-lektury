import os
import json
import hashlib
import tempfile
import subprocess
from dataclasses import dataclass, asdict
from typing import Callable, List, Dict, Any, Optional
from faster_whisper import WhisperModel

ProgressCb = Optional[Callable[[float, str], None]]


@dataclass
class WordTimestamp:
    word: str
    start: float
    end: float
    probability: float


@dataclass
class TranscriptionSegment:
    id: int
    seek: int
    start: float
    end: float
    text: str
    words: List[WordTimestamp]


@dataclass
class TranscriptionResult:
    audio_path: str
    duration: float
    language: str
    segments: List[TranscriptionSegment]
    all_words: List[WordTimestamp]


def probe_duration(audio_path: str) -> float:
    """Zwraca długość pliku audio w sekundach (ffprobe). 0.0 gdy nie da się odczytać."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
            capture_output=True, text=True, check=True,
        )
        return round(float(out.stdout.strip()), 3)
    except (subprocess.SubprocessError, ValueError, FileNotFoundError):
        return 0.0


# Przybliżone zużycie VRAM przez modele Whisper w float16. Służy ostrzeganiu w interfejsie -
# przy zbyt małej karcie ctranslate2 przerywa ładowanie, a aplikacja schodzi na CPU,
# co bez ostrzeżenia wygląda po prostu jak bardzo wolne przetwarzanie.
MODEL_VRAM_GB = {
    "tiny": 0.5, "base": 0.7, "small": 1.0, "medium": 2.5,
    "large-v1": 4.7, "large-v2": 4.7, "large-v3": 4.7, "large": 4.7,
}


def query_gpu() -> Dict[str, Any]:
    """Nazwa karty i pamięć w MB wg nvidia-smi. Pusty słownik, gdy narzędzia brak."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True, timeout=15,
        )
        first = out.stdout.strip().splitlines()[0]
        name, memory = (part.strip() for part in first.split(",", 1))
        return {"gpu_name": name, "vram_mb": int(float(memory))}
    except (subprocess.SubprocessError, FileNotFoundError, ValueError, IndexError):
        return {}


def detect_device(requested_device: str = "auto", requested_compute: str = "auto") -> tuple:
    """Ustala parę (device, compute_type). 'auto' wybiera CUDA gdy dostępna, inaczej CPU."""
    if requested_device and requested_device != "auto":
        compute = requested_compute if requested_compute and requested_compute != "auto" else (
            "float16" if requested_device == "cuda" else "int8"
        )
        return requested_device, compute

    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", ("float16" if requested_compute == "auto" else requested_compute)
    except Exception:
        pass
    return "cpu", ("int8" if requested_compute == "auto" else requested_compute)


class WhisperTranscriber:
    def __init__(
        self,
        model_size: str = "small",
        device: str = "auto",
        compute_type: str = "auto",
        cache_dir: Optional[str] = None,
    ):
        self.model_size = model_size
        self.requested_device = device
        self.requested_compute = compute_type
        self.device, self.compute_type = detect_device(device, compute_type)
        self.cache_dir = cache_dir or os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "Data", "Cache_Transcripts"
        )
        os.makedirs(self.cache_dir, exist_ok=True)
        self._model = None

    def describe_device(self) -> Dict[str, Any]:
        info = {
            "device": self.device,
            "compute_type": self.compute_type,
            "model_size": self.model_size,
        }
        if self.device == "cuda":
            info.update(query_gpu())
        return info

    def _get_model(self) -> WhisperModel:
        if self._model is None:
            print(f"[WhisperTranscriber] Ładowanie modelu '{self.model_size}' na {self.device} ({self.compute_type})...")
            try:
                self._model = WhisperModel(self.model_size, device=self.device, compute_type=self.compute_type)
            except Exception as e:
                print(f"[WhisperTranscriber] Błąd inicjalizacji GPU: {e}. Przełączam na CPU / int8.")
                self.device, self.compute_type = "cpu", "int8"
                self._model = WhisperModel(self.model_size, device="cpu", compute_type="int8")
        return self._model

    def _get_cache_path(self, audio_path: str) -> str:
        base_name = os.path.splitext(os.path.basename(audio_path))[0]
        # Skrót ścieżki zapobiega kolizjom nazw między plikami z różnych katalogów.
        digest = hashlib.sha1(os.path.abspath(audio_path).encode("utf-8")).hexdigest()[:8]
        return os.path.join(self.cache_dir, f"{base_name}_{self.model_size}_{digest}.json")

    def _legacy_cache_path(self, audio_path: str) -> str:
        base_name = os.path.splitext(os.path.basename(audio_path))[0]
        return os.path.join(self.cache_dir, f"{base_name}_{self.model_size}.json")

    def has_cache(self, audio_path: str) -> bool:
        return os.path.exists(self._get_cache_path(audio_path)) or os.path.exists(
            self._legacy_cache_path(audio_path)
        )

    @staticmethod
    def _result_from_cache(data: Dict[str, Any], audio_path: str, language: str) -> TranscriptionResult:
        segments, all_words = [], []
        for seg in data["segments"]:
            words = [WordTimestamp(**w) for w in seg["words"]]
            all_words.extend(words)
            segments.append(TranscriptionSegment(
                id=seg["id"], seek=seg.get("seek", 0), start=seg["start"],
                end=seg["end"], text=seg["text"], words=words,
            ))
        return TranscriptionResult(
            audio_path=audio_path,
            duration=data.get("duration", 0.0),
            language=data.get("language", language),
            segments=segments,
            all_words=all_words,
        )

    def transcribe(
        self,
        audio_path: str,
        language: str = "pl",
        use_cache: bool = True,
        vad_filter: bool = True,
        progress_cb: ProgressCb = None,
    ) -> TranscriptionResult:
        cache_file = self._get_cache_path(audio_path)
        legacy_file = self._legacy_cache_path(audio_path)

        if use_cache:
            for candidate in (cache_file, legacy_file):
                if os.path.exists(candidate):
                    print(f"[WhisperTranscriber] Wczytuję transkrypcję z cache: {candidate}")
                    if progress_cb:
                        progress_cb(1.0, f"Transkrypcja z cache: {os.path.basename(audio_path)}")
                    with open(candidate, "r", encoding="utf-8") as f:
                        return self._result_from_cache(json.load(f), audio_path, language)

        total_duration = probe_duration(audio_path)
        model = self._get_model()
        print(f"[WhisperTranscriber] Transkrypcja {audio_path}...")
        if progress_cb:
            progress_cb(0.0, f"Transkrypcja: {os.path.basename(audio_path)}")

        segments_gen, info = model.transcribe(
            audio_path,
            language=language,
            word_timestamps=True,
            vad_filter=vad_filter,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        if total_duration <= 0:
            total_duration = getattr(info, "duration", 0.0) or 0.0

        segments: List[TranscriptionSegment] = []
        all_words: List[WordTimestamp] = []

        for seg_idx, seg in enumerate(segments_gen):
            words: List[WordTimestamp] = []
            if seg.words:
                for w in seg.words:
                    wt = WordTimestamp(
                        word=w.word.strip(),
                        start=round(w.start, 3),
                        end=round(w.end, 3),
                        probability=round(w.probability, 3),
                    )
                    words.append(wt)
                    all_words.append(wt)
            else:
                # Brak znaczników słów dla segmentu - rozkładamy je równomiernie w jego obrębie.
                seg_words = seg.text.strip().split()
                if seg_words:
                    dur_per_word = (seg.end - seg.start) / len(seg_words)
                    for i, sw in enumerate(seg_words):
                        wt = WordTimestamp(
                            word=sw,
                            start=round(seg.start + i * dur_per_word, 3),
                            end=round(seg.start + (i + 1) * dur_per_word, 3),
                            probability=1.0,
                        )
                        words.append(wt)
                        all_words.append(wt)

            segments.append(TranscriptionSegment(
                id=seg_idx, seek=seg.seek, start=round(seg.start, 3),
                end=round(seg.end, 3), text=seg.text.strip(), words=words,
            ))

            if progress_cb and total_duration > 0:
                progress_cb(
                    min(0.999, seg.end / total_duration),
                    f"Transkrypcja {os.path.basename(audio_path)}: "
                    f"{seg.end / 60:.1f} / {total_duration / 60:.1f} min",
                )

        result = TranscriptionResult(
            audio_path=audio_path,
            duration=round(total_duration or info.duration, 3),
            language=info.language,
            segments=segments,
            all_words=all_words,
        )

        cache_data = {
            "audio_path": audio_path,
            "duration": result.duration,
            "language": result.language,
            "segments": [
                {"id": s.id, "seek": s.seek, "start": s.start, "end": s.end,
                 "text": s.text, "words": [asdict(w) for w in s.words]}
                for s in segments
            ],
        }
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)

        print(f"[WhisperTranscriber] Gotowe: {len(segments)} segmentów, {len(all_words)} słów. Cache: {cache_file}")
        if progress_cb:
            progress_cb(1.0, f"Transkrypcja zakończona: {os.path.basename(audio_path)}")
        return result

    def transcribe_snippet(
        self,
        audio_path: str,
        seconds: float = 45.0,
        language: str = "pl",
        use_cache: bool = True,
    ) -> List[str]:
        """
        Zwraca listę słów z początku nagrania. Używane do wyszukiwania kotwic rozdziałów
        w tekście książki - transkrybuje wyłącznie wycięty fragment, więc jest tanie.
        Gdy pełna transkrypcja pliku jest już w cache, korzysta z niej zamiast liczyć ponownie.
        """
        if use_cache and self.has_cache(audio_path):
            full = self.transcribe(audio_path, language=language, use_cache=True)
            return [w.word for w in full.all_words if w.start < seconds]

        snippet_path = None
        try:
            fd, snippet_path = tempfile.mkstemp(suffix=".wav", prefix="anchor_")
            os.close(fd)
            cmd = [
                "ffmpeg", "-y", "-i", audio_path, "-t", f"{seconds:.2f}",
                "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", snippet_path,
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

            model = self._get_model()
            segments_gen, _ = model.transcribe(
                snippet_path, language=language, word_timestamps=False, vad_filter=False
            )
            words: List[str] = []
            for seg in segments_gen:
                words.extend(seg.text.strip().split())
            return words
        except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
            print(f"[WhisperTranscriber] Nie udało się przygotować próbki dla {audio_path}: {e}")
            return []
        finally:
            if snippet_path and os.path.exists(snippet_path):
                try:
                    os.remove(snippet_path)
                except OSError:
                    pass
