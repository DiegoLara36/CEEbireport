from etl.phase3_pg_test_support import django_test_database, test_database_config

_test_db = test_database_config()

from .settings import *  # noqa: E402,F403

DATABASES = {"default": django_test_database(_test_db)}
