# Copyright 2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""HTTP service for the rack controller."""

__all__ = [
    "HTTPResource",
    "RackHTTPService",
]

from collections import defaultdict
from datetime import timedelta
import os
import sys

import attr
from netaddr import IPAddress
from provisioningserver.config import is_dev_environment
from provisioningserver.events import (
    EVENT_TYPES,
    send_node_event_ip_address,
)
from provisioningserver.logger import LegacyLogger
from provisioningserver.path import get_tentative_data_path
from provisioningserver.prometheus.metrics import PROMETHEUS_METRICS
from provisioningserver.prometheus.resource import PrometheusMetricsResource
from provisioningserver.service_monitor import service_monitor
from provisioningserver.utils import (
    load_template,
    snappy,
)
from provisioningserver.utils.fs import atomic_write
from provisioningserver.utils.twisted import callOut
from twisted.application.internet import TimerService
from twisted.internet import reactor
from twisted.internet.defer import maybeDeferred
from twisted.internet.task import deferLater
from twisted.internet.threads import deferToThread
from twisted.web import resource


log = LegacyLogger()


def get_http_config_dir():
    """Location of MAAS' http configuration files."""
    setting = os.getenv(
        "MAAS_HTTP_CONFIG_DIR",
        get_tentative_data_path('/var/lib/maas/http'))
    if isinstance(setting, bytes):
        fsenc = sys.getfilesystemencoding()
        return setting.decode(fsenc)
    else:
        return setting


def compose_http_config_path(filename):
    """Return the full path for a HTTP config."""
    return os.path.join(get_http_config_dir(), filename)


class HTTPConfigFail(Exception):
    """Raised if there's a problem with a HTTP config."""


class RackHTTPService(TimerService):

    # Initial start the interval is low so that proxy_pass of nginx gets
    # at least one region controller. When no region controllers are set
    # on the proxy_pass the interval is always set to the lower setting.
    INTERVAL_LOW = timedelta(seconds=5).total_seconds()

    # Once at least one region controller is set on the proxy_pass then
    # the inverval is higher as at least one controller is handling the
    # requests for metadata.
    INTERVAL_HIGH = timedelta(seconds=30).total_seconds()

    _configuration = None
    _resource_root = None
    _rpc_service = None

    def __init__(self, resource_root, rpc_service, reactor):
        super().__init__(self.INTERVAL_LOW, self._tryUpdate)
        self._resource_root = resource_root
        # Nginx requires the that root have an ending slash.
        if not self._resource_root.endswith('/'):
            self._resource_root += '/'
        self._rpc_service = rpc_service
        self.clock = reactor

    def _update_interval(self, num_region_ips):
        """Change the update interval."""
        if num_region_ips <= 0:
            self._loop.interval = self.step = self.INTERVAL_LOW
        else:
            self._loop.interval = self.step = self.INTERVAL_HIGH

    def _tryUpdate(self):
        """Update the HTTP server running on this host."""
        d = maybeDeferred(self._getConfiguration)
        d.addCallback(self._maybeApplyConfiguration)
        d.addErrback(log.err, "Failed to update HTTP configuration.")
        return d

    def _genRegionIps(self):
        """Generate IP addresses for all rack controller this rack
        controller is connected to."""
        # Filter the connects by region.
        conn_per_region = defaultdict(set)
        for eventloop, connection in self._rpc_service.connections.items():
            conn_per_region[eventloop.split(':')[0]].add(connection)
        for _, connections in conn_per_region.items():
            # Sort the connections so the same IP is always picked per
            # region controller. This ensures that the HTTP configuration
            # is not reloaded unless its actually required to reload.
            conn = list(sorted(
                connections, key=lambda conn: conn.address[0]))[0]
            addr = IPAddress(conn.address[0])
            if addr.is_ipv4_mapped():
                yield str(addr.ipv4())
            elif addr.version == 6:
                yield '[%s]' % addr
            else:
                yield str(addr)

    def _getConfiguration(self):
        """Return HTTP server configuration.

        The configuration object returned is comparable with previous and
        subsequently obtained configuration objects, allowing this service to
        determine whether a change needs to be applied to the HTTP server.
        """
        region_ips = list(self._genRegionIps())
        self._update_interval(len(region_ips))
        return _Configuration(upstream_http=region_ips)

    def _maybeApplyConfiguration(self, configuration):
        """Reconfigure the HTTP server if the configuration changes.

        Reconfigure and restart `maas-http` if the current configuration
        differs from a previously applied configuration, otherwise do nothing.

        :param configuration: The configuration object obtained from
            `_getConfiguration`.
        """
        if configuration != self._configuration:
            d = maybeDeferred(self._applyConfiguration, configuration)
            d.addCallback(callOut, self._configurationApplied, configuration)
            return d

    def _applyConfiguration(self, configuration):
        """Configure the HTTP server.

        :param configuration: The configuration object obtained from
            `_getConfiguration`.
        """
        d = deferToThread(
            self._configure,
            configuration.upstream_http)
        # XXX: blake_r 2018-06-12 bug=1687620. When running in a snap,
        # supervisord tracks services. It does not support reloading.
        # Instead, we need to restart the service.
        if snappy.running_in_snap():
            d.addCallback(
                lambda _: service_monitor.restartService("http"))
        elif is_dev_environment():
            pass
        else:
            d.addCallback(
                lambda _: service_monitor.reloadService("http"))
        return d

    def _configurationApplied(self, configuration):
        """Record the currently applied HTTP server configuration.

        :param configuration: The configuration object obtained from
            `_getConfiguration`.
        """
        self._configuration = configuration

    def _configure(self, upstream_http):
        """Update the HTTP configuration for the rack."""
        template = load_template('http', 'rackd.nginx.conf.template')
        try:
            rendered = template.substitute({
                'upstream_http': list(sorted(upstream_http)),
                'resource_root': self._resource_root,
            })
        except NameError as error:
            raise HTTPConfigFail(*error.args)
        else:
            # The rendered configuration is Unicode text but should contain
            # only ASCII characters.
            rendered = rendered.encode("ascii")

        target_path = compose_http_config_path('rackd.nginx.conf')
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        atomic_write(rendered, target_path, overwrite=True, mode=0o644)


@attr.s
class _Configuration:
    """Configuration for the rack's HTTP server."""

    # Addresses of upstream HTTP servers.
    upstream_http = attr.ib(converter=frozenset)


class HTTPLogResource(resource.Resource):
    isLeaf = True

    def render_GET(self, request):
        # Extract the original path and original IP of the request.
        path = request.getHeader('X-Original-URI')
        remote_host = request.getHeader('X-Original-Remote-IP')

        # Log the HTTP request to rackd.log and push that event to the
        # region controller.
        log.info(
            "{path} requested by {remote_host}",
            path=path, remote_host=remote_host)
        d = deferLater(
            reactor, 0, send_node_event_ip_address,
            event_type=EVENT_TYPES.NODE_HTTP_REQUEST,
            ip_address=remote_host, description=path)
        d.addErrback(log.err, "Logging HTTP request failed.")

        # Respond empty to nginx.
        return b''


class HTTPResource(resource.Resource):
    """The root resource for HTTP."""

    def __init__(self):
        super().__init__()
        self.putChild(b'log', HTTPLogResource())
        self.putChild(
            b'metrics', PrometheusMetricsResource(PROMETHEUS_METRICS))
