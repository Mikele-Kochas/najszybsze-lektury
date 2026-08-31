# Plan architektoniczny: System Generowania Pakietów Lektur

Dokument opisuje architekturę aplikacji po przejściu z etapu Proof of Concept do wersji
przyjmującej dane z zewnątrz. Stan na moment ostatniej aktualizacji: **zrealizowane**.

---

## 1. Model danych: jeden aktywny projekt

Aplikacja obsługuje jedną książkę naraz. Stan trzyma manifest `Data/project.json`:

```jsonc
{
  "title": "Doktor Dolittle i jego zwierzęta",
  "author": "Hugh Lofting",
  "text_file": "…/Data/Text/Dolittle.txt",
  "audio_files": ["…/Data/Audio/…_001_rozdzial-i.mp3", "…"],
  "audio_mode": "multi",              // jeden plik audio na rozdział
  "settings": { "model_size": "small", "device": "auto", "max_lines_per_board": 11, … },
  "status":   { "uploaded": true, "mapped": true, "processed": true, "exported": true },
  "chapter_map": [ { "chapter_num": 1, "text_start": 106, "text_end": 22567,
                     "audio_file": "…", "confidence": 1.0, "source": "headings:rozdzial_named" } ]
}
```

`chapter_map` jest **jedynym źródłem prawdy** o podziale książki. Wszystko poniżej —
parsowanie akapitów, chunking, alignment, eksport — czyta granice stąd, a nie z regexów
uruchamianych ad hoc. Zmiana mapy unieważnia wyniki przetwarzania (`clear_derived()`).

Wgranie nowych materiałów zastępuje projekt. Manifest jest odtwarzany z zawartości katalogów,
jeśli zniknie — dzięki temu materiały skopiowane ręcznie do `Data/` nadal działają.

**Podmiana projektu nie kasuje źródeł.** `clear_sources()` przenosi `Text/` i `Audio/` do
`Data/Poprzedni_projekt/` wraz z kopią manifestu; usuwane są tylko dane pochodne (cache, JSON),
które odtwarza ponowne przetworzenie. Operacja, która jednym kliknięciem niszczyłaby nagrania
całej książki, jest nie do przyjęcia — użytkownik może nie mieć ich nigdzie indziej.
Trzymany jest jeden poziom cofnięcia; UI pokazuje, co czeka w archiwum.

---

## 2. Wejście: upload zamiast ręcznego kopiowania plików

`POST /api/upload` (multipart) przyjmuje **jeden `.txt`** i **N plików audio**.

- Pliki zapisywane są strumieniowo (bloki po 1 MB), bez wciągania całości do pamięci —
  komplet nagrań książki to często kilkaset MB.
- Rozszerzenia spoza listy dozwolonych są odrzucane (np. `informacje.txt` dołączany
  do paczek Wolnych Lektur).
- Kodowanie `.txt` wykrywane automatycznie: UTF-8 (samowalidujące), dalej CP1250 / ISO-8859-2 /
  CP852 wybierane wg liczby poprawnych polskich znaków.
- Zakończenia linii normalizowane do `\n` — inaczej `\r` z plików CRLF trafia do tytułów
  rozdziałów, a stamtąd do SRT i etykiet Audacity.

---

## 3. Podział na rozdziały: hybryda z kreatorem

Najbardziej zawodny etap, bo formaty plików `.txt` nie są ustandaryzowane.

### 3.1 Wykrywanie nagłówków
Kilka wzorców testowanych równolegle; wybierany ten o najwyższym wyniku funkcji oceniającej,
która premiuje: zgodność liczby rozdziałów z liczbą nagrań (waga 3.0), rosnącą i unikalną
numerację (1.5) oraz równomierne odstępy, a karze rozdziały krótsze niż 400 znaków.

Rozpoznawane warianty: cyfry rzymskie i arabskie, **liczebniki słowne** (`pierwszy` …
`pięćdziesiąty dziewiąty`, rodzaj męski i żeński), nagłówki bez numeru (`Ostatni rozdział`,
`Prolog`, `Epilog`, `Zakończenie`, `Posłowie`), `Część` / `Księga` / `Chapter`, a także tytuł
umieszczony w osobnej linii pod nagłówkiem.

### 3.2 Uzgadnianie z audio (`reconcile_with_audio`)
Gdy liczba nagłówków ≠ liczba nagrań:

1. Wyliczane jest globalne tempo czytania `rate = suma_znaków / suma_czasu_nagrań`.
2. Nagrania przypisywane są rozdziałom zachłannie: kolejny plik dołączany jest do rozdziału
   tylko wtedy, gdy **zbliża** sumaryczny czas do oczekiwanego (`długość_tekstu / rate`)
   i gdy zostaje dość plików dla pozostałych rozdziałów.
3. Rozdział obsługiwany przez k > 1 nagrań jest cięty w tekście. Punkt cięcia wyznacza
   **kotwica**: pierwsze 45 s nadmiarowego nagrania jest transkrybowane (ffmpeg wycina
   fragment, więc koszt jest znikomy) i wyszukiwane w tekście algorytmem nasion + `difflib`.
4. Gdy kotwica zawiedzie (pewność < 0.45), podział jest proporcjonalny do czasu nagrań,
   a użytkownik dostaje ostrzeżenie.

> Przypadek rzeczywisty: *Doktor Dolittle* — 20 nagłówków w tekście, 21 nagrań. Rozdział 20
> miał 23,6 znaku/s przy średniej 13,5, co ujawniło sklejenie. Kotwica wskazała granicę
> z pewnością 0.85 na nagłówku „Ostatni rozdział”, którego nie obejmował żaden wzorzec numeryczny.

### 3.3 Kreator w UI
Propozycja **zawsze** trafia do tabeli, w której widać nagłówek, przypisane nagranie, zakres
znaków, pewność dopasowania i podgląd fragmentu tekstu. Każde pole jest edytowalne; zapis
oznacza wpis jako `source: "manual"`.

---

## 4. Przetwarzanie: zadania w tle

Transkrypcja całej książki to minuty (GPU) lub godziny (CPU), więc żadna z tych operacji nie może
blokować requestu HTTP.

`Engine/jobs.py` udostępnia jednowątkową kolejkę: `submit()` zwraca `job_id`, worker wykonuje
zadania sekwencyjnie (modele Whisper i tak nie skalują się liniowo równolegle), a `JobHandle`
pozwala raportować postęp i sprawdzać żądanie anulowania. UI odpytuje `GET /api/jobs/{id}`
co sekundę i pokazuje pasek postępu, komunikat oraz log.

Postęp transkrypcji liczony jest z pozycji czasowej segmentów zwracanych leniwie przez
faster-whisper (`seg.end / czas_trwania`), więc pasek rusza od razu, a nie dopiero na końcu.

Serwer dopuszcza jedno zadanie naraz (`require_no_active_job`) — równoległy upload w trakcie
przetwarzania mógłby podmienić pliki spod działającego procesu.

---

## 5. Obsługa wtrąceń i edycja plansz

Fragmenty czytane przez lektora, a nieobecne w książce (wstęp, stopka wydawnictwa), otrzymują
w alignerze typ `intro_outro` i tekst zastępczy `(brak tekstu w pliku źródłowym)`. W interfejsie
plansza jest wyróżniona i daje trzy akcje: przyjęcie tekstu Whispera, edycję inline lub odrzucenie.

`POST /api/chunk/edit` aktualizuje JSON rozdziału i od razu odświeża pliki tekstowe.
Cięcie audio **nie** jest wtedy powtarzane — przy każdej edycji byłoby to nieproporcjonalnie
kosztowne; robi je dopiero eksport paczki.

---

## 5a. Granice plansz i punkty cięcia

Timestampy z alignmentu są zbyt niedokładne, żeby ciąć po nich MP3: Whisper kończy słowo
systematycznie za wcześnie, a zaczyna następne za późno, więc eksport ucinał końcówki wyrazów.
Pomiar na całej książce: **4 z 678 granic wypadały w ciszy**, mediana zapasu 0 ms.

Rozwiązanie rozbija dawny łańcuch `chunker → aligner → plansze` na trzy warstwy:

```
chapter_NNN.json   words[]   czasy pojedynczych słów      (drogie: Whisper + alignment)
Layouts/NNN.json   breaks[]  gdzie w tekście są granice   (decyzja użytkownika)
                   overrides ręczne czasy cięć
audio_analysis     silences  gdzie w nagraniu jest cisza  (liczone raz, cache)
```

Plansze są **widokiem pochodnym** tych trzech rzeczy. Przesunięcie granicy to przeliczenie
tablicy, nie ponowna transkrypcja — bez tego ręczna korekta 21 rozdziałów byłaby nie do przejścia.

Kolejność pierwszeństwa przy ustalaniu czasu cięcia: ręczne ustawienie → środek najbliższej
ciszy → środek luki między słowami. Po dosunięciu **619 z 678 granic ma po 80 ms ciszy z obu
stron**, mediana zapasu 200 ms; każdy koniec planszy wypada nie wcześniej niż przedtem.

Dwa ekrany obsługują to, czego automat nie domyka:

- **Granice** — tekst rozdziału ze znacznikami podziału; klik dzieli, przycisk scala,
  uchwyt przesuwa. Na czerwono granice, przez które lektor czyta bez przerwy (91 z 655) —
  tam żadne przesunięcie czasu nie pomoże, trzeba scalić plansze albo przenieść granicę.
- **Cięcia** — fala dźwiękowa z przeciąganym punktem cięcia, dociąganiem do ciszy, korektą
  co 10 ms i odsłuchem styku. Cisze, wypowiedzi Whispera i granice słów są narysowane na fali.

Znacznik „przejrzane" jest trwały — przy kilkuset cięciach użytkownik musi wiedzieć,
gdzie skończył.

---

## 6. Eksport

```
Książka X/
├── Rozdział 01/
│   ├── Teksty/
│   │   ├── Tekst_zrodlowy_slashe.txt   # plansze rozdzielone wierszem "///"
│   │   ├── Rozdzial_01.srt
│   │   └── Etykiety_Audacity.txt       # start ⇥ koniec ⇥ ETYKIETA (5 pierwszych słów, wersaliki)
│   └── Audio/
│       └── {NNN} - {pierwsze 5 słów}.mp3
└── …
```

Katalog `Audio/` rozdziału jest czyszczony przed ponownym cięciem — bez tego po zmniejszeniu
liczby plansz w paczce zostawałyby osierocone pliki z poprzedniego przebiegu.

---

## 7. Mapa modułów

| Moduł | Odpowiedzialność |
|-------|------------------|
| `Engine/project.py` | Manifest, zapis źródeł, wykrywanie kodowania, sanityzacja nazw |
| `Engine/jobs.py` | Kolejka zadań w tle: postęp, log, anulowanie |
| `Engine/chapter_matcher.py` | Wykrywanie nagłówków, kotwice audio, uzgadnianie liczby rozdziałów |
| `Engine/text_parser.py` | Akapity, dialogi, zdania; budowa rozdziałów z mapy granic |
| `Engine/chunker.py` | Podział na plansze (limit linii, grupowanie dialogów) |
| `Engine/transcriber.py` | Faster-Whisper, wykrywanie GPU, cache, próbki do kotwic |
| `Engine/aligner.py` | Dopasowanie sekwencyjne słów, interpolacja luk, wykrywanie wtrąceń |
| `Engine/audio_analysis.py` | Obwiednia RMS, wykrywanie ciszy, dosuwanie cięć, peaki fali |
| `Engine/layout.py` | Edytowalny podział na plansze: tokeny, granice, ręczne czasy |
| `Engine/timing.py` | Timestampy z podziału: kolejność źródeł, diagnostyka granic |
| `Engine/exporter.py` | `///`, etykiety Audacity, cięcie MP3, archiwum ZIP |
| `Engine/pipeline.py` | Orkiestrator + CLI |
| `Interface/server.py` | API FastAPI |
| `Interface/static/setup.js` | Projekt, upload, kreator, zadania, eksport |
| `Interface/static/app.js` | Studio weryfikacji jakości |
| `Interface/static/editor.js` | Ekrany granic plansz i punktów cięcia |

---

## 8. Wdrożenie w kontenerze

Obraz zakłada obecność karty **NVIDIA** — `docker-compose.yml` rezerwuje GPU, a Dockerfile
domyślnie instaluje `cuBLAS` i `cuDNN` (build arg `WITH_CUDA`, obraz rośnie z 1,4 GB do 5,3 GB).
Bez tych bibliotek `ctranslate2` przerywa ładowanie modelu, a aplikacja schodzi na CPU.

Rzeczy, które trzeba było rozwiązać, żeby obraz był naprawdę przenośny:

| Problem | Skutek | Rozwiązanie |
|---|---|---|
| `uvicorn` na `127.0.0.1` | mapowanie portu bezużyteczne, aplikacja nieosiągalna z hosta | `HOST`/`PORT` ze zmiennych środowiskowych, w compose `0.0.0.0` |
| Bezwzględne ścieżki w manifeście | projekt nie otwiera się po zamontowaniu `Data/` pod inną ścieżką | manifest trzyma same nazwy plików, pełne ścieżki odtwarzane przy wczytaniu |
| Sztywny port 8000 | kolizja z inną aplikacją blokuje start | `${APP_PORT:-8000}` |
| Przechwytywanie TLS (antywirus, proxy) | `pip install` przerywa build | opcjonalny katalog `certs/` + `update-ca-certificates` |
| `certifi` nie widzi systemowego CA | pobranie modelu Whispera zawodzi mimo poprawnego korzenia | `SSL_CERT_FILE` i `REQUESTS_CA_BUNDLE` wskazują systemowy bundle |
| Model większy niż VRAM | ciche zejście na CPU wygląda jak „bardzo wolno” | interfejs porównuje wymagania modelu z `nvidia-smi` i ostrzega |

Katalog `Data/` jest bind-mountem hosta, a modele Whispera trafiają do nazwanego wolumenu,
więc przebudowa kontenera nie wymusza ponownego pobierania modelu.

---

## 9. Znane ograniczenia

- **Jedna książka na instancję.** Wgranie nowych materiałów kasuje poprzedni projekt.
  Przejście na wiele projektów wymaga rozbicia `PipelineManager` na instancję per projekt.
- **Jedno zadanie naraz.** Świadome uproszczenie — chroni przed podmianą plików w trakcie pracy.
- **Eksport bez cięcia audio** pozostawia pliki z poprzedniego przebiegu; jeśli granice plansz
  się zmieniły, trzeba wyeksportować ponownie z zaznaczonym cięciem.
- **Podział zapisany dla innej wersji tekstu** jest odrzucany po odcisku sekwencji słów
  i zastępowany propozycją automatu — ręczna praca nad rozdziałem przepada, jeśli zmieni się
  mapa rozdziałów albo plik źródłowy.
- **Podział jednego długiego MP3** na rozdziały nie jest obsługiwany — założeniem jest
  jedno nagranie na rozdział.
