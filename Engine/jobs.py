"""
Prosta kolejka zadan w tle dla dlugotrwalych operacji (transkrypcja, przetwarzanie, eksport).

Jeden watek roboczy przetwarza zadania sekwencyjnie - modele Whisper i tak nie skaluja sie
liniowo przy rownoleglych wywolaniach, a sekwencyjnosc daje przewidywalny postep w UI.
"""
import uuid
import queue
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

PENDING = "pending"
RUNNING = "running"
DONE = "done"
ERROR = "error"
CANCELLED = "cancelled"

MAX_LOG_LINES = 400
MAX_FINISHED_JOBS = 30


class JobCancelled(Exception):
    """Podnoszone wewnatrz zadania, gdy uzytkownik zazadal anulowania."""


@dataclass
class Job:
    id: str
    kind: str
    label: str
    status: str = PENDING
    progress: float = 0.0
    message: str = "Oczekuje w kolejce..."
    log: List[str] = field(default_factory=list)
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "progress": round(self.progress, 4),
            "message": self.message,
            "log": self.log[-60:],
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class JobHandle:
    """Uchwyt przekazywany do funkcji zadania: raportowanie postepu i kontrola anulowania."""

    def __init__(self, job: Job, lock: threading.Lock):
        self._job = job
        self._lock = lock

    @property
    def id(self) -> str:
        return self._job.id

    def check_cancelled(self) -> None:
        if self._job._cancel.is_set():
            raise JobCancelled()

    @property
    def cancelled(self) -> bool:
        return self._job._cancel.is_set()

    def progress(self, value: float, message: Optional[str] = None) -> None:
        self.check_cancelled()
        with self._lock:
            self._job.progress = max(0.0, min(1.0, float(value)))
            if message:
                self._job.message = message
                self._append_log(message)

    def log(self, message: str) -> None:
        with self._lock:
            self._job.message = message
            self._append_log(message)

    def _append_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self._job.log.append(f"[{stamp}] {message}")
        if len(self._job.log) > MAX_LOG_LINES:
            del self._job.log[:-MAX_LOG_LINES]


class JobManager:
    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._order: List[str] = []
        self._lock = threading.Lock()
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._functions: Dict[str, Callable[[JobHandle], Any]] = {}
        self._worker = threading.Thread(target=self._run_worker, name="job-worker", daemon=True)
        self._worker.start()

    def submit(self, kind: str, label: str, fn: Callable[[JobHandle], Any]) -> Job:
        job_id = uuid.uuid4().hex[:12]
        job = Job(
            id=job_id,
            kind=kind,
            label=label,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        with self._lock:
            self._jobs[job_id] = job
            self._order.append(job_id)
            self._functions[job_id] = fn
        self._queue.put(job_id)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [self._jobs[j].to_dict() for j in self._order if j in self._jobs]

    def active(self) -> Optional[Dict[str, Any]]:
        """Zwraca pierwsze zadanie oczekujace lub w trakcie wykonania."""
        with self._lock:
            for job_id in self._order:
                job = self._jobs.get(job_id)
                if job and job.status in (PENDING, RUNNING):
                    return job.to_dict()
        return None

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status in (DONE, ERROR, CANCELLED):
                return False
            job._cancel.set()
            if job.status == PENDING:
                job.status = CANCELLED
                job.message = "Anulowano przed uruchomieniem."
                job.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            else:
                job.message = "Anulowanie w toku..."
            return True

    # ---------- watek roboczy ----------

    def _run_worker(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:
                break
            with self._lock:
                job = self._jobs.get(job_id)
                fn = self._functions.pop(job_id, None)
            if not job or not fn:
                continue
            if job._cancel.is_set():
                continue

            handle = JobHandle(job, self._lock)
            with self._lock:
                job.status = RUNNING
                job.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                job.message = "Uruchomiono..."

            try:
                result = fn(handle)
                with self._lock:
                    job.status = DONE
                    job.progress = 1.0
                    job.result = result if isinstance(result, dict) else {"value": result}
                    job.message = "Zakonczono pomyslnie."
            except JobCancelled:
                with self._lock:
                    job.status = CANCELLED
                    job.message = "Zadanie anulowane przez uzytkownika."
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
                with self._lock:
                    job.status = ERROR
                    job.error = detail
                    job.message = f"Blad: {detail}"
            finally:
                with self._lock:
                    job.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self._prune()

    def _prune(self) -> None:
        """Ogranicza historie zakonczonych zadan, by pamiec nie rosla w nieskonczonosc."""
        with self._lock:
            finished = [j for j in self._order
                        if self._jobs.get(j) and self._jobs[j].status in (DONE, ERROR, CANCELLED)]
            excess = len(finished) - MAX_FINISHED_JOBS
            for job_id in finished[:max(0, excess)]:
                self._jobs.pop(job_id, None)
                self._order.remove(job_id)
