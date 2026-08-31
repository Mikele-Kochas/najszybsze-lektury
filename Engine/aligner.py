import re
import difflib
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict
from .transcriber import WordTimestamp, TranscriptionResult
from .chunker import BoardChunk


def normalize_word(w: str) -> str:
    """Lowercases and removes punctuation for token matching."""
    cleaned = re.sub(r'[^\w]', '', w.lower(), flags=re.UNICODE)
    return cleaned


@dataclass
class AlignedWord:
    original_word: str
    norm_word: str
    start: float
    end: float
    matched: bool
    whisper_word: Optional[str] = None


@dataclass
class AlignmentReport:
    total_book_words: int
    total_whisper_words: int
    matched_words: int
    match_percentage: float
    intro_detected: bool
    intro_duration: float
    outro_detected: bool
    outro_duration: float


class SequenceAligner:
    def __init__(self, fuzzy_threshold: float = 0.75):
        self.fuzzy_threshold = fuzzy_threshold

    def align_chapter(
        self,
        chunks: List[BoardChunk],
        transcription: TranscriptionResult,
        intro_outro_placeholder: str = "(brak tekstu w pliku txt)"
    ) -> Tuple[List[BoardChunk], AlignmentReport, List[AlignedWord]]:
        """
        Aligns book chunks with whisper word timestamps.
        Returns updated chunks with start_time and end_time, plus intro/outro boards if present.

        Trzeci element to czasy pojedynczych słów książki, w kolejności czytania.
        To one są właściwym wynikiem dopasowania — plansze da się z nich odtworzyć
        w dowolnym podziale, bez powtarzania transkrypcji. Bez tej listy każda zmiana
        granicy wymagałaby przeliczenia całego rozdziału od nowa.
        """
        # 1. Flatten all book words from chunks
        book_words_map: List[Tuple[int, int, str]] = []  # (chunk_idx, word_idx_in_chunk, word_text)
        for c_idx, chunk in enumerate(chunks):
            for w_idx, w in enumerate(chunk.words):
                book_words_map.append((c_idx, w_idx, w))

        book_norm = [normalize_word(w[2]) for w in book_words_map]
        whisper_words = transcription.all_words
        whisper_norm = [normalize_word(w.word) for w in whisper_words]

        # 2. Sequence Matcher for exact token sequences
        matcher = difflib.SequenceMatcher(None, whisper_norm, book_norm, autojunk=False)
        matching_blocks = matcher.get_matching_blocks()

        aligned_book_words: List[Optional[AlignedWord]] = [None] * len(book_norm)
        matched_whisper_indices: Dict[int, int] = {}  # whisper_idx -> book_idx
        book_to_whisper: Dict[int, int] = {}          # book_idx -> whisper_idx (odwrotny indeks)

        for w_start, b_start, length in matching_blocks:
            for k in range(length):
                w_idx = w_start + k
                b_idx = b_start + k
                if w_idx < len(whisper_words) and b_idx < len(book_words_map):
                    aligned_book_words[b_idx] = AlignedWord(
                        original_word=book_words_map[b_idx][2],
                        norm_word=book_norm[b_idx],
                        start=whisper_words[w_idx].start,
                        end=whisper_words[w_idx].end,
                        matched=True,
                        whisper_word=whisper_words[w_idx].word
                    )
                    matched_whisper_indices[w_idx] = b_idx
                    book_to_whisper[b_idx] = w_idx

        # 3. Secondary local fuzzy match in unmapped gaps
        for b_idx in range(len(book_norm)):
            if aligned_book_words[b_idx] is not None:
                continue

            # Find nearest left and right matched whisper boundaries
            # to restrict search window
            left_w = 0
            for prev_b in range(b_idx - 1, -1, -1):
                if aligned_book_words[prev_b] is not None:
                    left_w = book_to_whisper.get(prev_b, 0)
                    break

            right_w = len(whisper_words) - 1
            for next_b in range(b_idx + 1, len(book_norm)):
                if aligned_book_words[next_b] is not None:
                    right_w = book_to_whisper.get(next_b, len(whisper_words) - 1)
                    break

            # Search in range [left_w, right_w]
            b_word = book_norm[b_idx]
            best_ratio = 0.0
            best_w_idx = -1
            if left_w <= right_w and b_word:
                for cand_w in range(left_w, min(right_w + 1, len(whisper_words))):
                    if cand_w in matched_whisper_indices:
                        continue
                    ratio = difflib.SequenceMatcher(None, b_word, whisper_norm[cand_w]).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_w_idx = cand_w

            if best_ratio >= self.fuzzy_threshold and best_w_idx != -1:
                aligned_book_words[b_idx] = AlignedWord(
                    original_word=book_words_map[b_idx][2],
                    norm_word=b_word,
                    start=whisper_words[best_w_idx].start,
                    end=whisper_words[best_w_idx].end,
                    matched=True,
                    whisper_word=whisper_words[best_w_idx].word
                )
                matched_whisper_indices[best_w_idx] = b_idx
                book_to_whisper[b_idx] = best_w_idx

        # 4. Interpolate any remaining unmapped book words
        # First ensure boundaries exist
        first_matched_idx = next((i for i, aw in enumerate(aligned_book_words) if aw is not None), None)
        last_matched_idx = next((i for i in range(len(aligned_book_words) - 1, -1, -1) if aligned_book_words[i] is not None), None)

        if first_matched_idx is None:
            # Complete failure fallback (e.g. empty audio or totally wrong file)
            duration = transcription.duration or 60.0
            time_step = duration / max(1, len(aligned_book_words))
            for i in range(len(aligned_book_words)):
                aligned_book_words[i] = AlignedWord(
                    original_word=book_words_map[i][2],
                    norm_word=book_norm[i],
                    start=round(i * time_step, 3),
                    end=round((i + 1) * time_step, 3),
                    matched=False
                )
        else:
            # Interpolate leading unmapped words
            first_time = aligned_book_words[first_matched_idx].start
            for i in range(first_matched_idx):
                est_start = max(0.0, first_time - (first_matched_idx - i) * 0.35)
                est_end = max(est_start + 0.1, first_time - (first_matched_idx - i - 1) * 0.35)
                aligned_book_words[i] = AlignedWord(
                    original_word=book_words_map[i][2],
                    norm_word=book_norm[i],
                    start=round(est_start, 3),
                    end=round(est_end, 3),
                    matched=False
                )

            # Interpolate trailing unmapped words
            last_time = aligned_book_words[last_matched_idx].end
            for i in range(last_matched_idx + 1, len(aligned_book_words)):
                offset = i - last_matched_idx
                est_start = min(transcription.duration, last_time + (offset - 1) * 0.35)
                est_end = min(transcription.duration, last_time + offset * 0.35)
                aligned_book_words[i] = AlignedWord(
                    original_word=book_words_map[i][2],
                    norm_word=book_norm[i],
                    start=round(est_start, 3),
                    end=round(est_end, 3),
                    matched=False
                )

            # Interpolate interior gaps between matched words
            prev_valid = first_matched_idx
            curr = first_matched_idx + 1
            while curr <= last_matched_idx:
                if aligned_book_words[curr] is not None:
                    gap_count = curr - prev_valid - 1
                    if gap_count > 0:
                        t_start = aligned_book_words[prev_valid].end
                        t_end = aligned_book_words[curr].start
                        t_span = max(0.05 * gap_count, t_end - t_start)
                        step = t_span / (gap_count + 1)
                        for g in range(gap_count):
                            g_idx = prev_valid + 1 + g
                            g_start = round(t_start + g * step, 3)
                            g_end = round(t_start + (g + 1) * step, 3)
                            aligned_book_words[g_idx] = AlignedWord(
                                original_word=book_words_map[g_idx][2],
                                norm_word=book_norm[g_idx],
                                start=g_start,
                                end=g_end,
                                matched=False
                            )
                    prev_valid = curr
                curr += 1

        # 5. Assign timestamps back to chunks
        chunk_word_ranges: Dict[int, List[int]] = {}
        for b_idx, (c_idx, w_idx, _) in enumerate(book_words_map):
            if c_idx not in chunk_word_ranges:
                chunk_word_ranges[c_idx] = []
            chunk_word_ranges[c_idx].append(b_idx)

        final_chunks: List[BoardChunk] = []

        def heard(t0: float, t1: float) -> str:
            """Co lektor mówi w danym przedziale — treść planszy z wtrąceniem."""
            said = " ".join(
                seg.text.strip() for seg in transcription.segments
                if seg.end > t0 + 0.1 and seg.start < t1 - 0.1 and seg.text.strip()
            ).strip()
            return said + "\n" + intro_outro_placeholder if said else intro_outro_placeholder

        # Check for Intro (speech before first matched book word)
        first_w_idx = min(matched_whisper_indices.keys()) if matched_whisper_indices else 0
        intro_detected = False
        intro_duration = 0.0

        if first_w_idx > 0 and len(whisper_words) > 0:
            intro_start = whisper_words[0].start
            intro_end = whisper_words[first_w_idx - 1].end
            if intro_end - intro_start >= 1.0:
                intro_detected = True
                intro_duration = round(intro_end - intro_start, 3)
                intro_text = heard(intro_start, intro_end)
                final_chunks.append(BoardChunk(
                    chunk_id=0,
                    text=intro_text,
                    chunk_type="intro_outro",
                    lines_count=len(intro_text.splitlines()),
                    words=intro_text.split(),
                    start_time=round(intro_start, 3),
                    end_time=round(intro_end, 3)
                ))

        # Add book chunks with matched timestamps
        for c_idx, chunk in enumerate(chunks):
            indices = chunk_word_ranges.get(c_idx, [])
            if indices:
                first_aw = aligned_book_words[indices[0]]
                last_aw = aligned_book_words[indices[-1]]
                c_start = first_aw.start if first_aw else 0.0
                c_end = last_aw.end if last_aw else c_start + 2.0
            else:
                c_start = 0.0
                c_end = 2.0

            # Ensure monotonic timestamp progression
            if final_chunks and c_start < final_chunks[-1].end_time:
                c_start = final_chunks[-1].end_time

            c_end = max(c_start + 0.5, c_end)

            chunk.start_time = round(c_start, 3)
            chunk.end_time = round(c_end, 3)
            final_chunks.append(chunk)

        # Check for Outro (speech after last matched book word)
        last_w_idx = max(matched_whisper_indices.keys()) if matched_whisper_indices else len(whisper_words) - 1
        outro_detected = False
        outro_duration = 0.0

        if last_w_idx < len(whisper_words) - 1 and len(whisper_words) > 0:
            outro_start = whisper_words[last_w_idx + 1].start
            outro_end = whisper_words[-1].end
            if outro_end - outro_start >= 1.0:
                outro_detected = True
                outro_duration = round(outro_end - outro_start, 3)
                outro_text = heard(outro_start, outro_end)
                final_chunks.append(BoardChunk(
                    chunk_id=len(final_chunks) + 1,
                    text=outro_text,
                    chunk_type="intro_outro",
                    lines_count=len(outro_text.splitlines()),
                    words=outro_text.split(),
                    start_time=round(outro_start, 3),
                    end_time=round(outro_end, 3)
                ))

        # Re-number chunk_ids consecutively (1, 2, 3...)
        for idx, fc in enumerate(final_chunks, 1):
            fc.chunk_id = idx

        matched_count = sum(1 for aw in aligned_book_words if aw and aw.matched)
        total_book = len(book_norm)
        match_pct = round((matched_count / total_book * 100.0) if total_book > 0 else 0.0, 2)

        report = AlignmentReport(
            total_book_words=total_book,
            total_whisper_words=len(whisper_words),
            matched_words=matched_count,
            match_percentage=match_pct,
            intro_detected=intro_detected,
            intro_duration=intro_duration,
            outro_detected=outro_detected,
            outro_duration=outro_duration
        )

        # Po interpolacji każde słowo ma już czas; rzutowanie zdejmuje Optional,
        # który miał sens tylko w trakcie wypełniania tablicy.
        word_times: List[AlignedWord] = [aw for aw in aligned_book_words if aw is not None]
        if len(word_times) != len(book_words_map):
            raise RuntimeError(
                f"Alignment zostawił {len(book_words_map) - len(word_times)} słów bez czasu "
                "— plansz nie da się z nich odtworzyć."
            )

        return final_chunks, report, word_times
