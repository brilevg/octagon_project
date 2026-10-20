"""
Фоновый worker анализа качества сообщений через YandexGPT.

Модуль независим от main.py, моделей SQLAlchemy и конкретной реализации
репозитория. Связь с БД передаётся извне через repository_factory.

Ожидаемый интерфейс репозитория:
    enqueue_revision_jobs(limit: int, pipeline_version: str) -> int
    claim_revision_jobs(limit: int, pipeline_version: str) -> list[dict]
    save_quality_findings(job_id: int, findings: list[dict],
                          model: str, pipeline_version: str) -> None
    fail_analysis_job(job_id: int, error: str, max_attempts: int,
                      retry_delay_seconds: int) -> None
    close() -> None

Каждая задача из claim_revision_jobs должна содержать:
    job_id, revision_id, text, pipeline_version
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """
Ты анализируешь качество сообщений в корпоративных рабочих чатах.
Оценивай только текст сообщения и его явный контекст. Не делай выводов
о личности, характере, мотивации или общей эффективности сотрудника.
Не оценивай скорость ответа: для этого нужна отдельная метрика.
Не считай несогласие, критику или деловой прямой стиль нарушением сами по себе.

Верни только JSON-объект формата:
{
  "findings": [
    {
      "category": "correctness|politeness|toxicity|communication_style|corporate_policy|clarity|privacy_risk",
      "severity": "low|medium|high",
      "confidence": 0.0,
      "evidence": "точная цитата из исходного сообщения",
      "explanation": "краткое объяснение проблемы",
      "recommendation": "практичная рекомендация"
    }
  ]
}

Если значимых проблем нет, верни {"findings": []}.
Поле evidence должно дословно присутствовать в исходном сообщении.
Не выдумывай правила компании, которых нет в предоставленном тексте.
Результат является кандидатом на проверку человеком, а не окончательным
заключением о сотруднике.
""".strip()


class Repository(Protocol):
    def enqueue_revision_jobs(self, limit: int, pipeline_version: str) -> int: ...
    def claim_revision_jobs(self, limit: int, pipeline_version: str) -> list[dict[str, Any]]: ...
    def save_quality_findings(
        self, job_id: int, findings: list[dict[str, Any]],
        model: str, pipeline_version: str
    ) -> None: ...
    def fail_analysis_job(
        self, job_id: int, error: str, max_attempts: int,
        retry_delay_seconds: int
    ) -> None: ...
    def close(self) -> None: ...
    def recover_stale_analysis_jobs(
    self,
    stale_after_minutes: int = 15,
    max_attempts: int = 5,
) -> int: ...


class YandexGPTClient:
    """Минимальный HTTP-клиент YandexGPT на стандартной библиотеке Python."""

    ENDPOINT = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

    def __init__(self) -> None:
        self.api_key = os.getenv("YANDEX_API_KEY", "").strip()
        self.folder_id = os.getenv("YANDEX_FOLDER_ID", "").strip()
        self.model = os.getenv("YANDEX_MODEL", "yandexgpt/latest").strip()
        self.timeout = int(os.getenv("YANDEX_API_TIMEOUT", "60"))

        if not self.api_key:
            raise RuntimeError("Не задана переменная окружения YANDEX_API_KEY")
        if not self.folder_id:
            raise RuntimeError("Не задана переменная окружения YANDEX_FOLDER_ID")

    def analyze(self, text: str) -> list[dict[str, Any]]:
        payload = {
            "modelUri": f"gpt://{self.folder_id}/{self.model}",
            "completionOptions": {
                "stream": False,
                "temperature": 0.0,
                "maxTokens": "2000",
            },
            "messages": [
                {"role": "system", "text": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "text": json.dumps(
                        {"message": text},
                        ensure_ascii=False,
                    ),
                },
            ],
        }

        request = urllib.request.Request(
            self.ENDPOINT,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Api-Key {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response_data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"YandexGPT HTTP {exc.code}: {details[:1500]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Ошибка соединения с YandexGPT: {exc}") from exc

        try:
            answer = response_data["result"]["alternatives"][0]["message"]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"Неожиданный ответ YandexGPT: {str(response_data)[:1500]}"
            ) from exc

        return self._parse_findings(answer, text)

    @staticmethod
    def _parse_findings(answer: str, original_text: str) -> list[dict[str, Any]]:
        answer = answer.strip()
        if answer.startswith("```"):
            lines = answer.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            answer = "\n".join(lines).strip()
        if not answer:
            raise RuntimeError("YandexGPT вернул пустой ответ")

        refusal_markers = (
            "я не могу обсуждать эту тему",
            "я не могу помочь с этим запросом",
            "i cannot discuss this topic",
            "i cannot help with this request",
        )

        if any(marker in answer.lower() for marker in refusal_markers):
            raise RuntimeError(
                f"YandexGPT отказался анализировать сообщение: {answer[:500]}"
            )

        try:
            parsed = json.loads(answer)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"YandexGPT вернул невалидный JSON: {answer[:1000]}"
            ) from exc
        try:
            parsed = json.loads(answer)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"YandexGPT вернул невалидный JSON: {answer[:1000]}"
            ) from exc

        raw_findings = parsed.get("findings")
        if not isinstance(raw_findings, list):
            raise RuntimeError("В ответе YandexGPT отсутствует массив findings")

        allowed_categories = {
            "correctness",
            "politeness",
            "toxicity",
            "communication_style",
            "corporate_policy",
            "clarity",
            "privacy_risk",
        }
        allowed_severities = {"low", "medium", "high"}
        findings: list[dict[str, Any]] = []

        for item in raw_findings:
            if not isinstance(item, dict):
                continue

            category = item.get("category")
            severity = item.get("severity")
            evidence = item.get("evidence")
            explanation = item.get("explanation")
            recommendation = item.get("recommendation")
            confidence = item.get("confidence")

            if category not in allowed_categories:
                continue
            if severity not in allowed_severities:
                continue
            if not isinstance(evidence, str) or not evidence:
                continue
            if evidence not in original_text:
                logger.warning("Пропущено замечание: цитата отсутствует в сообщении")
                continue
            if not isinstance(explanation, str) or not explanation.strip():
                continue

            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                continue
            if not 0.0 <= confidence <= 1.0:
                continue

            findings.append({
                "category": category,
                "severity": severity,
                "confidence": confidence,
                "evidence": evidence,
                "explanation": explanation.strip(),
                "recommendation": (
                    recommendation.strip()
                    if isinstance(recommendation, str) and recommendation.strip()
                    else None
                ),
            })

        return findings


class QualityWorker:
    """Периодически ставит ревизии в очередь и обрабатывает задачи."""

    def __init__(
        self,
        repository_factory: Callable[[], Repository],
        *,
        poll_interval: int = 5,
        batch_size: int = 5,
        pipeline_version: str = "v1",
        max_attempts: int = 5,
        retry_delay_seconds: int = 60,
        client: YandexGPTClient | None = None,
    ) -> None:
        if poll_interval < 1:
            raise ValueError("poll_interval должен быть не меньше 1")
        if batch_size < 1:
            raise ValueError("batch_size должен быть не меньше 1")

        self.repository_factory = repository_factory
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self.pipeline_version = pipeline_version
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.client = client or YandexGPTClient()

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            logger.warning("QualityWorker уже запущен")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="quality-worker",
            daemon=True,
        )
        self._thread.start()
        logger.info("QualityWorker запущен")

    def stop(self, timeout: float = 30) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        if self._thread and self._thread.is_alive():
            logger.warning("QualityWorker не завершился за %.1f секунд", timeout)
        else:
            logger.info("QualityWorker остановлен")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            repository: Repository | None = None
            try:
                # Фабрика вызывается внутри потока: сессия БД не разделяется
                # с main.py, сборщиком или административным ботом.
                repository = self.repository_factory()
                repository.recover_stale_analysis_jobs(
                    stale_after_minutes=15,
                    max_attempts=self.max_attempts,
                )
                repository.enqueue_revision_jobs(
                    limit=500,
                    pipeline_version=self.pipeline_version,
                )
                jobs = repository.claim_revision_jobs(
                    limit=self.batch_size,
                    pipeline_version=self.pipeline_version,
                )

                # Уже захваченные задачи обрабатываем до конца, даже если
                # пришёл сигнал остановки: иначе оставшиеся задачи batch
                # зависнут в статусе processing.
                for job in jobs:
                    self._process_job(repository, job)

                if not jobs:
                    self._stop_event.wait(self.poll_interval)

            except Exception:
                logger.exception("Ошибка рабочего цикла QualityWorker")
                self._stop_event.wait(self.poll_interval)
            finally:
                if repository is not None:
                    try:
                        repository.close()
                    except Exception:
                        logger.exception("Не удалось закрыть репозиторий worker")

    def _process_job(self, repository: Repository, job: dict[str, Any]) -> None:
        job_id = int(job["job_id"])
        text = job.get("text")

        try:
            if not isinstance(text, str) or not text.strip():
                # Пустые сообщения не должны отправляться в LLM.
                repository.save_quality_findings(
                    job_id=job_id,
                    findings=[],
                    model=self.client.model,
                    pipeline_version=self.pipeline_version,
                )
                return

            findings = self.client.analyze(text)
            repository.save_quality_findings(
                job_id=job_id,
                findings=findings,
                model=self.client.model,
                pipeline_version=self.pipeline_version,
            )
            logger.info(
                "Задача анализа %s завершена: замечаний %s",
                job_id,
                len(findings),
            )

        except Exception as exc:
            logger.exception("Ошибка анализа задачи %s", job_id)
            try:
                repository.fail_analysis_job(
                    job_id=job_id,
                    error=str(exc)[:4000],
                    max_attempts=self.max_attempts,
                    retry_delay_seconds=self.retry_delay_seconds,
                )
            except Exception:
                logger.exception(
                    "Не удалось отметить задачу %s как неуспешную", job_id
                )
