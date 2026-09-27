from django.apps import AppConfig


class StoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'store'

    def ready(self):
        from . import security  # noqa: F401  — connects sign-in/sign-out audit receivers
