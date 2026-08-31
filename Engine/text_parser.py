import re
from dataclasses import dataclass
from typing import List, Optional

ROMAN_TO_INT = {
    'I': 1, 'II': 2, 'III': 3, 'IV': 4, 'V': 5,
    'VI': 6, 'VII': 7, 'VIII': 8, 'IX': 9, 'X': 10,
    'XI': 11, 'XII': 12, 'XIII': 13, 'XIV': 14, 'XV': 15,
    'XVI': 16, 'XVII': 17, 'XVIII': 18, 'XIX': 19, 'XX': 20,
    'XXI': 21, 'XXII': 22, 'XXIII': 23, 'XXIV': 24, 'XXV': 25
}

DIALOGUE_PREFIXES = ('—', '–', '-', '―', '“', '„', '"')

@dataclass
class TextBlock:
    text: str
    is_dialogue: bool
    estimated_lines: int
    sentences: List[str]

@dataclass
class Chapter:
    number: int
    roman: str
    title: str
    header: str
    raw_text: str
    blocks: List[TextBlock]


def estimate_line_count(text: str, max_chars_per_line: int = 45) -> int:
    """Estimates how many lines the text will occupy on screen."""
    lines = 0
    paragraphs = text.split('\n')
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        # Split paragraph into lines based on char count
        words = p.split()
        current_len = 0
        p_lines = 1 if words else 0
        for w in words:
            if current_len == 0:
                current_len = len(w)
            elif current_len + 1 + len(w) <= max_chars_per_line:
                current_len += 1 + len(w)
            else:
                p_lines += 1
                current_len = len(w)
        lines += p_lines
    return max(1, lines) if text.strip() else 0


def split_into_sentences(text: str) -> List[str]:
    """Splits a paragraph into sentences preserving ending punctuation."""
    # Pattern looks for sentence terminators (. ! ?) followed by whitespace or end of string
    raw_sentences = re.split(r'(?<=[.!?…])\s+(?=[A-ZĄĆĘŁŃÓŚŹŻ—–\-])', text)
    sentences = [s.strip() for s in raw_sentences if s.strip()]
    return sentences if sentences else [text.strip()]


def is_dialogue_line(text: str) -> bool:
    """Checks if a paragraph starts with dialogue indicator (em-dash, hyphen etc)."""
    trimmed = text.strip()
    return any(trimmed.startswith(prefix) for prefix in DIALOGUE_PREFIXES)


def parse_chapter_blocks(raw_text: str, max_chars_per_line: int = 45) -> List[TextBlock]:
    """Splits chapter text into atomic text blocks (paragraphs or dialogue lines)."""
    # Split on double or multiple newlines, or single newlines if following line is dialogue
    raw_paragraphs = re.split(r'\n\s*\n', raw_text)
    blocks = []

    for p in raw_paragraphs:
        p = p.strip()
        if not p:
            continue
        
        # If paragraph contains multiple internal dialogue lines separated by single newlines
        sub_lines = [line.strip() for line in p.split('\n') if line.strip()]
        if len(sub_lines) > 1 and any(is_dialogue_line(l) for l in sub_lines):
            for sub in sub_lines:
                is_diag = is_dialogue_line(sub)
                lines = estimate_line_count(sub, max_chars_per_line)
                sentences = split_into_sentences(sub)
                blocks.append(TextBlock(
                    text=sub,
                    is_dialogue=is_diag,
                    estimated_lines=lines,
                    sentences=sentences
                ))
        else:
            is_diag = is_dialogue_line(p)
            lines = estimate_line_count(p, max_chars_per_line)
            sentences = split_into_sentences(p)
            blocks.append(TextBlock(
                text=p,
                is_dialogue=is_diag,
                estimated_lines=lines,
                sentences=sentences
            ))

    return blocks


def parse_book(file_path: str, max_chars_per_line: int = 45) -> List[Chapter]:
    """Parses full book .txt file into structured chapters."""
    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    # Pattern to match: Rozdział [Numer/Cyfra]. [Tytuł]
    # We look for lines starting with "Rozdział" or "Rozdział <numer>"
    pattern = re.compile(
        r'(?:^|\n)[ \t]*(Rozdzia[łl]\s+([IVXLCDM\d]+)[.:\s]*(.*?))(?=\n\s*\n|\n[A-ZĄĆĘŁŃÓŚŹŻ—–\-])',
        re.IGNORECASE
    )

    matches = list(pattern.finditer(content))
    chapters = []

    for idx, match in enumerate(matches):
        full_header = match.group(1).strip()
        roman_or_num = match.group(2).strip().upper()
        title = match.group(3).strip() if match.group(3) else ""

        # Determine chapter number
        if roman_or_num.isdigit():
            ch_num = int(roman_or_num)
        else:
            ch_num = ROMAN_TO_INT.get(roman_or_num, idx + 1)

        # Chapter content is between end of this match header and start of next match (or end of file)
        start_pos = match.end()
        end_pos = matches[idx + 1].start() if idx + 1 < len(matches) else len(content)
        raw_chapter_text = content[start_pos:end_pos].strip()

        # Clean header artifact if repeated
        blocks = parse_chapter_blocks(raw_chapter_text, max_chars_per_line)

        chapters.append(Chapter(
            number=ch_num,
            roman=roman_or_num,
            title=title,
            header=full_header,
            raw_text=raw_chapter_text,
            blocks=blocks
        ))

    return chapters


INT_TO_ROMAN = [
    (1000, 'M'), (900, 'CM'), (500, 'D'), (400, 'CD'), (100, 'C'), (90, 'XC'),
    (50, 'L'), (40, 'XL'), (10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I'),
]


def int_to_roman(value: int) -> str:
    """Zamienia liczbe na cyfre rzymska (uzywane gdy mapa rozdzialow nie niesie oryginalnego naglowka)."""
    if value <= 0:
        return ""
    out = []
    remaining = value
    for amount, numeral in INT_TO_ROMAN:
        while remaining >= amount:
            out.append(numeral)
            remaining -= amount
    return "".join(out)


def build_chapter(
    number: int,
    raw_text: str,
    header: str = "",
    title: str = "",
    roman: str = "",
    max_chars_per_line: int = 45,
) -> Chapter:
    """Buduje obiekt Chapter z gotowego fragmentu tekstu (granice pochodza z mapy rozdzialow)."""
    body = (raw_text or "").strip()
    return Chapter(
        number=number,
        roman=roman or int_to_roman(number),
        title=title or "",
        header=header or f"Rozdzial {number}",
        raw_text=body,
        blocks=parse_chapter_blocks(body, max_chars_per_line),
    )


def chapters_from_map(
    full_text: str,
    chapter_map: List[dict],
    max_chars_per_line: int = 45,
) -> List[Chapter]:
    """
    Tworzy liste rozdzialow na podstawie zatwierdzonej mapy granic.
    Kazdy wpis mapy musi zawierac text_start i text_end (indeksy znakowe w full_text).
    """
    chapters: List[Chapter] = []
    for entry in chapter_map:
        start = max(0, int(entry.get("text_start", 0)))
        end = int(entry.get("text_end", len(full_text)))
        if end <= start:
            end = len(full_text)
        chapters.append(build_chapter(
            number=int(entry.get("chapter_num", len(chapters) + 1)),
            raw_text=full_text[start:end],
            header=entry.get("header", ""),
            title=entry.get("title", ""),
            max_chars_per_line=max_chars_per_line,
        ))
    return chapters
