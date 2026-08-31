from dataclasses import dataclass, field
from typing import List
from .text_parser import TextBlock, estimate_line_count, split_into_sentences


@dataclass
class BoardChunk:
    chunk_id: int
    text: str
    chunk_type: str  # 'narration', 'dialogue', or 'intro_outro'
    lines_count: int
    words: List[str] = field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0


def extract_words(text: str) -> List[str]:
    """Extracts raw words preserving order for alignment."""
    # Split on whitespace, strip punctuation for matching while keeping token list
    return [w for w in text.split() if w.strip()]


def create_chunks_for_chapter(
    blocks: List[TextBlock],
    max_lines: int = 11,
    max_chars_per_line: int = 45
) -> List[BoardChunk]:
    """
    Chunks chapter blocks into display boards.
    - Standard narration: 1 paragraph = 1 board (unless > max_lines, then split by sentences).
    - Dialogues: consecutive dialogue lines grouped into boards up to max_lines.
    """
    chunks: List[BoardChunk] = []
    chunk_counter = 1

    i = 0
    while i < len(blocks):
        block = blocks[i]

        if block.is_dialogue:
            # Accumulate consecutive dialogue lines into one board
            dialogue_lines: List[str] = [block.text]
            current_combined_text = block.text
            current_lines = estimate_line_count(current_combined_text, max_chars_per_line)

            # If a single dialogue block alone is already too big (> max_lines), split by sentences
            if current_lines > max_lines:
                sentences = block.sentences
                sub_sentences: List[str] = []
                sub_text = ""
                for s in sentences:
                    test_text = (sub_text + " " + s).strip() if sub_text else s
                    if estimate_line_count(test_text, max_chars_per_line) <= max_lines:
                        sub_sentences.append(s)
                        sub_text = test_text
                    else:
                        if sub_sentences:
                            chunks.append(BoardChunk(
                                chunk_id=chunk_counter,
                                text=sub_text,
                                chunk_type='dialogue',
                                lines_count=estimate_line_count(sub_text, max_chars_per_line),
                                words=extract_words(sub_text)
                            ))
                            chunk_counter += 1
                        sub_sentences = [s]
                        sub_text = s
                if sub_text:
                    chunks.append(BoardChunk(
                        chunk_id=chunk_counter,
                        text=sub_text,
                        chunk_type='dialogue',
                        lines_count=estimate_line_count(sub_text, max_chars_per_line),
                        words=extract_words(sub_text)
                    ))
                    chunk_counter += 1
                i += 1
                continue

            # Otherwise, pull next dialogue blocks while total lines <= max_lines
            j = i + 1
            while j < len(blocks) and blocks[j].is_dialogue:
                candidate_text = current_combined_text + "\n" + blocks[j].text
                candidate_lines = estimate_line_count(candidate_text, max_chars_per_line)
                if candidate_lines <= max_lines:
                    dialogue_lines.append(blocks[j].text)
                    current_combined_text = candidate_text
                    current_lines = candidate_lines
                    j += 1
                else:
                    break

            # Create dialogue chunk
            final_text = "\n".join(dialogue_lines)
            chunks.append(BoardChunk(
                chunk_id=chunk_counter,
                text=final_text,
                chunk_type='dialogue',
                lines_count=current_lines,
                words=extract_words(final_text)
            ))
            chunk_counter += 1
            i = j  # Advance index

        else:
            # Narration block
            lines = estimate_line_count(block.text, max_chars_per_line)
            if lines <= max_lines:
                # Exactly 1 paragraph = 1 board
                chunks.append(BoardChunk(
                    chunk_id=chunk_counter,
                    text=block.text,
                    chunk_type='narration',
                    lines_count=lines,
                    words=extract_words(block.text)
                ))
                chunk_counter += 1
            else:
                # Long paragraph: split gracefully on sentence boundaries
                sentences = block.sentences
                sub_sentences: List[str] = []
                sub_text = ""
                for s in sentences:
                    test_text = (sub_text + " " + s).strip() if sub_text else s
                    if estimate_line_count(test_text, max_chars_per_line) <= max_lines:
                        sub_sentences.append(s)
                        sub_text = test_text
                    else:
                        if sub_sentences:
                            chunks.append(BoardChunk(
                                chunk_id=chunk_counter,
                                text=sub_text,
                                chunk_type='narration',
                                lines_count=estimate_line_count(sub_text, max_chars_per_line),
                                words=extract_words(sub_text)
                            ))
                            chunk_counter += 1
                        sub_sentences = [s]
                        sub_text = s
                if sub_text:
                    chunks.append(BoardChunk(
                        chunk_id=chunk_counter,
                        text=sub_text,
                        chunk_type='narration',
                        lines_count=estimate_line_count(sub_text, max_chars_per_line),
                        words=extract_words(sub_text)
                    ))
                    chunk_counter += 1
            i += 1

    return chunks
