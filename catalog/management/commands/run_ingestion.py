from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from etl.persistence.repositories import IngestionRepository
from etl.services.etl_engine import ETLEngine
from etl.services.resilience_service import read_settings


class Command(BaseCommand):
    help = "Ejecuta la ingesta ETL programada para PUCP y/o CIBERTEC."

    def add_arguments(self, parser):
        parser.add_argument(
            "--source",
            choices=("PUCP", "CIBERTEC"),
            help="Limita la ejecución a una fuente.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Ignora next_run_at para ejecutar manualmente.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        source_code = options["source"]
        force = options["force"]
        ETLEngine.ensure_sources(
            [source_code] if source_code else ["PUCP", "CIBERTEC"]
        )
        with transaction.atomic(), connection.cursor() as cursor:
            IngestionRepository.purge_retained_data(cursor, read_settings(cursor))
            if source_code:
                cursor.execute(
                    """
                    SELECT code FROM source
                     WHERE code = %s AND active = TRUE
                       AND (%s = TRUE OR next_run_at <= %s);
                    """,
                    (source_code, force, now),
                )
            else:
                cursor.execute(
                    """
                    SELECT code FROM source
                     WHERE active = TRUE AND (%s = TRUE OR next_run_at <= %s)
                     ORDER BY code;
                    """,
                    (force, now),
                )
            sources = [row[0] for row in cursor.fetchall()]

        if source_code and not sources:
            raise CommandError(
                f"La fuente {source_code} no existe, está inactiva o aún no debe ejecutarse. "
                "Use --force para ignorar next_run_at."
            )
        if not sources:
            self.stdout.write("No hay fuentes activas pendientes de ejecución.")
            return

        for code in sources:
            self.stdout.write(f"Iniciando ingesta de {code}...")
            result = ETLEngine(code).run(respect_schedule=not force)
            status = result.get("status", "UNKNOWN")
            if status == "SKIPPED":
                self.stdout.write(
                    self.style.WARNING(
                        f"{code}: omitida ({result.get('skip_reason', 'UNKNOWN')})."
                    )
                )
                continue
            quality_status = result.get("quality_status")
            quality_label = f" ({quality_status})" if quality_status else ""
            self.stdout.write(
                f"{code}: {status}{quality_label}; "
                f"descubiertos={result['discovered']}, "
                f"procesados={result['processed_ok']}, cuarentena={result['quarantined']}."
            )
