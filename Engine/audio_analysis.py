"""
Analiza akustyczna nagrania: obwiednia RMS, wykrywanie ciszy, dosuwanie granic plansz.

Moduł jest celowo samodzielny — nie importuje pipeline'u, chunkera ani alignera.
Dzięki temu da się go uruchomić i zmierzyć w oderwaniu od reszty aplikacji:

    python -m Engine.audio_analysis --all
    python -m Engine.audio_analysis --chapter 1 --json raport.json
    python -m Engine.audio_analysis --all --apply

Powód istnienia: Whisper kończy słowo systematycznie za wcześnie, a zaczyna następne
za późno. Eksporter tnie dokładnie w tych punktach, czyli w ogonie i ataku wyrazu —
mimo że pomiędzy planszami leży zwykle 0,4–1,6 s realnej ciszy. Przesunięcie cięcia
na środek tej ciszy usuwa obcinanie bez udziału użytkownika.
"""

from __future__ import annotations

import os
import sys
import json
import glob
import shutil
import hashlib
import argparse
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

DEFAULT_SAMPLE_RATE = 16000
DEFAULT_HOP_MS = 10.0
DEFAULT_PEAK_BUCKETS = 400      # kubełków min/max na sekundę — materiał dla fali w UI
DEFAULT_SEARCH_WINDOW = 0.70    # ile sekund wokół granicy przeszukujemy w poszukiwaniu ciszy
DEFAULT_MIN_SILENCE = 0.10      # krótsza przerwa to raczej zwarcie w mowie niż pauza
DEFAULT_HEAD_PAD = 0.25
DEFAULT_TAIL_PAD = 0.35
SAFE_MARGIN = 0.08              # próg "cięcie jest bezpieczne" używany w raporcie


# ---------------------------------------------------------------------------
# Dekodowanie
# ---------------------------------------------------------------------------

def decode_pcm(audio_path: str, sample_rate: int = DEFAULT_SAMPLE_RATE) -> np.ndarray:
    """Dekoduje nagranie do mono float32 w zakresie [-1, 1]."""
    cmd = [
        "ffmpeg", "-v", "error", "-i", audio_path,
        "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=True)
    except FileNotFoundError:
        raise RuntimeError("Nie znaleziono ffmpeg. Zainstaluj ffmpeg i dodaj go do PATH.")
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", errors="replace").strip()[:300]
        raise RuntimeError(f"ffmpeg nie zdekodował {os.path.basename(audio_path)}: {detail}")

    if not proc.stdout:
        raise RuntimeError(f"Dekoder zwrócił pusty strumień dla {os.path.basename(audio_path)}.")

    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


# ---------------------------------------------------------------------------
# Obwiednia i cisza
# ---------------------------------------------------------------------------

@dataclass
class Envelope:
    """Obwiednia RMS nagrania wraz z wyznaczonym progiem ciszy."""
    rms: np.ndarray
    hop_s: float
    duration: float
    noise_floor: float
    speech_level: float
    threshold: float

    def frame_at(self, t: float) -> int:
        return int(np.clip(round(t / self.hop_s), 0, len(self.rms) - 1))

    def time_of(self, frame: int) -> float:
        return frame * self.hop_s

    def is_quiet(self, t: float) -> bool:
        return bool(self.rms[self.frame_at(t)] < self.threshold)


@dataclass
class Silence:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def center(self) -> float:
        return (self.start + self.end) / 2.0


def build_envelope(
    samples: np.ndarray,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    hop_ms: float = DEFAULT_HOP_MS,
) -> Envelope:
    """Liczy RMS w oknach hop_ms i wyznacza próg oddzielający ciszę od mowy."""
    hop = max(1, int(round(sample_rate * hop_ms / 1000.0)))
    frame_count = len(samples) // hop
    if frame_count == 0:
        raise ValueError("Nagranie jest krótsze niż jedno okno analizy.")

    frames = samples[: frame_count * hop].reshape(frame_count, hop)
    rms = np.sqrt((frames ** 2).mean(axis=1)).astype(np.float32)

    # Percentyle zamiast minimum/maksimum — pojedynczy trzask albo jedno głośne
    # słowo nie mogą przesunąć progu dla całego rozdziału.
    noise_floor = float(np.percentile(rms, 5))
    speech_level = float(np.percentile(rms, 75))
    threshold = float(np.clip(
        max(noise_floor * 3.0, speech_level * 0.06),
        1e-4,
        max(speech_level * 0.35, 1e-4),
    ))

    return Envelope(
        rms=rms,
        hop_s=hop / float(sample_rate),
        duration=len(samples) / float(sample_rate),
        noise_floor=noise_floor,
        speech_level=speech_level,
        threshold=threshold,
    )


def find_silences(env: Envelope, min_duration: float = DEFAULT_MIN_SILENCE) -> List[Silence]:
    """Zwraca przedziały ciszy dłuższe niż min_duration."""
    quiet = env.rms < env.threshold
    if not quiet.any():
        return []

    marks = np.diff(quiet.astype(np.int8))
    starts = (np.flatnonzero(marks == 1) + 1).tolist()
    ends = (np.flatnonzero(marks == -1) + 1).tolist()
    if quiet[0]:
        starts.insert(0, 0)
    if quiet[-1]:
        ends.append(len(quiet))

    out: List[Silence] = []
    for s, e in zip(starts, ends):
        sil = Silence(start=env.time_of(s), end=env.time_of(e))
        if sil.duration >= min_duration:
            out.append(sil)
    return out


def quiet_run_before(env: Envelope, t: float, limit: float) -> float:
    """Ile sekund nieprzerwanej ciszy leży bezpośrednio przed chwilą t (maks. limit)."""
    frame = env.frame_at(t)
    max_back = int(round(limit / env.hop_s))
    steps = 0
    idx = frame - 1
    while idx >= 0 and steps < max_back and env.rms[idx] < env.threshold:
        idx -= 1
        steps += 1
    return steps * env.hop_s


def quiet_run_after(env: Envelope, t: float, limit: float) -> float:
    """Ile sekund nieprzerwanej ciszy leży bezpośrednio po chwili t (maks. limit)."""
    frame = env.frame_at(t)
    max_fwd = int(round(limit / env.hop_s))
    steps = 0
    idx = frame
    while idx < len(env.rms) and steps < max_fwd and env.rms[idx] < env.threshold:
        idx += 1
        steps += 1
    return steps * env.hop_s


def cut_safety(env: Envelope, t: float, probe: float = 0.6) -> float:
    """
    Miara bezpieczeństwa punktu cięcia: ile ciszy zostaje po obu stronach.

    Zero oznacza cięcie w środku mowy — dokładnie to, co obcina końcówki wyrazów.
    """
    if not env.is_quiet(t):
        return 0.0
    return min(quiet_run_before(env, t, probe), quiet_run_after(env, t, probe))


# ---------------------------------------------------------------------------
# Dosuwanie granic
# ---------------------------------------------------------------------------

@dataclass
class BoundarySnap:
    """Propozycja przesunięcia jednej granicy między planszami."""
    index: int                  # numer granicy: między planszą index a index+1
    original_end: float
    original_start: float
    cut: float
    confidence: float
    method: str                 # 'silence' albo 'midpoint'
    silence_start: float = 0.0
    silence_end: float = 0.0
    safety_before: float = 0.0  # bezpieczeństwo starych punktów cięcia
    safety_after: float = 0.0   # bezpieczeństwo nowego punktu

    @property
    def shift_end(self) -> float:
        return self.cut - self.original_end

    @property
    def shift_start(self) -> float:
        return self.cut - self.original_start

    @property
    def silence_duration(self) -> float:
        return self.silence_end - self.silence_start


def snap_boundary(
    env: Envelope,
    t_end: float,
    t_start: float,
    silences: Optional[List[Silence]] = None,
    search: float = DEFAULT_SEARCH_WINDOW,
    min_silence: float = DEFAULT_MIN_SILENCE,
    bias: float = 0.5,
    index: int = 0,
) -> BoundarySnap:
    """
    Przesuwa granicę między planszami na środek najbliższej sensownej ciszy.

    t_end   — koniec poprzedniej planszy wg alignmentu,
    t_start — początek następnej planszy wg alignmentu,
    bias    — 0.0 tnie przy początku ciszy, 1.0 przy końcu, 0.5 dzieli ją po połowie.

    Gdy w oknie nie ma dość długiej ciszy, zwracany jest środek luki z pewnością 0 —
    taka granica jest kandydatem do ręcznej poprawki w edytorze fali.
    """
    if silences is None:
        silences = find_silences(env, min_silence)

    midpoint = (t_end + t_start) / 2.0
    lo = max(0.0, min(t_end, midpoint) - search)
    hi = min(env.duration, max(t_start, midpoint) + search)

    best: Optional[Tuple[float, float, float, float]] = None
    for sil in silences:
        s0 = max(sil.start, lo)
        s1 = min(sil.end, hi)
        span = s1 - s0
        if span < min_silence:
            continue
        candidate = s0 + bias * span
        # Dłuższa cisza jest lepsza, ale cisza odległa od granicy to najczęściej
        # pauza wewnątrz sąsiedniej planszy — dosunięcie się do niej przeniosłoby
        # cięcie o całe zdanie. Stąd kara za odległość.
        score = min(span, 0.5) / 0.5 - 0.8 * abs(candidate - midpoint) / max(search, 1e-6)
        if best is None or score > best[0]:
            best = (score, candidate, s0, s1)

    if best is None:
        return BoundarySnap(
            index=index,
            original_end=t_end,
            original_start=t_start,
            cut=round(midpoint, 3),
            confidence=0.0,
            method="midpoint",
            safety_before=min(cut_safety(env, t_end), cut_safety(env, t_start)),
            safety_after=cut_safety(env, midpoint),
        )

    score, candidate, s0, s1 = best
    return BoundarySnap(
        index=index,
        original_end=t_end,
        original_start=t_start,
        cut=round(candidate, 3),
        confidence=round(float(np.clip(score, 0.0, 1.0)), 3),
        method="silence",
        silence_start=round(s0, 3),
        silence_end=round(s1, 3),
        safety_before=min(cut_safety(env, t_end), cut_safety(env, t_start)),
        safety_after=cut_safety(env, candidate),
    )


def _reach_forward(env: Envelope, t: float, limit: float) -> float:
    """
    O ile sekund można przesunąć krawędź w prawo, żeby objąć całą ciszę za mową.

    Chwila t bywa jeszcze w środku wyrazu — czasy słów z Whispera kończą się
    systematycznie przed faktycznym wybrzmieniem głoski. Dlatego najpierw dochodzimy
    do ciszy, a dopiero potem ją obejmujemy. Gdy w zasięgu nie ma ciszy, zwracamy 0:
    wydłużanie w głąb mowy dokleiłoby do planszy cudze słowa.
    """
    max_steps = int(round(limit / env.hop_s))
    idx = env.frame_at(t)
    steps = 0
    seen_quiet = False
    while idx < len(env.rms) and steps < max_steps:
        quiet = bool(env.rms[idx] < env.threshold)
        if quiet:
            seen_quiet = True
        elif seen_quiet:
            break
        idx += 1
        steps += 1
    return steps * env.hop_s if seen_quiet else 0.0


def _reach_backward(env: Envelope, t: float, limit: float) -> float:
    """Lustrzane odbicie `_reach_forward` — o ile można cofnąć krawędź w lewo."""
    max_steps = int(round(limit / env.hop_s))
    idx = env.frame_at(t) - 1
    steps = 0
    seen_quiet = False
    while idx >= 0 and steps < max_steps:
        quiet = bool(env.rms[idx] < env.threshold)
        if quiet:
            seen_quiet = True
        elif seen_quiet:
            break
        idx -= 1
        steps += 1
    return steps * env.hop_s if seen_quiet else 0.0


def snap_head(env: Envelope, t_first: float, pad: float = DEFAULT_HEAD_PAD) -> float:
    """Cofa początek pierwszej planszy o ciszę poprzedzającą pierwsze słowo."""
    return round(max(0.0, t_first - _reach_backward(env, t_first, pad)), 3)


def snap_tail(env: Envelope, t_last: float, pad: float = DEFAULT_TAIL_PAD) -> float:
    """Wydłuża koniec ostatniej planszy o ciszę następującą po ostatnim słowie."""
    return round(min(env.duration, t_last + _reach_forward(env, t_last, pad)), 3)


# ---------------------------------------------------------------------------
# Fala do interfejsu (materiał dla etapu 5)
# ---------------------------------------------------------------------------

def build_peaks(
    samples: np.ndarray,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    buckets_per_sec: int = DEFAULT_PEAK_BUCKETS,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Min/max w kubełkach — gotowy materiał do rysowania fali na canvasie.

    Przeglądarka nie może dekodować 30-minutowego MP3 (~600 MB float32 w pamięci);
    tu 30 minut to ~1,4 MB w int8.
    """
    bucket = max(1, int(round(sample_rate / float(buckets_per_sec))))
    count = len(samples) // bucket
    if count == 0:
        return np.zeros(0, dtype=np.int8), np.zeros(0, dtype=np.int8)

    frames = samples[: count * bucket].reshape(count, bucket)
    lo = np.clip(frames.min(axis=1) * 127.0, -127, 127).astype(np.int8)
    hi = np.clip(frames.max(axis=1) * 127.0, -127, 127).astype(np.int8)
    return lo, hi


# ---------------------------------------------------------------------------
# Analiza z cache
# ---------------------------------------------------------------------------

@dataclass
class AudioAnalysis:
    audio_path: str
    envelope: Envelope
    silences: List[Silence]
    peaks_min: np.ndarray
    peaks_max: np.ndarray
    buckets_per_sec: int


def _cache_file(audio_path: str, cache_dir: str, hop_ms: float, sr: int, buckets: int) -> str:
    stat = os.stat(audio_path)
    key = f"{os.path.basename(audio_path)}|{stat.st_size}|{int(stat.st_mtime)}|{hop_ms}|{sr}|{buckets}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    base = os.path.splitext(os.path.basename(audio_path))[0]
    return os.path.join(cache_dir, f"{base}_{digest}.npz")


def analyze_audio(
    audio_path: str,
    cache_dir: Optional[str] = None,
    hop_ms: float = DEFAULT_HOP_MS,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    buckets_per_sec: int = DEFAULT_PEAK_BUCKETS,
    min_silence: float = DEFAULT_MIN_SILENCE,
    use_cache: bool = True,
) -> AudioAnalysis:
    """Obwiednia + cisze + peaki, z cache na dysku (dekodowanie to najdroższy krok)."""
    if not os.path.exists(audio_path):
        raise FileNotFoundError(f"Brak pliku audio: {audio_path}")

    cache_path = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = _cache_file(audio_path, cache_dir, hop_ms, sample_rate, buckets_per_sec)

    if use_cache and cache_path and os.path.exists(cache_path):
        try:
            with np.load(cache_path) as data:
                env = Envelope(
                    rms=data["rms"],
                    hop_s=float(data["hop_s"]),
                    duration=float(data["duration"]),
                    noise_floor=float(data["noise_floor"]),
                    speech_level=float(data["speech_level"]),
                    threshold=float(data["threshold"]),
                )
                return AudioAnalysis(
                    audio_path=audio_path,
                    envelope=env,
                    silences=find_silences(env, min_silence),
                    peaks_min=data["peaks_min"],
                    peaks_max=data["peaks_max"],
                    buckets_per_sec=int(data["buckets_per_sec"]),
                )
        except (OSError, KeyError, ValueError) as exc:
            print(f"[AudioAnalysis] Cache {os.path.basename(cache_path)} nieczytelny ({exc}), liczę od nowa.")

    samples = decode_pcm(audio_path, sample_rate)
    env = build_envelope(samples, sample_rate, hop_ms)
    peaks_min, peaks_max = build_peaks(samples, sample_rate, buckets_per_sec)

    if cache_path:
        try:
            np.savez_compressed(
                cache_path,
                rms=env.rms, hop_s=env.hop_s, duration=env.duration,
                noise_floor=env.noise_floor, speech_level=env.speech_level,
                threshold=env.threshold, peaks_min=peaks_min, peaks_max=peaks_max,
                buckets_per_sec=buckets_per_sec,
            )
        except OSError as exc:
            print(f"[AudioAnalysis] Nie udało się zapisać cache: {exc}")

    return AudioAnalysis(
        audio_path=audio_path,
        envelope=env,
        silences=find_silences(env, min_silence),
        peaks_min=peaks_min,
        peaks_max=peaks_max,
        buckets_per_sec=buckets_per_sec,
    )


# ---------------------------------------------------------------------------
# Poziom rozdziału
# ---------------------------------------------------------------------------

def resolve_audio_path(payload: Dict[str, Any], audio_dir: str) -> Optional[str]:
    """
    Ścieżka audio rozdziału. Zapisana w JSON-ie bywa nieaktualna (wyniki powstałe
    w kontenerze niosą /app/Data/...), więc awaryjnie szukamy po nazwie pliku.
    """
    path = payload.get("audio_path")
    if path and os.path.exists(path):
        return path
    name = payload.get("audio_file") or (os.path.basename(path) if path else None)
    if name:
        candidate = os.path.join(audio_dir, name)
        if os.path.exists(candidate):
            return candidate
    return None


def refine_chapter(
    payload: Dict[str, Any],
    analysis: AudioAnalysis,
    search: float = DEFAULT_SEARCH_WINDOW,
    min_silence: float = DEFAULT_MIN_SILENCE,
    bias: float = 0.5,
    head_pad: float = DEFAULT_HEAD_PAD,
    tail_pad: float = DEFAULT_TAIL_PAD,
) -> Dict[str, Any]:
    """Liczy propozycje cięć dla wszystkich granic rozdziału. Nie modyfikuje payloadu."""
    env = analysis.envelope
    chunks = payload.get("chunks", [])
    snaps: List[BoundarySnap] = []

    for i in range(len(chunks) - 1):
        snaps.append(snap_boundary(
            env,
            t_end=float(chunks[i].get("end_time", 0.0)),
            t_start=float(chunks[i + 1].get("start_time", 0.0)),
            silences=analysis.silences,
            search=search,
            min_silence=min_silence,
            bias=bias,
            index=i,
        ))

    head = snap_head(env, float(chunks[0]["start_time"]), head_pad) if chunks else 0.0
    tail = snap_tail(env, float(chunks[-1]["end_time"]), tail_pad) if chunks else 0.0

    return {
        "chapter_num": payload.get("chapter_num"),
        "audio_file": payload.get("audio_file"),
        "duration": env.duration,
        "threshold": env.threshold,
        "noise_floor": env.noise_floor,
        "head": head,
        "tail": tail,
        "snaps": snaps,
    }


def apply_refinement(payload: Dict[str, Any], refinement: Dict[str, Any]) -> int:
    """
    Wpisuje wyliczone cięcia do plansz. Zwraca liczbę zmienionych granic.

    Po tej operacji plansze przylegają do siebie: każda sekunda nagrania należy
    dokładnie do jednej planszy, a cięcie leży w środku pauzy. Format zapisu
    pozostaje bez zmian — modyfikowane są wyłącznie wartości czasów.
    """
    chunks = payload.get("chunks", [])
    if not chunks:
        return 0

    changed = 0
    for snap in refinement["snaps"]:
        i = snap.index
        prev_start = float(chunks[i]["start_time"])
        next_end = float(chunks[i + 1]["end_time"])
        # Cięcie musi zostać wewnątrz pary plansz, inaczej powstałby ujemny czas
        # trwania albo plansza przeskoczyłaby przez sąsiada.
        if not (prev_start + 0.05 < snap.cut < next_end - 0.05):
            continue
        chunks[i]["end_time"] = round(snap.cut, 3)
        chunks[i + 1]["start_time"] = round(snap.cut, 3)
        changed += 1

    chunks[0]["start_time"] = round(min(refinement["head"], float(chunks[0]["end_time"]) - 0.05), 3)
    chunks[-1]["end_time"] = round(max(refinement["tail"], float(chunks[-1]["start_time"]) + 0.05), 3)

    for c in chunks:
        c["duration"] = round(float(c["end_time"]) - float(c["start_time"]), 3)

    return changed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _default_dirs() -> Tuple[str, str, str]:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data = os.path.join(base, "Data")
    return (
        os.path.join(data, "Processed_JSON"),
        os.path.join(data, "Audio"),
        os.path.join(data, "Cache_Audio_Analysis"),
    )


def _print_chapter_table(refinement: Dict[str, Any]) -> None:
    print(f"  {'granica':>9} {'koniec':>8} {'start':>8} {'luka':>6} "
          f"{'cisza':>7} {'ciecie':>8} {'przes.':>7} {'bezp.przed':>10} {'bezp.po':>8} {'pewn.':>6}")
    for s in refinement["snaps"]:
        sil = f"{s.silence_duration * 1000:5.0f}ms" if s.method == "silence" else "   brak"
        print(f"  {s.index + 1:>4}/{s.index + 2:<4} {s.original_end:8.2f} {s.original_start:8.2f} "
              f"{s.original_start - s.original_end:6.2f} {sil:>7} {s.cut:8.2f} {s.shift_end:+7.2f} "
              f"{s.safety_before * 1000:8.0f}ms {s.safety_after * 1000:6.0f}ms {s.confidence:6.2f}")


def main(argv: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    proc_dir, audio_dir, cache_dir = _default_dirs()
    parser = argparse.ArgumentParser(
        description="Dosuwanie granic plansz do ciszy w nagraniu (etap 1)."
    )
    parser.add_argument("--chapter", type=int, action="append", help="Numer rozdziału (można podać wiele razy)")
    parser.add_argument("--all", action="store_true", help="Wszystkie przetworzone rozdziały")
    parser.add_argument("--processed-dir", default=proc_dir)
    parser.add_argument("--audio-dir", default=audio_dir)
    parser.add_argument("--cache-dir", default=cache_dir)
    parser.add_argument("--search", type=float, default=DEFAULT_SEARCH_WINDOW)
    parser.add_argument("--min-silence", type=float, default=DEFAULT_MIN_SILENCE)
    parser.add_argument("--bias", type=float, default=0.5, help="0 = tnij na początku ciszy, 1 = na końcu")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="Tylko podsumowanie, bez tabel")
    parser.add_argument("--json", help="Zapisz raport do pliku JSON")
    parser.add_argument("--apply", action="store_true",
                        help="Wpisz nowe czasy do chapter_*.json (kopia zapasowa w .bak)")
    args = parser.parse_args(argv)

    files = sorted(glob.glob(os.path.join(args.processed_dir, "chapter_*.json")))
    if args.chapter:
        wanted = set(args.chapter)
        files = [f for f in files
                 if int(os.path.basename(f).split("_")[1].split(".")[0]) in wanted]
    elif not args.all:
        parser.error("Podaj --chapter N albo --all.")

    if not files:
        print("Nie znaleziono przetworzonych rozdziałów.")
        return 1

    report: List[Dict[str, Any]] = []
    safe_before = safe_after = total_boundaries = 0
    margins_before: List[float] = []
    margins_after: List[float] = []
    no_silence = 0

    for path in files:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        ch_num = payload.get("chapter_num")
        audio_path = resolve_audio_path(payload, args.audio_dir)
        if not audio_path:
            print(f"Rozdział {ch_num}: brak pliku audio ({payload.get('audio_file')}), pomijam.")
            continue

        analysis = analyze_audio(
            audio_path,
            cache_dir=None if args.no_cache else args.cache_dir,
            min_silence=args.min_silence,
            use_cache=not args.no_cache,
        )
        refinement = refine_chapter(
            payload, analysis,
            search=args.search, min_silence=args.min_silence, bias=args.bias,
        )

        if not args.quiet:
            env = analysis.envelope
            print(f"\nRozdział {ch_num}  ({os.path.basename(audio_path)}, {env.duration:.1f}s, "
                  f"szum {env.noise_floor:.4f}, próg {env.threshold:.4f}, "
                  f"{len(refinement['snaps'])} granic)")
            _print_chapter_table(refinement)

        for s in refinement["snaps"]:
            total_boundaries += 1
            margins_before.append(s.safety_before)
            margins_after.append(s.safety_after)
            if s.safety_before >= SAFE_MARGIN:
                safe_before += 1
            if s.safety_after >= SAFE_MARGIN:
                safe_after += 1
            if s.method != "silence":
                no_silence += 1

        report.append({
            "chapter_num": ch_num,
            "audio_file": payload.get("audio_file"),
            "duration": refinement["duration"],
            "threshold": refinement["threshold"],
            "head": refinement["head"],
            "tail": refinement["tail"],
            "boundaries": [
                {
                    "index": s.index, "original_end": s.original_end,
                    "original_start": s.original_start, "cut": s.cut,
                    "shift_end": round(s.shift_end, 3), "shift_start": round(s.shift_start, 3),
                    "silence_duration": round(s.silence_duration, 3),
                    "safety_before": round(s.safety_before, 3),
                    "safety_after": round(s.safety_after, 3),
                    "confidence": s.confidence, "method": s.method,
                }
                for s in refinement["snaps"]
            ],
        })

        if args.apply:
            backup = path + ".bak"
            if not os.path.exists(backup):
                shutil.copy2(path, backup)
            changed = apply_refinement(payload, refinement)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            print(f"  -> zapisano {changed}/{len(refinement['snaps'])} granic "
                  f"w {os.path.basename(path)} (kopia: {os.path.basename(backup)})")

    if total_boundaries:
        def pct(n: int) -> float:
            return 100.0 * n / total_boundaries

        def med(xs: List[float]) -> float:
            return float(np.median(xs)) * 1000.0

        print("\n" + "=" * 74)
        print(f"PODSUMOWANIE — {len(report)} rozdz., {total_boundaries} granic, "
              f"próg bezpieczeństwa {SAFE_MARGIN * 1000:.0f} ms ciszy po obu stronach cięcia")
        print(f"  przed:  {safe_before:4d} bezpiecznych ({pct(safe_before):5.1f}%),  "
              f"mediana marginesu {med(margins_before):6.0f} ms")
        print(f"  po:     {safe_after:4d} bezpiecznych ({pct(safe_after):5.1f}%),  "
              f"mediana marginesu {med(margins_after):6.0f} ms")
        print(f"  granic bez wykrytej ciszy (do ręcznej poprawki): {no_silence} "
              f"({pct(no_silence):.1f}%)")
        print("=" * 74)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"Raport JSON: {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
