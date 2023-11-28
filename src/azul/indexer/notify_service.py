import json
import logging

from azul import (
    JSON,
    config,
)
from azul.deployment import (
    aws,
)
from azul.strings import (
    trunc_ellipses,
)

log = logging.getLogger(__name__)


class AzulEmailNotificationService:

    def notify_group(self, subject: str, message: str) -> None:
        log.info('Notifying group of event %r', trunc_ellipses(message, 256))
        # Try to improve readability by adding indent
        try:
            body = json.loads(message)
        except json.decoder.JSONDecodeError:
            log.warning('Not a JSON serializable event, sending as received.')
            body = message
        else:
            body = json.dumps(body, indent=4)
        response = aws.ses.send_email(
            FromEmailAddress=' '.join([
                'Azul',
                config.deployment_stage,
                'Monitoring',
                '<monitoring@' + config.api_lambda_domain('indexer') + '>'
            ]),
            Destination={
                'ToAddresses': [config.monitoring_email]
            },
            Content=self._content(subject, body)
        )
        log.info('Sent notification %r', response['MessageId'])

    def _content(self, subject: str, body: str) -> JSON:
        return {
            'Simple': {
                'Subject': {
                    'Data': subject
                },
                'Body': {
                    'Text': {
                        'Data': body
                    }
                }
            }
        }
