import logging
import time
from typing import (
    Optional,
    Union,
)

from google.cloud.bigquery_reservation_v1 import (
    Assignment,
    CapacityCommitment,
    Reservation,
    ReservationServiceClient,
)
from google.cloud.bigquery_reservation_v1.services.reservation_service.pagers import (
    ListAssignmentsPager,
    ListCapacityCommitmentsPager,
    ListReservationsPager,
)
from google.oauth2.service_account import (
    Credentials,
)
from more_itertools import (
    one,
)

from azul import (
    cached_property,
    config,
    require,
)
from azul.deployment import (
    aws,
)

log = logging.getLogger(__name__)


class BigQueryReservation:
    _reservation_id = 'default'

    _rest_api_url = 'https://content-bigqueryreservation.googleapis.com/v1/'

    _http_scopes = ['https://www.googleapis.com/auth/bigquery']

    _path_suffixes = {
        'capacity_commitment': '',
        'reservation': '',
        'assignment': '/reservations/-'
    }

    capacity_commitment: Optional[CapacityCommitment]
    reservation: Optional[Reservation]
    assignment: Optional[Assignment]
    location: str

    def __init__(self,
                 *,
                 location: str = config.tdr_source_location,
                 slots: int = config.bigquery_reserved_slots,
                 dry_run: bool = False):
        """
        :param dry_run: If true, methods will not create/update/destroy any
                        cloud resources.
        """
        self.location = location
        self.slots = slots
        self.dry_run = dry_run
        self.refresh()

    def refresh(self):
        for resource_type, path_suffix in self._path_suffixes.items():
            self._refresh(resource_type)

    def _refresh(self, resource_type):
        pager_method = getattr(self._client, f'list_{resource_type}s')
        path_suffix = self._path_suffixes[resource_type]
        pager = pager_method(parent=self._reservation_parent_path + path_suffix)
        setattr(self, f'{resource_type}', self._single_resource(pager))

    @cached_property
    def credentials(self) -> Credentials:
        with aws.service_account_credentials(config.ServiceAccount.indexer) as file_name:
            credentials = Credentials.from_service_account_file(file_name)
        return credentials.with_scopes(self._http_scopes)

    @cached_property
    def _client(self) -> ReservationServiceClient:
        return ReservationServiceClient(credentials=self.credentials)

    @property
    def _project(self) -> str:
        return self.credentials.project_id

    @property
    def _reservation_parent_path(self) -> str:
        return self._client.common_location_path(project=self._project,
                                                 location=self.location)

    @property
    def is_active(self) -> Optional[bool]:
        resource_statuses = {
            self.capacity_commitment is not None,
            self.reservation is not None,
            self.assignment is not None
        }
        try:
            return one(resource_statuses)
        except ValueError:
            return None

    @property
    def update_time(self) -> Optional[float]:
        """
        The time at which the current Reservation was updated as a Unix
        timestamp, or None if is there is no Reservation.
        """
        if self.reservation is None:
            return None
        else:
            return self.reservation.update_time.timestamp()

    def activate(self) -> None:
        self._ensure_capacity_commitment()
        self._create_reservation()
        self._assign_slots()
        self.refresh()
        if not self.dry_run:
            if not self.is_active:
                raise RuntimeError('Failed to activate slots')
            if self.capacity_commitment.slot_count < self.slots:
                raise RuntimeError('Failed to acquire enough slots',
                                   self.capacity_commitment.slot_count,
                                   self.slots)

    def _ensure_capacity_commitment(self) -> None:
        """
        Idempotently purchase capacity commitment, ensuring that the number of
        reserved slots is sufficient for the current configuration.
        """
        self._refresh('capacity_commitment')
        if self.capacity_commitment is None:
            self._create_capacity_commitment(self.slots)
        else:
            current_capacity = self.capacity_commitment.slot_count
            log.info('Slot commitment with %d slots already purchased in location %r',
                     current_capacity, self.location)
            if current_capacity < self.slots:
                deficit = self.slots - current_capacity
                log.info('Slot deficit is %d', deficit)
                # To increase the capacity of an existing commitment, we must
                # create a new commitment and then merge it with the old one.
                old_commitment = self.capacity_commitment
                self._create_capacity_commitment(deficit)
                new_commitment = self.capacity_commitment
                if self.dry_run:
                    assert old_commitment is new_commitment
                    log.info('Would merge capacity commitments in location %r', self.location)
                else:
                    assert old_commitment.name != new_commitment.name
                    log.info('Merging capacity commitments {%r, %r}',
                             old_commitment.name, new_commitment.name)

                    def extract_id(commitment: CapacityCommitment) -> str:
                        parent, resource_type, id = commitment.name.rsplit('/', 2)
                        assert parent == self._reservation_parent_path
                        assert resource_type == 'capacityCommitments'
                        return id

                    self.capacity_commitment = self._client.merge_capacity_commitments(
                        parent=self._reservation_parent_path,
                        capacity_commitment_ids=list(map(extract_id, [old_commitment, new_commitment]))
                    )
                    log.info('Merged capacity commitments {%r, %r} to create %r with %d total slots',
                             old_commitment.name, new_commitment.name, self.capacity_commitment.name,
                             self.capacity_commitment.slot_count)
            elif current_capacity > self.slots:
                log.warning('Existing slot commitment has more slots than requested; '
                            'excessive costs may be incurred. If a smaller capacity '
                            'is desired, it must be reduced manually.')
            else:
                log.info('No purchase necessary')

    def _create_capacity_commitment(self, slots: int) -> None:
        commitment = CapacityCommitment(dict(slot_count=slots,
                                             plan=CapacityCommitment.CommitmentPlan.FLEX))
        if self.dry_run:
            log.info('Would purchase %d BigQuery slots in location %r',
                     commitment.slot_count, self.location)
        else:
            log.info('Purchasing %d BigQuery slots in location %r',
                     commitment.slot_count, self.location)
            commitment = self._client.create_capacity_commitment(capacity_commitment=commitment,
                                                                 parent=self._reservation_parent_path)
            log.info('Purchased %d BigQuery slots in location %r, commitment name: %r',
                     commitment.slot_count, self.location, commitment.name)
            # Record state before waiting for activation so that we can delete it on failure
            self.capacity_commitment = commitment
            self.capacity_commitment = self._await_active_commitment(commitment)

    def _await_active_commitment(self,
                                 commitment: CapacityCommitment
                                 ) -> CapacityCommitment:
        """
        Poll for a minute or until commitment is active. Fail gracefully if we
        are unable to get commitment. See Google's docs for more info:
        https://cloud.google.com/bigquery/docs/reservations-tasks#purchased_slots_are_pending
        """
        start = time.time()
        deadline = start + 60
        now = start
        while commitment.state != commitment.State.ACTIVE:
            if now > deadline:
                self.deactivate()
                log.error('Commitment %r in state %r after %.3fs seconds. '
                          'Commitment was deleted. Try again later.',
                          commitment.name, commitment.state.name, now - start)
                raise RuntimeError('Slot commitment not active in time')
            elif commitment.state == commitment.State.FAILED:
                self.deactivate()
                raise RuntimeError('Slot commitment failed to activate',
                                   commitment.failure_status)
            elif commitment.state == commitment.State.PENDING:
                log.info('Commitment %r pending. Trying again in 10 seconds...',
                         commitment.name)
                time.sleep(10)
                commitment = self._client.get_capacity_commitment(name=commitment.name)
                now = time.time()
            else:
                assert False, commitment.state
        log.info('Commitment %r is active after %.3fs seconds',
                 commitment.name, now - start)
        return commitment

    def _create_reservation(self) -> None:
        """
        Idempotently create reservation.
        """
        self._refresh('reservation')
        if self.reservation is None:
            reservation = Reservation(dict(slot_capacity=self.slots,
                                           ignore_idle_slots=False))
            if self.dry_run:
                log.info('Would reserve %d BigQuery slots in location %r, reservation ID: %r',
                         reservation.slot_capacity, self.location, self._reservation_id)
            else:
                log.info('Reserving %d BigQuery slots in location %r, reservation ID: %r',
                         reservation.slot_capacity, self.location, self._reservation_id)
                reservation = self._client.create_reservation(reservation=reservation,
                                                              reservation_id=self._reservation_id,
                                                              parent=self._reservation_parent_path)
                log.info('Reserved %d BigQuery slots in location %r, reservation name: %r',
                         reservation.slot_capacity, self.location, reservation.name)
                self.reservation = reservation
        else:
            current_capacity = self.reservation.slot_capacity
            log.info('Reservation with capacity %d already created in location %r',
                     current_capacity, self.location)
            if current_capacity < self.slots:
                log.info('Capacity deficit is %d', self.slots - current_capacity)
                if self.dry_run:
                    log.info('Would increase reservation capacity to %d', self.slots)
                else:
                    log.info('Increasing reservation capacity to %d', self.slots)
                    self.reservation.slot_capacity = self.slots
                    self.reservation = self._client.update_reservation(
                        reservation=self.reservation,
                        update_mask='slotCapacity'
                    )
                    log.info('Reservation now has capacity %d', self.reservation.slot_capacity)

    def _assign_slots(self) -> None:
        """
        Idempotently assign capacity commitment to a reservation.
        """
        self._refresh('assignment')
        if self.assignment is not None:
            log.info('Slots already assigned in location %r',
                     self.location)
        else:
            assignment = Assignment(dict(assignee=f'projects/{self._project}',
                                         job_type=Assignment.JobType.QUERY))
            if self.dry_run:
                reservation_name = None if self.reservation is None else self.reservation.name
                log.info('Would assign slots to reservation %r in location %r',
                         reservation_name, self.location)
            else:
                require(self.reservation is not None)
                log.info('Assigning slots to reservation %r in location %r',
                         self.reservation.name, self.location)
                assignment = self._client.create_assignment(parent=self.reservation.name,
                                                            assignment=assignment)
                log.info('Assigned slots in location %r, assignment name: %r',
                         self.location, assignment.name)
                self.assignment = assignment

    def deactivate(self) -> None:
        """
        Idempotently delete all resources.
        """
        for resource_type in ('assignment', 'reservation', 'capacity_commitment'):
            resource = getattr(self, resource_type)
            if resource is None:
                log.info('%r does not exist in location %r',
                         resource_type, self.location)
            else:
                resource_str = f'{resource_type}:{resource.name}'
                if self.dry_run:
                    log.info('Would delete resource %r in location %r',
                             resource_str, self.location)
                else:
                    delete_method = getattr(self._client, 'delete_' + resource_type)
                    delete_method(name=resource.name)
                    log.info('Deleted resource %r in location %r',
                             resource_str, self.location)
        self.refresh()
        # self.is_active is None when some, but not all resources are present
        if not self.dry_run and self.is_active is not False:
            raise RuntimeError(f'Failed to delete slots in location {self.location!r}')

    ResourcePager = Union[
        ListCapacityCommitmentsPager,
        ListReservationsPager,
        ListAssignmentsPager
    ]

    Resource = Union[
        CapacityCommitment,
        Reservation,
        Assignment
    ]

    def _single_resource(self, resources: ResourcePager) -> Optional[Resource]:
        resources = list(resources)
        try:
            resource, *extras = resources
        except ValueError:
            return None
        else:
            require(not extras,
                    'Too many resources in path (should be 0 or 1)',
                    self._reservation_parent_path, resources)
            return resource
