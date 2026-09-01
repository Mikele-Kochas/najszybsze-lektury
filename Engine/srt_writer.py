from typing import List
from .chunker import BoardChunk


def format_srt_timestamp(seconds: float) -> str:
    """Converts seconds (e.g. 123.456) into SRT timestamp format: HH:MM:SS,mmm"""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    ms = total_ms % 1000
    total_sec = total_ms // 1000
    sec = total_sec % 60
    total_min = total_sec // 60
    min_ = total_min % 60
    hours = total_min // 60
    return f"{hours:02d}:{min_:02d}:{sec:02d},{ms:03d}"


def srt_cue_text(text: str) -> str:
    """
    Tekst planszy w postaci bezpiecznej dla SRT.

    W formacie SRT pusta linia konczy napis, wiec akapit rozdzielony pusta linia
    rozbilby plik na smieciowe wpisy. Tekst korekty pochodzi z pola tekstowego
    w przegladarce, gdzie uzytkownik moze wcisnac Enter dwa razy - dlatego zwijamy
    ciagi pustych linii do pojedynczego lamania.
    """
    lines = [line.strip() for line in (text or '').splitlines()]
    return "\n".join(line for line in lines if line)


def generate_srt_content(chunks: List[BoardChunk]) -> str:
    """Generates valid SRT formatted string from list of BoardChunks."""
    lines = []
    for idx, chunk in enumerate(chunks, start=1):
        start_str = format_srt_timestamp(chunk.start_time)
        end_str = format_srt_timestamp(chunk.end_time)

        lines.append(str(idx))
        lines.append(f"{start_str} --> {end_str}")
        lines.append(srt_cue_text(chunk.text))
        lines.append("")  # Empty line separator

    return "\n".join(lines)


def write_srt_file(chunks: List[BoardChunk], output_path: str) -> None:
    """Writes chunks directly to an .srt file on disk with utf-8 encoding."""
    content = generate_srt_content(chunks)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(content)
