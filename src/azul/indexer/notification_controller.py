import chalice.app

from azul import (
    cached_property,
)
from azul.chalice import (
    AppController,
)
from azul.indexer.notify_service import (
    AzulEmailNotificationService,
)


class NotificationController(AppController):

    @cached_property
    def service(self):
        return AzulEmailNotificationService()

    def notify_group(self, event: chalice.app.SNSEvent) -> None:
        self.service.notify_group(event.subject, event.message)
