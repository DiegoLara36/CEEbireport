# catalog/management/commands/seed_catalog.py
from django.core.management.base import BaseCommand
from etl.extractors.pucp import PucpETLService


class Command(BaseCommand):
    help = "Ejecuta el ETL de Educación Continua PUCP hacia PostgreSQL."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Limita la cantidad de cursos a procesar para pruebas rápidas",
        )

    def handle(self, *args, **options):
        service = PucpETLService()
        service.run(max_courses=options["limit"])