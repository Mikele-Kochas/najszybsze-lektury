# Najszybsze Lektury 📖

> Aplikacja webowa, która z **jednego pliku `.txt`** i **kompletu nagrań `.mp3`** (jedno na rozdział)
> generuje gotową paczkę produkcyjną: napisy SRT, etykiety Audacity, tekst z podziałem na plansze
> oraz pocięte audio.

---

## ⚡ Pierwsze uruchomienie

Potrzebny **Docker** oraz karta **NVIDIA** ze sterownikami (szczegóły i wariant bez GPU
niżej, w sekcji [Uruchomienie z Dockerem](#-uruchomienie-z-dockerem)).

```bash
git clone <adres-repozytorium>
cd "Najszybsze Lektury"
docker compose up -d --build      # pierwszy build ok. 5 GB, potem już z cache
# http://localhost:8000
```

Czego się spodziewać przy pierwszym starcie:

- **`Data/` jest puste i tak ma być.** Materiały (`.txt` + nagrania) wgrywa się
  w kroku „Projekt" w interfejsie. Repozytorium zawiera narzędzie, nie obrabiane książki.
- **Model Whisper pobiera się przy pierwszym przetwarzaniu** (kilkaset MB, potrzebny
  internet). Trafia do wolumenu `whisper_models`, więc kolejne uruchomienia go nie ściągają.
- Jeśli `pip install` przerywa build błędem `CERTIFICATE_VERIFY_FAILED`, w sieci działa
  skanowanie HTTPS — patrz [Sieci z przechwytywaniem TLS](#sieci-z-przechwytywaniem-tls).

Zamierzasz zmieniać kod? Zajrzyj do [Tryb deweloperski](#tryb-deweloperski--praca-nad-kodem) —
bez tego każda edycja wymaga przebudowy obrazu.

---

## 🔄 Przepływ pracy

Aplikacja prowadzi przez sześć kroków widocznych w górnej nawigacji:

| Krok | Widok | Co się dzieje |
|------|-------|----------------|
| **1** | **Projekt** | Wgrywasz `.txt` z tekstem książki i pliki audio (przeciągnij lub wybierz). Ustawiasz model Whisper, urządzenie i parametry plansz. |
| **2** | **Rozdziały** | Aplikacja wykrywa podział na rozdziały i przypisuje im nagrania. Sprawdzasz i poprawiasz mapę, a potem uruchamiasz przetwarzanie. |
| **3** | **Granice** | Ręczny montaż podziału na plansze: dzielisz, scalasz i przesuwasz granice, poprawiasz teksty. Działa **bez nagrania** — podział zależy tylko od tekstu. |
| **4** | **Cięcia** | Dopracowujesz punkt cięcia każdej granicy na fali dźwięku; automat dosuwa go do ciszy, ty masz ostatnie słowo. |
| **5** | **Weryfikacja** | Odsłuchujesz plansze (losowe próbki lub ciągłe odtwarzanie) i oceniasz dopasowanie. |
| **6** | **Eksport** | Budujesz paczkę i pobierasz ZIP. |

Kroki 3 i 4 są opcjonalne — automat proponuje kompletny podział i czasy, więc da się
przejść z kroku 2 wprost do eksportu. Zatwierdzone ręcznie granice, czasy cięć i poprawki
tekstu **przeżywają ponowne przetworzenie rozdziału** (leżą w `Data/Layouts/`, osobno
od wyników Whispera).

Jedna instancja obsługuje **jedną aktywną książkę**. Wgranie nowych materiałów zastępuje poprzedni
projekt, ale jego pliki źródłowe (`.txt` i nagrania) są **przenoszone do `Data/Poprzedni_projekt/`**,
a nie kasowane — zawsze zostaje jeden poziom cofnięcia. Usuwane są wyłącznie dane pochodne
(cache transkrypcji i wyniki przetwarzania), które odtwarza ponowne uruchomienie.

---

## 🧭 Jak działa wykrywanie rozdziałów

Podział tekstu na rozdziały to najbardziej zawodny element całego procesu, bo pliki `.txt` z różnych
źródeł nie mają wspólnego formatu. Aplikacja stosuje trzy strategie, w tej kolejności:

1. **Nagłówki** — testuje kilka wzorców naraz i wybiera ten, który daje podział najbliższy liczbie
   nagrań. Rozpoznaje:
   - cyfry rzymskie i arabskie (`Rozdział XIV`, `Rozdział 7`),
   - **liczebniki słowne** (`Rozdział pierwszy`, `Rozdział dwudziesty drugi`),
   - nagłówki bez numeru (`Ostatni rozdział`, `Prolog`, `Epilog`, `Zakończenie`),
   - warianty `Część` / `Księga` / `Chapter`,
   - tytuł rozdziału w osobnej linii pod nagłówkiem.
2. **Uzgadnianie z audio** — gdy liczba nagłówków nie zgadza się z liczbą nagrań, aplikacja porównuje
   tempo czytania (znaki tekstu na sekundę nagrania) i wykrywa rozdziały sklejone w tekście.
   Punkt podziału znajduje **kotwicą**: transkrybuje pierwsze 45 sekund nadmiarowego nagrania
   i wyszukuje ten fragment w tekście.
3. **Kotwice dla całości** — gdy nagłówków nie ma wcale, cały podział powstaje z kotwic audio.

Wynik zawsze trafia do **kreatora**, gdzie widzisz pewność dopasowania każdego rozdziału
i możesz poprawić granice, zanim ruszy właściwe przetwarzanie.

> **Przykład z życia:** w wydaniu *Doktora Dolittle* ostatni rozdział nosi tytuł „Ostatni rozdział”
> zamiast numeru — tekst ma 20 nagłówków, a audio 21 plików. Aplikacja wykrywa rozbieżność po tempie
> czytania (rozdział 20 miałby 23,6 znaku/s przy średniej 13,5) i dzieli go w miejscu znalezionym kotwicą.

---

## 📦 Struktura paczki wyjściowej

```
Książka X/
├── Rozdział 01/
│   ├── Teksty/
│   │   ├── Tekst_zrodlowy_slashe.txt   # tekst ciągły z separatorem /// na granicach plansz
│   │   ├── Rozdzial_01.srt             # standardowe napisy SRT
│   │   └── Etykiety_Audacity.txt       # TSV: start ⇥ koniec ⇥ ETYKIETA
│   └── Audio/
│       ├── 001 - Co mi za Boże Narodzenie.mp3
│       └── ...                         # {numer:03d} - {pierwsze 5 słów}.mp3
├── Rozdział 02/
└── ...
```

Całość pakowana jest do `{Tytuł}.zip` w `Data/Output_Packages/`.

---

## ✨ Pozostałe funkcje

- **Precyzyjne dopasowanie (forced alignment)** — Faster-Whisper z akceleracją CUDA lub fallbackiem
  CPU; rozpoznane słowa mapowane są na **dokładny tekst książki**, co eliminuje halucynacje modelu.
- **Plansze pod format wideo** — do 11 linii (ok. 40–50 słów), łączenie kolejnych kwestii dialogowych,
  podział długich akapitów wyłącznie na granicach zdań. Limity konfigurowalne w UI.
- **Obsługa wtrąceń** — fragmenty spoza książki (wstęp lektora, stopka wydawnictwa) dostają status
  `intro_outro`; w interfejsie możesz przyjąć tekst Whispera, poprawić go inline albo odrzucić.
- **Zadania w tle** — transkrypcja, przetwarzanie i eksport nie blokują serwera; UI pokazuje pasek
  postępu, log na żywo i pozwala anulować zadanie.
- **Cache transkrypcji** — ponowne przetworzenie rozdziału po zmianie parametrów plansz nie wymaga
  ponownego uruchamiania Whispera.
- **Automatyczne kodowanie** — pliki `.txt` w UTF-8, CP1250 i ISO-8859-2 rozpoznawane są automatycznie;
  zakończenia linii normalizowane, by `\r` nie trafiał do napisów.

---

## 🚀 Uruchomienie z Dockerem

Aplikacja jest przeznaczona na maszyny z kartą **NVIDIA**.

**Wymagania hosta:** sterowniki NVIDIA, Docker oraz
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
Na Windows wystarczy Docker Desktop z backendem WSL2 i sterownik obsługujący WSL.

```bash
docker compose up -d --build
# http://localhost:8000
```

Obraz zawiera `cuBLAS` i `cuDNN`, więc waży ok. 5 GB. Wykryte urządzenie i pamięć karty
widać w prawym górnym rogu interfejsu.

Gdy port 8000 jest zajęty przez inną aplikację:

```bash
APP_PORT=8010 docker compose up -d
```

### Dobór modelu do pamięci karty

Interfejs ostrzega, gdy wybrany model nie zmieści się w VRAM. Orientacyjnie (float16):

| Model | VRAM | Uwagi |
|-------|------|-------|
| `tiny` / `base` | 0,5–0,7 GB | do szybkich testów |
| `small` | ~1 GB | **domyślny**, dobry kompromis |
| `medium` | ~2,5 GB | wyraźnie dokładniejszy |
| `large-v3` | ~4,7 GB | wymaga karty ≥ 6 GB |

Gdy model się nie zmieści, `ctranslate2` przerywa ładowanie, a aplikacja przechodzi na CPU —
przetwarzanie wtedy działa, ale jest kilkanaście razy wolniejsze.

### Uruchomienie bez GPU

Usuń sekcję `deploy` z `docker-compose.yml` i zbuduj lżejszy obraz (ok. 1,4 GB):

```bash
docker compose build --build-arg WITH_CUDA=false && docker compose up -d
```

### Sieci z przechwytywaniem TLS

Jeśli `pip install` przerywa build błędem `CERTIFICATE_VERIFY_FAILED`, w sieci działa
antywirus ze skanowaniem HTTPS albo firmowe proxy. Rozwiązanie opisuje `certs/README.md` —
wystarczy wrzucić tam certyfikat główny w formacie PEM. Pusty katalog nie zmienia nic
na maszynach bez takiego oprogramowania.

Bind-mount `./Data` sprawia, że materiały źródłowe, cache transkrypcji i gotowe paczki
zostają na hoście. Modele Whisper trafiają do nazwanego wolumenu `whisper_models`,
więc nie pobierają się ponownie przy każdej przebudowie kontenera.

> Projekt jest **przenośny między maszynami**: manifest `Data/project.json` zapisuje same
> nazwy plików, a pełne ścieżki są odtwarzane przy wczytaniu. Katalog `Data/` można
> skopiować na inny komputer albo podmontować pod dowolną ścieżką w kontenerze.

Przydatne polecenia:

```bash
docker compose logs -f          # log aplikacji
docker compose ps               # stan i healthcheck
docker compose down             # zatrzymanie
```

### Tryb deweloperski — praca nad kodem

Domyślny obraz ma kod **zapieczony w środku**, więc każda zmiana wymagałaby przebudowy.
Nakładka `docker-compose.dev.yml` montuje `Engine/`, `Interface/` i `Tests/` z dysku:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

Od tego momentu pętla pracy to:

1. edytujesz plik u siebie,
2. `docker compose restart` — kilka sekund, **bez** przebudowy obrazu,
3. odświeżasz przeglądarkę.

Przebudowa (`--build`) jest potrzebna wyłącznie po zmianie `requirements.txt` albo
`Dockerfile`. Wariant produkcyjny zostaje nietknięty — `docker compose up -d --build`
bez `-f` nadal uruchamia obraz z kodem w środku.

Zmiany w CSS i JS wchodzą po zwykłym odświeżeniu strony: serwer wysyła dla plików
statycznych `Cache-Control: no-cache`, więc przeglądarka za każdym razem pyta o nowszą
wersję (przy niezmienionym pliku dostaje 304, więc nic to nie kosztuje).

> `Interface/server.py` obsługuje też `RELOAD=1` (przeładowanie po zapisie pliku, bez
> `restart`). Jest **domyślnie wyłączone i niezweryfikowane** — na Windows potrafi się
> zawiesić. `restart` jest przewidywalny i działa wszędzie.

Testy w kontenerze:

```bash
docker compose exec najszybsze-lektury python -m pytest Tests -q
```

## 💻 Uruchomienie lokalne

```bash
pip install -r requirements.txt
python Interface/server.py
# http://127.0.0.1:8000
```

Wymagany **ffmpeg** w `PATH` (cięcie audio i próbki do kotwic).

---

## ⌨️ Tryb wiersza poleceń

Przydatny do przetwarzania wsadowego, gdy materiały leżą już w `Data/`:

```bash
python -m Engine.pipeline --map                 # wykryj i zapisz mapę rozdziałów
python -m Engine.pipeline --all                 # przetwórz wszystkie rozdziały
python -m Engine.pipeline --chapter 3           # pojedynczy rozdział
python -m Engine.pipeline --export              # zbuduj paczkę ZIP
python -m Engine.pipeline --export --no-audio   # eksport bez cięcia MP3
python -m Engine.pipeline --all --model large-v3 --no-cache
```

---

## 📂 Struktura projektu

```
Najszybsze Lektury/
├── Data/
│   ├── project.json         # manifest aktywnego projektu (tytuł, mapa rozdziałów, stan)
│   ├── Text/                # źródłowy .txt
│   ├── Audio/               # źródłowe nagrania
│   ├── Cache_Transcripts/   # cache transkrypcji Whispera
│   ├── Processed_JSON/      # wyniki per rozdział (plansze, znaczniki, raport)
│   └── Output_Packages/     # gotowe paczki i archiwa ZIP
│
├── Engine/
│   ├── project.py           # manifest projektu, zapis źródeł, wykrywanie kodowania
│   ├── jobs.py              # kolejka zadań w tle z postępem i anulowaniem
│   ├── text_parser.py       # akapity, dialogi, budowa rozdziałów z mapy granic
│   ├── chapter_matcher.py   # wykrywanie rozdziałów: nagłówki + kotwice audio
│   ├── transcriber.py       # Faster-Whisper, cache, próbki do kotwic
│   ├── aligner.py           # dopasowanie sekwencyjne + wykrywanie wtrąceń
│   ├── chunker.py           # podział startowy na plansze (propozycja automatu)
│   ├── layout.py            # PODZIAŁ JAKO EDYTOWALNY BYT: granice, korekty, wstawki
│   ├── timing.py            # czasy cięć z podziału + ciszy w nagraniu
│   ├── audio_analysis.py    # obwiednia, wykrywanie ciszy, fala do rysowania
│   ├── srt_writer.py        # generowanie SRT
│   ├── exporter.py          # /// , etykiety Audacity, cięcie MP3, ZIP
│   └── pipeline.py          # orkiestrator + CLI
│
├── Interface/
│   ├── server.py            # API FastAPI
│   └── static/
│       ├── index.html       # powłoka sześciu widoków
│       ├── setup.js         # projekt, upload, kreator, zadania, eksport
│       ├── app.js           # studio weryfikacji
│       ├── editor.js        # montaż: granice plansz i punkty cięcia
│       ├── style.css        # warstwa bazowa
│       ├── setup.css        # warstwa produkcyjna
│       ├── editor.css       # ekrany montażu
│       ├── theme.css        # motyw „Bajka" — wczytywany OSTATNI, nadpisuje resztę
│       └── favicon*, logo*  # znak marki (generowane z brand/)
│
├── Tests/                   # pytest; uruchomienie: python -m pytest Tests -q
│   ├── test_layout.py       # tokenizacja, granice, operacje edycyjne
│   ├── test_timing.py       # cięcia, dosuwanie do ciszy, kolejność
│   ├── test_inserts.py      # wstawki i korekty tekstu
│   ├── test_board_identity.py  # KTÓRA plansza dostaje poprawkę (cut_key)
│   └── test_persistence.py  # trwałość korekt i zapisu podziału
│
├── brand/
│   ├── logo-source.jpg      # źródło znaku marki
│   └── build_assets.py      # generuje favicony i logo do Interface/static/
│
├── Dockerfile
├── docker-compose.yml       # wariant produkcyjny (kod w obrazie)
├── docker-compose.dev.yml   # nakładka deweloperska (kod z dysku)
└── PROJECT_PLAN.md
```

---

## 🔌 Najważniejsze endpointy

| Metoda | Ścieżka | Opis |
|--------|---------|------|
| `GET` | `/api/project` | Stan projektu, ustawienia, wykryte urządzenie |
| `POST` | `/api/upload` | Wgranie `.txt` + N plików audio (multipart) |
| `POST` | `/api/chapters/detect` | Zadanie: wykrycie podziału na rozdziały |
| `GET` | `/api/chapters/proposal` | Propozycja mapy do zatwierdzenia w kreatorze |
| `POST` | `/api/chapters/map` | Zapis zatwierdzonej mapy |
| `POST` | `/api/process` | Zadanie: przetworzenie rozdziałów (`{chapters, use_cache}`) |
| `POST` | `/api/export` | Zadanie: budowa paczki (`{book_name, slice_audio}`) |
| `GET` | `/api/export/download` | Pobranie gotowego ZIP-a |
| `GET` | `/api/jobs/{id}` | Postęp, log i wynik zadania |

---

## ⌨️ Skróty klawiszowe (widok Weryfikacja)

<kbd>Spacja</kbd> start/pauza · <kbd>←</kbd>/<kbd>→</kbd> ±5 s · <kbd>R</kbd> powtórz wycinek ·
<kbd>1</kbd> zgodny · <kbd>2</kbd> błędne dopasowanie · <kbd>3</kbd> brak w źródle ·
<kbd>N</kbd> losuj kolejną planszę
