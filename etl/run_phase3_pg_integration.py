import re
import sys
import unittest

import psycopg

from etl.phase3_pg_test_support import PROJECT_ROOT, test_database_config


def execute_script(conn, script: str) -> None:
    with psycopg.ClientCursor(conn) as cursor:
        cursor.execute(script)


def prepare_database(config: dict[str, str]) -> None:
    with psycopg.connect(**config, autocommit=True) as conn:
        actual_name = conn.execute("SELECT current_database();").fetchone()[0]
        if actual_name.casefold() != config["dbname"].casefold():
            raise RuntimeError(
                "PostgreSQL conectó a una base distinta de CEEBI_TEST_DB_NAME; ejecución abortada."
            )

        existing = {
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public';"
            ).fetchall()
        }
        ddl_path = PROJECT_ROOT / "DDL.txt"
        ddl_source = ddl_path.read_text(encoding="utf-8")
        base_tables = set(
            re.findall(
                r"(?im)^\s*CREATE\s+TABLE\s+([a-z_]\w*)\s*\(",
                ddl_source,
            )
        )
        if not base_tables:
            raise RuntimeError("DDL.txt no contiene CREATE TABLE para bootstrap.")
        if not existing:
            ddl = re.sub(
                r"(?im)^\s*DROP\s+TABLE\b[^;]*;\s*$",
                "",
                ddl_source,
            )
            if re.search(r"(?im)^\s*DROP\s+TABLE\b", ddl):
                raise RuntimeError("DDL.txt contiene DROP TABLE en el bootstrap filtrado.")
            execute_script(conn, ddl)
        elif not base_tables.issubset(existing):
            missing = ", ".join(sorted(base_tables - existing))
            raise RuntimeError(
                "La base de pruebas no está vacía ni tiene el esquema base completo; "
                f"no se aplicará DDL parcialmente. Faltan: {missing}"
            )

        migration = (
            PROJECT_ROOT / "database" / "migrations" / "003_persistence_history.sql"
        ).read_text(encoding="utf-8")
        execute_script(conn, migration)


def main() -> int:
    try:
        config = test_database_config()
    except RuntimeError as exc:
        print(f"PRECHECK FAILED: {exc}", file=sys.stderr)
        return 2
    prepare_database(config)

    import os

    os.environ["CEEBI_RUN_PHASE3_PG_TESTS"] = "1"
    os.environ["DJANGO_SETTINGS_MODULE"] = "ceebireport.phase3_test_settings"
    import django

    django.setup()

    from etl import test_phase3_pg_integration

    suite = unittest.defaultTestLoader.loadTestsFromModule(
        test_phase3_pg_integration
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
