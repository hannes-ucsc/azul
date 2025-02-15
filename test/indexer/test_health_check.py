from moto import (
    mock_sqs,
    mock_sts,
)

from azul.logging import (
    configure_test_logging,
)
from azul.types import (
    MutableJSON,
)
from azul_test_case import (
    DCP1TestCase,
)
from health_check_test_case import (
    HealthCheckTestCase,
)


# noinspection PyPep8Naming
def setUpModule():
    configure_test_logging()


class TestIndexerHealthCheck(DCP1TestCase, HealthCheckTestCase):

    @classmethod
    def lambda_name(cls) -> str:
        return 'indexer'

    def _expected_health(self,
                         endpoints_up: bool = True,
                         es_up: bool = True
                         ) -> MutableJSON:
        return {
            'up': False,
            **self._expected_elasticsearch(up=es_up),
            **self._expected_queues(up=not es_up),
            **self._expected_progress()
        }

    @mock_sts
    @mock_sqs
    def test_queues_down(self):
        response = self._test()
        self.assertEqual(503, response.status_code)
        self.assertEqual(self._expected_health(), response.json())


del HealthCheckTestCase
