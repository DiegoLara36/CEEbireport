from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from etl.services.etl_engine import ETLEngine


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
        with connection.cursor() as cursor:
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
            result = ETLEngine(code).run()
            status = result.get("status", "UNKNOWN")
            self.stdout.write(
                f"{code}: {status}; descubiertos={result['discovered']}, "
                f"procesados={result['processed_ok']}, cuarentena={result['quarantined']}."
            )
