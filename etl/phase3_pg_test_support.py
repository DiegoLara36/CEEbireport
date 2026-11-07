import os
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST_DB_NAME = "ceebi_phase3_test"


def test_database_config(
    environ: Mapping[str, str | None] | None = None,
) -> dict[str, str]:
    values = os.environ if environ is None else environ
    file_values = dotenv_values(PROJECT_ROOT / ".env")

    def configured_value(key: str, default: str | None = None) -> str | None:
        if key in values:
            return values[key]
        return file_values.get(key, default)

    name = (configured_value("CEEBI_TEST_DB_NAME", DEFAULT_TEST_DB_NAME) or "").strip()
    app_db_name = file_values.get("DB_NAME")

    if not name.casefold().endswith("test"):
        raise RuntimeError(
            "CEEBI_TEST_DB_NAME debe terminar en 'test'; no se abrirá ninguna conexión."
        )
    if app_db_name and name.casefold() == str(app_db_name).strip().casefold():
        raise RuntimeError(
            "CEEBI_TEST_DB_NAME coincide con DB_NAME de .env; destino rechazado."
        )

    user = configured_value("CEEBI_TEST_DB_USER")
    password = configured_value("CEEBI_TEST_DB_PASSWORD")
    if not user or password is None:
        raise RuntimeError(
            "Configure CEEBI_TEST_DB_USER y CEEBI_TEST_DB_PASSWORD para la base aislada."
        )

    return {
        "dbname": name,
        "user": user,
        "password": password,
        "host": configured_value("CEEBI_TEST_DB_HOST", "localhost") or "localhost",
        "port": configured_value("CEEBI_TEST_DB_PORT", "5432") or "5432",
    }


def django_test_database(config: Mapping[str, str]) -> dict[str, str]:
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config["dbname"],
        "USER": config["user"],
        "PASSWORD": config["password"],
        "HOST": config["host"],
        "PORT": config["port"],
    }
