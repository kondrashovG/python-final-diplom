from django.apps import AppConfig


class BackendConfig(AppConfig):
    name = 'orders'

    def ready(self):
        """
        импортируем сигналы
        """