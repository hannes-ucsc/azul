import os
from typing import (
    ClassVar,
    Optional,
)
import warnings

import docker
from docker import (
    DockerClient,
)
from docker.models.containers import (
    Container,
)
from more_itertools import (
    one,
)

from azul import (
    Netloc,
    config,
)
from azul.logging import (
    get_test_logger,
)
from azul_test_case import (
    AzulUnitTestCase,
)

log = get_test_logger(__name__)


class DockerContainerTestCase(AzulUnitTestCase):
    """
    A test case facilitating the creation of Docker containers that live as long
    as the test case.
    """
    _docker: ClassVar[Optional[DockerClient]] = None

    _containers: ClassVar[list[Container]] = []

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        assert len(cls._containers) == 0  # tearDownClass reset this already
        if cls._docker is None:
            cls._docker = docker.from_env()

    @classmethod
    def _create_container(cls, image: str, container_port: int, **kwargs) -> Netloc:
        """
        Create a Docker container from the given image, exposing the given
        container port on an interface that is within reach of the current
        process.

        :param image: the fully qualified name of the Docker image to run

        :param container_port: The TCP port that the process inside the
                               container binds to.

        :param kwargs: Additional parameters to the client.container.run()
                       method of the Docker Python SDK.

        :return: A tuple `(ip, port)` describing the actual endpoint the given
                 container port was exposed on.
        """
        # If the current process runs in a container (as is currently the case
        # on Gitlab), our best guess is that the container launcher here will
        # be a sibling of the current container. Exposing the container port on
        # the host is difficult if not impossible since we don't know—and may
        # not even have access to—the host's network interfaces. Even if we
        # correctly guessed the IP of an interface on the host, we would still
        # need traffic to be forwarded from the current container to that host
        # interface.
        image = config.docker_registry + image
        is_sibling = cls._running_in_docker()
        log.info('Launching %scontainer from image %s',
                 'sibling ' if is_sibling else '', image)
        ports = None if is_sibling else {container_port: ('127.0.0.1', None)}
        container = cls._docker.containers.run(image,
                                               detach=True,
                                               auto_remove=True,
                                               ports=ports,
                                               **kwargs)
        try:
            container_info = cls._docker.api.inspect_container(container.name)
            network_settings = container_info['NetworkSettings']
            if is_sibling:  # no coverage
                container_ip = network_settings['IPAddress']
                assert isinstance(container_ip, str)
                endpoint = (container_ip, container_port)
                log.info('Launched sibling container %s from image %s, listening on %s:%i',
                         container.name, image, container_ip, container_port)
            else:
                ports = network_settings['Ports']
                port = one(ports[f'{container_port}/tcp'])
                host_ip = port['HostIp']
                host_port = int(port['HostPort'])
                log.info('Launched container %s from image %s, '
                         'with container port %s mapped to %s:%i on the host',
                         container.name, image, container_port, host_ip, host_port)
                endpoint = (host_ip, host_port)
        except BaseException:  # no coverage
            container.kill()
            raise
        else:
            cls._containers.append(container)
            return endpoint

    @classmethod
    def _running_in_docker(cls):
        """
        Detect if the current process is running inside a Docker container.
        """
        # This is how Docker does it internally.
        #
        # https://github.com/docker/libnetwork/blob/411d314/drivers/bridge/setup_bridgenetfiltering.go#L160
        #
        # People have been warning that it might go away. However, they've been
        # saying that since 2015.
        try:
            os.stat('/.dockerenv')
        except FileNotFoundError:
            running_in_container = False
        else:  # no coverage
            running_in_container = True
        return running_in_container

    @classmethod
    def _kill_containers(cls):
        for container in cls._containers:
            container.kill()
        cls._containers.clear()

    @classmethod
    def tearDownClass(cls):
        for containers in cls._containers:
            for line in containers.logs().decode().split('\n'):
                if 'deprecated' in line.lower():
                    warnings.warn(line, DeprecationWarning)
        cls._kill_containers()
        super().tearDownClass()
