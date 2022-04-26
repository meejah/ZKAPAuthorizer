# -*- coding: utf-8 -*-
# Copyright 2019 PrivateStorage.io, LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
This module implements views (in the MVC sense) for the web interface for
the client side of the storage plugin.  This interface allows users to redeem
vouchers for fresh tokens.

In the future it should also allow users to read statistics about token usage.
"""

from collections.abc import Awaitable
from functools import partial
from json import loads
from typing import Callable

from allmydata.uri import ReadonlyDirectoryURI, from_string
from attr import Factory, define, field
from twisted.internet.defer import Deferred, inlineCallbacks
from twisted.logger import Logger
from twisted.web.http import (
    ACCEPTED,
    BAD_REQUEST,
    CONFLICT,
    CREATED,
    INTERNAL_SERVER_ERROR,
)
from twisted.web.iweb import IRequest
from twisted.web.resource import ErrorPage, IResource, NoResource, Resource
from twisted.web.server import NOT_DONE_YET
from zope.interface import Attribute

from . import NAME
from . import __version__ as _zkapauthorizer_version
from ._base64 import urlsafe_b64decode
from ._json import dumps_utf8
from .controller import PaymentController, get_redeemer
from .lease_maintenance import LeaseMaintenanceConfig
from .model import NotEmpty, VoucherStore
from .pricecalculator import PriceCalculator
from .private import create_private_tree
from .recover import Downloader, StatefulRecoverer
from .replicate import ReplicationAlreadySetup
from .storage_common import (
    get_configured_allowed_public_keys,
    get_configured_pass_value,
    get_configured_shares_needed,
    get_configured_shares_total,
)

# The number of tokens to submit with a voucher redemption.
NUM_TOKENS = 2 ** 15


class IZKAPRoot(IResource):
    """
    The root of the resource tree of this plugin's client web presence.
    """

    store = Attribute("The ``VoucherStore`` used by this resource tree.")
    controller = Attribute("The ``PaymentController`` used by this resource tree.")


def get_token_count(
    plugin_name,
    node_config,
):
    """
    Retrieve the configured voucher value, in number of tokens, from the given
    configuration.

    :param str plugin_name: The plugin name to use to choose a
        configuration section.

    :param _Config node_config: See ``from_configuration``.

    :param int default: The value to return if none is configured.
    """
    section_name = "storageclient.plugins.{}".format(plugin_name)
    return int(
        node_config.get_config(
            section=section_name,
            option="default-token-count",
            default=NUM_TOKENS,
        )
    )


def from_configuration(
    node_config,
    store,
    get_downloader,
    setup_replication,
    redeemer=None,
    clock=None,
):
    """
    Instantiate the plugin root resource using data from its configuration
    section, **storageclient.plugins.privatestorageio-zkapauthz-v2**, in the
    Tahoe-LAFS configuration file.  See the configuration documentation for
    details of the configuration section.

    :param _Config node_config: An object representing the overall node
        configuration.  The plugin configuration can be extracted from this.
        This is also used to read and write files in the private storage area
        of the node's persistent state location.

    :param VoucherStore store: The store to use.

    :param IRedeemer redeemer: The voucher redeemer to use.  If ``None`` a
        sensible one is constructed.

    :param clock: See ``PaymentController._clock``.

    :return IZKAPRoot: The root of the resource hierarchy presented by the
        client side of the plugin.
    """
    if redeemer is None:
        redeemer = get_redeemer(
            NAME,
            node_config,
            None,
            None,
        )

    default_token_count = get_token_count(
        NAME,
        node_config,
    )
    controller = PaymentController(
        store,
        redeemer,
        default_token_count,
        allowed_public_keys=get_configured_allowed_public_keys(node_config),
        clock=clock,
    )

    calculator = PriceCalculator(
        get_configured_shares_needed(node_config),
        get_configured_shares_total(node_config),
        get_configured_pass_value(node_config),
    )
    calculate_price = _CalculatePrice(
        calculator,
        LeaseMaintenanceConfig.from_node_config(node_config).get_lease_duration(),
    )

    root = create_private_tree(
        lambda: node_config.get_private_config("api_auth_token").encode("utf-8"),
        authorizationless_resource_tree(
            store,
            controller,
            get_downloader,
            setup_replication,
            calculate_price,
        ),
    )
    root.store = store
    root.controller = controller
    return root


@define
class ReplicateResource(Resource):
    """
    Integrate the replication configuration implementation with the HTTP
    interface.

    :ivar _setup: The callable the resource will use to do the actual setup
        work.
    """

    _setup: Callable[[], Awaitable[str]]

    _log = Logger()

    def __attrs_post_init__(self):
        Resource.__init__(self)

    def render_POST(self, request):
        self._setup_replication(request)
        return NOT_DONE_YET

    @inlineCallbacks
    def _setup_replication(self, request):
        """
        Call the replication setup function and asynchronously deliver its result
        as a response to the given request.
        """
        try:
            cap_str = yield Deferred.fromCoroutine(self._setup())
        except ReplicationAlreadySetup:
            request.setResponseCode(CONFLICT)
        except:
            self._log.failure("replication setup failed")
            request.setResponseCode(INTERNAL_SERVER_ERROR)
        else:
            application_json(request)
            request.setResponseCode(CREATED)
            request.write(dumps_utf8({"recovery-capability": cap_str}))

        request.finish()


@define
class RecoverResource(Resource):
    """
    Implement the endpoint for triggering local state recovery from a remote
    replica.
    """

    _log = Logger()

    store: VoucherStore = field()
    get_downloader: Callable[[str], Downloader] = field()
    recoverer: StatefulRecoverer = field(default=Factory(StatefulRecoverer))

    def __attrs_post_init__(self):
        Resource.__init__(self)

    def render_GET(self, request):
        application_json(request)
        return dumps_utf8(self.recoverer.state().marshal())

    def render_POST(self, request):
        if wrong_content_type(request, "application/json"):
            return NOT_DONE_YET

        try:
            body = loads(request.content.read())
        except:
            request.setResponseCode(BAD_REQUEST)
            return b"could not parse json"

        if body.keys() != {"recovery-capability"}:
            request.setResponseCode(BAD_REQUEST)
            return b"json did not have expected properties"

        cap_str = body["recovery-capability"]
        if not isinstance(cap_str, str):
            request.setResponseCode(BAD_REQUEST)
            return b"recovery-capability must be a read-only dircap string"

        cap = from_string(cap_str)
        if not isinstance(cap, ReadonlyDirectoryURI):
            request.setResponseCode(BAD_REQUEST)
            return b"recovery-capability must be a read-only dircap string"

        # The response to this request does not wait for recovery to complete.
        # Instead, a separate endpoint exposes the progress of the recovery
        # attempt.  So we're not going to wait for `recovering` to complete.
        # However, we do have to schedule it in the event loop or it will
        # never even start.
        recovering = self._recover(request, self.store, cap)
        d = Deferred.fromCoroutine(recovering)

        # The recovery code is meant to be /pretty/ unlikely to raise an
        # exception - it directs all errors into the status information
        # exposed by StatefulRecoverer instead of letting them get raised.
        # Still, it's not _impossible_ that an exception could come out.  If
        # it does, make sure it shows up in the log at least.
        d.addErrback(partial(self._log.failure, "unhandled recovery failure"))

        # _recover is responsible for generating the response.
        return NOT_DONE_YET

    async def _recover(
        self,
        request: IRequest,
        store: VoucherStore,
        cap: ReadonlyDirectoryURI,
    ):
        async def initiate(request, recoverer, downloader, cursor):
            recovering = recoverer.recover(downloader, cursor)
            recovering_d = Deferred.fromCoroutine(recovering)

            # The only way to get to this point is if the store is empty and
            # the recoverer appears to at least be willing to try to start.
            # This is no guarantee that recovery will succeed but it is a
            # guarantee that the *try* has begun.  Generate a response that
            # reflects this.
            request.setResponseCode(ACCEPTED)
            request.finish()

            # Really start the recovery attempt.
            await recovering_d

        try:
            # If these things succeed then we will have started recovery and
            # generated a response to the request.
            downloader = self.get_downloader(cap)
            await store.call_if_empty(
                partial(initiate, request, self.recoverer, downloader)
            )
        except NotEmpty:
            # If the database had anything in it, though, recovery will not be
            # attempted - and it will even fail quickly enough to be a
            # reasonable way to detect and report the conflict case.
            request.setResponseCode(CONFLICT)
            request.write(b"there is existing local state")
            request.finish()
        except:
            # And if something else is broken, then who knows...  At least try
            # to generate an error response.
            self._log.failure("recovery setup failed")
            request.setResponseCode(INTERNAL_SERVER_ERROR)
            request.write(b"recovery setup failed")
            request.finish()


def authorizationless_resource_tree(
    store,
    controller,
    get_downloader: Callable[[str], Downloader],
    setup_replication: Callable[[], Awaitable[str]],
    calculate_price,
):
    """
    Create the full ZKAPAuthorizer client plugin resource hierarchy with no
    authorization applied.

    :param VoucherStore store: The store to use.
    :param PaymentController controller: The payment controller to use.

    :param get_downloader: A callable which accepts a replica identifier and
        can download the replica data.

    :param IResource calculate_price: The resource for the price calculation endpoint.

    :return IResource: The root of the resource hierarchy.
    """
    root = Resource()

    root.putChild(
        b"recover",
        RecoverResource(store, get_downloader),
    )
    root.putChild(
        b"replicate",
        ReplicateResource(setup_replication),
    )
    root.putChild(
        b"voucher",
        _VoucherCollection(
            store,
            controller,
        ),
    )
    root.putChild(
        b"lease-maintenance",
        _LeaseMaintenanceResource(
            store,
            controller,
        ),
    )
    root.putChild(
        b"version",
        _ProjectVersion(),
    )
    root.putChild(
        b"calculate-price",
        calculate_price,
    )
    return root


class _CalculatePrice(Resource):
    """
    This resource exposes a storage price calculator.
    """

    allowedMethods = [b"POST"]

    render_HEAD = render_GET = None

    def __init__(self, price_calculator, lease_period):
        """
        :param _PriceCalculator price_calculator: The object which can actually
            calculate storage prices.

        :param lease_period: See ``authorizationless_resource_tree``
        """
        self._price_calculator = price_calculator
        self._lease_period = lease_period
        Resource.__init__(self)

    def render_POST(self, request):
        """
        Calculate the price in ZKAPs to store or continue storing files specified
        sizes.
        """
        if wrong_content_type(request, "application/json"):
            return NOT_DONE_YET

        application_json(request)
        payload = request.content.read()
        try:
            body_object = loads(payload)
        except ValueError:
            request.setResponseCode(BAD_REQUEST)
            return dumps_utf8(
                {
                    "error": "could not parse request body",
                }
            )

        try:
            version = body_object["version"]
            sizes = body_object["sizes"]
        except (TypeError, KeyError):
            request.setResponseCode(BAD_REQUEST)
            return dumps_utf8(
                {
                    "error": "could not read `version` and `sizes` properties",
                }
            )

        if version != 1:
            request.setResponseCode(BAD_REQUEST)
            return dumps_utf8(
                {
                    "error": "did not find required version number 1 in request",
                }
            )

        if not isinstance(sizes, list) or not all(
            isinstance(size, int) and size >= 0 for size in sizes
        ):
            request.setResponseCode(BAD_REQUEST)
            return dumps_utf8(
                {
                    "error": "did not find required positive integer sizes list in request",
                }
            )

        application_json(request)

        price = self._price_calculator.calculate(sizes)
        return dumps_utf8(
            {
                "price": price,
                "period": self._lease_period,
            }
        )


def wrong_content_type(request, required_type):
    """
    Check the content-type of a request and respond if it is incorrect.

    :param request: The request object to check.

    :param str required_type: The required content-type (eg
        ``"application/json"``).

    :return bool: ``True`` if the content-type is wrong and an error response
        has been generated.  ``False`` otherwise.
    """
    actual_type = request.requestHeaders.getRawHeaders(
        "content-type",
        [None],
    )[0]
    if actual_type != required_type:
        request.setResponseCode(BAD_REQUEST)
        request.finish()
        return True
    return False


def application_json(request):
    """
    Set the given request's response content-type to ``application/json``.

    :param twisted.web.iweb.IRequest request: The request to modify.
    """
    request.responseHeaders.setRawHeaders("content-type", ["application/json"])


class _ProjectVersion(Resource):
    """
    This resource exposes the version of **ZKAPAuthorizer** itself.
    """

    def render_GET(self, request):
        application_json(request)
        return dumps_utf8(
            {
                "version": _zkapauthorizer_version,
            }
        )


class _LeaseMaintenanceResource(Resource):
    """
    This class implements inspection of lease maintenance activity.  Users
    **GET** this resource to learn about lease maintenance spending.
    """

    _log = Logger()

    def __init__(self, store, controller):
        self._store = store
        self._controller = controller
        Resource.__init__(self)

    def render_GET(self, request):
        """
        Retrieve the spending information.
        """
        application_json(request)
        return dumps_utf8(
            {
                "total": self._store.count_unblinded_tokens(),
                "spending": self._lease_maintenance_activity(),
            }
        )

    def _lease_maintenance_activity(self):
        activity = self._store.get_latest_lease_maintenance_activity()
        if activity is None:
            return activity
        return {
            "when": activity.finished.isoformat(),
            "count": activity.passes_required,
        }


class _VoucherCollection(Resource):
    """
    This class implements redemption of vouchers.  Users **PUT** such numbers
    to this resource which delegates redemption responsibilities to the
    redemption controller.  Child resources of this resource can also be
    retrieved to monitor the status of previously submitted vouchers.
    """

    _log = Logger()

    def __init__(self, store, controller):
        self._store = store
        self._controller = controller
        Resource.__init__(self)

    def render_PUT(self, request):
        """
        Record a voucher and begin attempting to redeem it.
        """
        try:
            payload = loads(request.content.read())
        except Exception:
            return bad_request("json request body required").render(request)
        if payload.keys() != {"voucher"}:
            return bad_request(
                "request object must have exactly one key: 'voucher'"
            ).render(request)
        voucher = payload["voucher"]
        if not is_syntactic_voucher(voucher):
            return bad_request("submitted voucher is syntactically invalid").render(
                request
            )

        self._log.info(
            "Accepting a voucher ({voucher}) for redemption.", voucher=voucher
        )
        self._controller.redeem(voucher.encode("ascii"))
        return b""

    def render_GET(self, request):
        application_json(request)
        return dumps_utf8(
            {
                "vouchers": list(
                    self._controller.incorporate_transient_state(voucher).marshal()
                    for voucher in self._store.list()
                ),
            }
        )

    def getChild(self, segment, request):
        voucher = segment.decode("utf-8")
        if not is_syntactic_voucher(voucher):
            return bad_request()
        try:
            voucher = self._store.get(voucher.encode("ascii"))
        except KeyError:
            return NoResource()
        return VoucherView(self._controller.incorporate_transient_state(voucher))


def is_syntactic_voucher(voucher):
    """
    :param voucher: A candidate object to inspect.

    :return bool: ``True`` if and only if ``voucher`` is a text string
        containing a syntactically valid voucher.  This says **nothing** about
        the validity of the represented voucher itself.  A ``True`` result
        only means the string can be **interpreted** as a voucher.
    """
    if not isinstance(voucher, str):
        return False
    if len(voucher) != 44:
        # TODO.  44 is the length of 32 bytes base64 encoded.  This model
        # information presumably belongs somewhere else.
        return False
    try:
        urlsafe_b64decode(voucher.encode("ascii"))
    except Exception:
        return False
    return True


class VoucherView(Resource):
    """
    This class implements a view for a ``Voucher`` instance.
    """

    def __init__(self, voucher):
        """
        :param Voucher reference: The model object for which to provide a
            view.
        """
        self._voucher = voucher
        Resource.__init__(self)

    def render_GET(self, request):
        application_json(request)
        return self._voucher.to_json()


def bad_request(reason="Bad Request"):
    """
    :return IResource: A resource which can be rendered to produce a **BAD
        REQUEST** response.
    """
    return ErrorPage(
        BAD_REQUEST,
        b"Bad Request",
        reason.encode("utf-8"),
    )
