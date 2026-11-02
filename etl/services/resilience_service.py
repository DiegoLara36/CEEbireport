import json
import logging
import smtplib
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import connection
from django.utils import timezone


logger = logging.getLogger(__name__)


def read_settings(cursor) -> dict[str, str]:
    cursor.execute("SELECT key, value FROM system_setting;")
    return {key: value for key, value in cursor.fetchall()}


def setting_int(values: dict[str, str], key: str, default: int) -> int:
    raw = values.get(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"system_setting.{key} debe ser un entero, se recibió {raw!r}") from exc


def setting_bool(values: dict[str, str], key: str, default: bool = False) -> bool:
    raw = values.get(key)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError(f"system_setting.{key} debe ser booleano, se recibió {raw!r}")
    return normalized in {"true", "1", "yes"}


class ResilienceService:
    def succeeded(self, cursor, source_id: int) -> None:
        values = read_settings(cursor)
        cursor.execute("SELECT frequency_days FROM source WHERE id = %s;", (source_id,))
        frequency = cursor.fetchone()[0]
        now = timezone.now()
        cursor.execute(
            """
            UPDATE source
               SET health_status = 'HEALTHY',
                   consecutive_failures = 0,
                   last_success_at = %s,
                   next_run_at = %s
             WHERE id = %s;
            """,
            (now, now + timedelta(days=frequency), source_id),
        )
        cursor.execute(
            """
            UPDATE alert
               SET resolved_at = %s
             WHERE resolved_at IS NULL
               AND entity_ref = (SELECT code FROM source WHERE id = %s);
            """,
            (now, source_id),
        )

    def failed(
        self,
        cursor,
        source_id: int,
        ingestion_id: int,
        error_type: str,
        error_message: str,
    ) -> None:
        values = read_settings(cursor)
        retry_hours = setting_int(values, "retry_interval_hours", 8)
        threshold = setting_int(values, "consecutive_failures_threshold", 3)
        now = timezone.now()
        cursor.execute(
            """
            UPDATE source
               SET consecutive_failures = consecutive_failures + 1
             WHERE id = %s
         RETURNING code, name, consecutive_failures, frequency_days, last_success_at;
            """,
            (source_id,),
        )
        source_code, source_name, failures, frequency_days, last_success = cursor.fetchone()
        if failures < threshold:
            health_status = "RETRYING"
            next_run = now + timedelta(hours=retry_hours)
            severity = "WARN"
            alert_type = error_type
            message = (
                f"{source_code}: {error_message}. Reintento programado en "
                f"{retry_hours} horas."
            )
        else:
            is_dom_failure = error_type in {"DOM_STRUCTURE_CHANGED", "ZERO_RESULTS"}
            health_status = "BROKEN_DOM" if is_dom_failure else "DOWN"
            next_run = now + timedelta(days=frequency_days)
            severity = "CRITICAL"
            alert_type = "CONSECUTIVE_FAIL"
            message = (
                f"{source_code} acumula {failures} fallos consecutivos: {error_message}"
            )
        cursor.execute(
            "UPDATE source SET health_status = %s, next_run_at = %s WHERE id = %s;",
            (health_status, next_run, source_id),
        )
        cursor.execute(
            """
            INSERT INTO alert (ingestion_id, severity, type, message, entity_ref)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id;
            """,
            (ingestion_id, severity, alert_type, message, source_code),
        )
        alert_id = cursor.fetchone()[0]

        if failures >= threshold and setting_bool(values, "alert_email_enabled", False):
            cursor.execute(
                """
                SELECT DISTINCT u.email
                  FROM app_user u
                  JOIN user_alert_subscription s ON s.user_id = u.id
                 WHERE s.source_id = %s AND s.active = TRUE
                   AND u.is_active = TRUE AND u.email <> '';
                """,
                (source_id,),
            )
            recipients = [row[0] for row in cursor.fetchall()]
            if recipients:
                from_email = values.get("email_from") or getattr(
                    settings, "DEFAULT_FROM_EMAIL", ""
                )
                subject = f"Alerta ETL: {source_name} presenta fallos consecutivos"
                body = (
                    f"La fuente {source_code} lleva {failures} intentos fallidos.\n\n"
                    f"Error: {error_message}\n"
                    f"Último éxito: {last_success or 'sin registros'}\n"
                )
                try:
                    send_mail(subject, body, from_email, recipients, fail_silently=False)
                except (smtplib.SMTPException, OSError):
                    logger.exception("No se pudo enviar alerta por correo para %s", source_code)
                else:
                    cursor.execute(
                        "UPDATE alert SET channel_sent = %s::jsonb WHERE id = %s;",
                        (json.dumps({"email": recipients}), alert_id),
                    )
