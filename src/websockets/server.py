from __future__ import annotations

import base64
import binascii
import email.utils
import http
import warnings
from typing import Any, Callable, Generator, List, Optional, Sequence, Tuple, cast

from .datastructures import Headers, MultipleValuesError
from .exceptions import (
    InvalidHandshake,
    InvalidHeader,
    InvalidHeaderValue,
    InvalidOrigin,
    InvalidStatus,
    InvalidUpgrade,
    NegotiationError,
)
from .extensions import Extension, ServerExtensionFactory
from .headers import (
    build_extension,
    parse_connection,
    parse_extension,
    parse_subprotocol,
    parse_upgrade,
)
from .http11 import Request, Response
from .protocol import CONNECTING, OPEN, SERVER, Protocol, State
from .typing import (
    ConnectionOption,
    ExtensionHeader,
    LoggerLike,
    Origin,
    Subprotocol,
    UpgradeProtocol,
)
from .utils import accept_key


# See #940 for why lazy_import isn't used here for backwards compatibility.
from .legacy.server import *  # isort:skip  # noqa


__all__ = ["ServerProtocol"]


class ServerProtocol(Protocol):
    """
    Sans-I/O implementation of a WebSocket server connection.

    Args:
        origins: acceptable values of the ``Origin`` header; include
            :obj:`None` in the list if the lack of an origin is acceptable.
            This is useful for defending against Cross-Site WebSocket
            Hijacking attacks.
        extensions: list of supported extensions, in order in which they
            should be tried.
        subprotocols: list of supported subprotocols, in order of decreasing
            preference.
        select_subprotocol: Callback for selecting a subprotocol among
            those supported by the client and the server. It has the same
            signature as the :meth:`select_subprotocol` method, including a
            :class:`ServerProtocol` instance as first argument.
        state: initial state of the WebSocket connection.
        max_size: maximum size of incoming messages in bytes;
            :obj:`None` disables the limit.
        logger: logger for this connection;
            defaults to ``logging.getLogger("websockets.client")``;
            see the :doc:`logging guide <../../topics/logging>` for details.

    """

    def __init__(
        self,
        *,
        origins: Optional[Sequence[Optional[Origin]]] = None,
        extensions: Optional[Sequence[ServerExtensionFactory]] = None,
        subprotocols: Optional[Sequence[Subprotocol]] = None,
        select_subprotocol: Optional[
            Callable[
                [ServerProtocol, Sequence[Subprotocol]],
                Optional[Subprotocol],
            ]
        ] = None,
        state: State = CONNECTING,
        max_size: Optional[int] = 2**20,
        logger: Optional[LoggerLike] = None,
    ):
        super().__init__(
            side=SERVER,
            state=state,
            max_size=max_size,
            logger=logger,
        )
        self.origins = origins
        self.available_extensions = extensions
        self.available_subprotocols = subprotocols
        if select_subprotocol is not None:
            # Bind select_subprotocol then shadow self.select_subprotocol.
            # Use setattr to work around https://github.com/python/mypy/issues/2427.
            setattr(
                self,
                "select_subprotocol",
                select_subprotocol.__get__(self, self.__class__),
            )

    def accept(self, request: Request) -> Response:
        """
        Create a handshake response to accept the connection.

        If the connection cannot be established, the handshake response
        actually rejects the handshake.

        You must send the handshake response with :meth:`send_response`.

        You may modify it before sending it, for example to add HTTP headers.

        Args:
            request: WebSocket handshake request event received from the client.

        Returns:
            WebSocket handshake response event to send to the client.

        """
        try:
            (
                accept_header,
                extensions_header,
                protocol_header,
            ) = self.process_request(request)
        except InvalidOrigin as exc:
            request._exception = exc
            self.handshake_exc = exc
            if self.debug:
                self.logger.debug("! invalid origin", exc_info=True)
            return self.reject(
                http.HTTPStatus.FORBIDDEN,
                f"Failed to open a WebSocket connection: {exc}.\n",
            )
        except InvalidUpgrade as exc:
            request._exception = exc
            self.handshake_exc = exc
            if self.debug:
                self.logger.debug("! invalid upgrade", exc_info=True)
            response = self.reject(
                http.HTTPStatus.UPGRADE_REQUIRED,
                (
                    f"Failed to open a WebSocket connection: {exc}.\n"
                    f"\n"
                    f"You cannot access a WebSocket server directly "
                    f"with a browser. You need a WebSocket client.\n"
                ),
            )
            response.headers["Upgrade"] = "websocket"
            return response
        except InvalidHandshake as exc:
            request._exception = exc
            self.handshake_exc = exc
            if self.debug:
                self.logger.debug("! invalid handshake", exc_info=True)
            return self.reject(
                http.HTTPStatus.BAD_REQUEST,
                f"Failed to open a WebSocket connection: {exc}.\n",
            )
        except Exception as exc:
            # Handle exceptions raised by user-provided select_subprotocol and
            # unexpected errors.
            request._exception = exc
            self.handshake_exc = exc
            self.logger.error("opening handshake failed", exc_info=True)
            return self.reject(
                http.HTTPStatus.INTERNAL_SERVER_ERROR,
                (
                    "Failed to open a WebSocket connection.\n"
                    "See server log for more information.\n"
                ),
            )

        headers = Headers()

        headers["Date"] = email.utils.formatdate(usegmt=True)

        headers["Upgrade"] = "websocket"
        headers["Connection"] = "Upgrade"
        headers["Sec-WebSocket-Accept"] = accept_header

        if extensions_header is not None:
            headers["Sec-WebSocket-Extensions"] = extensions_header

        if protocol_header is not None:
            headers["Sec-WebSocket-Protocol"] = protocol_header

        self.logger.info("connection open")
        return Response(101, "Switching Protocols", headers)

    def process_request(
        self,
        request: Request,
    ) -> Tuple[str, Optional[str], Optional[str]]:
        """
        Check a handshake request and negotiate extensions and subprotocol.

        This function doesn't verify that the request is an HTTP/1.1 or higher
        GET request and doesn't check the ``Host`` header. These controls are
        usually performed earlier in the HTTP request handling code. They're
        the responsibility of the caller.

        Args:
            request: WebSocket handshake request received from the client.

        Returns:
            Tuple[str, Optional[str], Optional[str]]:
            ``Sec-WebSocket-Accept``, ``Sec-WebSocket-Extensions``, and
            ``Sec-WebSocket-Protocol`` headers for the handshake response.

        Raises:
            InvalidHandshake: if the handshake request is invalid;
                then the server must return 400 Bad Request error.

        """
        headers = request.headers

        connection: List[ConnectionOption] = sum(
            [parse_connection(value) for value in headers.get_all("Connection")], []
        )

        if not any(value.lower() == "upgrade" for value in connection):
            raise InvalidUpgrade(
                "Connection", ", ".join(connection) if connection else None
            )

        upgrade: List[UpgradeProtocol] = sum(
            [parse_upgrade(value) for value in headers.get_all("Upgrade")], []
        )

        # For compatibility with non-strict implementations, ignore case when
        # checking the Upgrade header. The RFC always uses "websocket", except
        # in section 11.2. (IANA registration) where it uses "WebSocket".
        if not (len(upgrade) == 1 and upgrade[0].lower() == "websocket"):
            raise InvalidUpgrade("Upgrade", ", ".join(upgrade) if upgrade else None)

        try:
            key = headers["Sec-WebSocket-Key"]
        except KeyError as exc:
            raise InvalidHeader("Sec-WebSocket-Key") from exc
        except MultipleValuesError as exc:
            raise InvalidHeader(
                "Sec-WebSocket-Key", "more than one Sec-WebSocket-Key header found"
            ) from exc

        try:
            raw_key = base64.b64decode(key.encode(), validate=True)
        except binascii.Error as exc:
            raise InvalidHeaderValue("Sec-WebSocket-Key", key) from exc
        if len(raw_key) != 16:
            raise InvalidHeaderValue("Sec-WebSocket-Key", key)

        try:
            version = headers["Sec-WebSocket-Version"]
        except KeyError as exc:
            raise InvalidHeader("Sec-WebSocket-Version") from exc
        except MultipleValuesError as exc:
            raise InvalidHeader(
                "Sec-WebSocket-Version",
                "more than one Sec-WebSocket-Version header found",
            ) from exc

        if version != "13":
            raise InvalidHeaderValue("Sec-WebSocket-Version", version)

        accept_header = accept_key(key)

        self.origin = self.process_origin(headers)

        extensions_header, self.extensions = self.process_extensions(headers)

        protocol_header = self.subprotocol = self.process_subprotocol(headers)

        return (
            accept_header,
            extensions_header,
            protocol_header,
        )

    def process_origin(self, headers: Headers) -> Optional[Origin]:
        """
        Handle the Origin HTTP request header.

        Args:
            headers: WebSocket handshake request headers.

        Returns:
           Optional[Origin]: origin, if it is acceptable.

        Raises:
            InvalidHandshake: if the Origin header is invalid.
            InvalidOrigin: if the origin isn't acceptable.

        """
        # "The user agent MUST NOT include more than one Origin header field"
        # per https://www.rfc-editor.org/rfc/rfc6454.html#section-7.3.
        try:
            origin = cast(Optional[Origin], headers.get("Origin"))
        except MultipleValuesError as exc:
            raise InvalidHeader("Origin", "more than one Origin header found") from exc
        if self.origins is not None:
            if origin not in self.origins:
                raise InvalidOrigin(origin)
        return origin

    def process_extensions(
        self,
        headers: Headers,
    ) -> Tuple[Optional[str], List[Extension]]:
        """
        Handle the Sec-WebSocket-Extensions HTTP request header.

        Accept or reject each extension proposed in the client request.
        Negotiate parameters for accepted extensions.

        Per :rfc:`6455`, negotiation rules are defined by the specification of
        each extension.

        To provide this level of flexibility, for each extension proposed by
        the client, we check for a match with each extension available in the
        server configuration. If no match is found, the extension is ignored.

        If several variants of the same extension are proposed by the client,
        it may be accepted several times, which won't make sense in general.
        Extensions must implement their own requirements. For this purpose,
        the list of previously accepted extensions is provided.

        This process doesn't allow the server to reorder extensions. It can
        only select a subset of the extensions proposed by the client.

        Other requirements, for example related to mandatory extensions or the
        order of extensions, may be implemented by overriding this method.

        Args:
            headers: WebSocket handshake request headers.

        Returns:
            Tuple[Optional[str], List[Extension]]: ``Sec-WebSocket-Extensions``
            HTTP response header and list of accepted extensions.

        Raises:
            InvalidHandshake: if the Sec-WebSocket-Extensions header is invalid.

        """
        response_header_value: Optional[str] = None

        extension_headers: List[ExtensionHeader] = []
        accepted_extensions: List[Extension] = []

        header_values = headers.get_all("Sec-WebSocket-Extensions")

        if header_values and self.available_extensions:
            parsed_header_values: List[ExtensionHeader] = sum(
                [parse_extension(header_value) for header_value in header_values], []
            )

            for name, request_params in parsed_header_values:
                for ext_factory in self.available_extensions:
                    # Skip non-matching extensions based on their name.
                    if ext_factory.name != name:
                        continue

                    # Skip non-matching extensions based on their params.
                    try:
                        response_params, extension = ext_factory.process_request_params(
                            request_params, accepted_extensions
                        )
                    except NegotiationError:
                        continue

                    # Add matching extension to the final list.
                    extension_headers.append((name, response_params))
                    accepted_extensions.append(extension)

                    # Break out of the loop once we have a match.
                    break

                # If we didn't break from the loop, no extension in our list
                # matched what the client sent. The extension is declined.

        # Serialize extension header.
        if extension_headers:
            response_header_value = build_extension(extension_headers)

        return response_header_value, accepted_extensions

    def process_subprotocol(self, headers: Headers) -> Optional[Subprotocol]:
        """
        Handle the Sec-WebSocket-Protocol HTTP request header.

        Args:
            headers: WebSocket handshake request headers.

        Returns:
           Optional[Subprotocol]: Subprotocol, if one was selected; this is
           also the value of the ``Sec-WebSocket-Protocol`` response header.

        Raises:
            InvalidHandshake: if the Sec-WebSocket-Subprotocol header is invalid.

        """
        subprotocols: Sequence[Subprotocol] = sum(
            [
                parse_subprotocol(header_value)
                for header_value in headers.get_all("Sec-WebSocket-Protocol")
            ],
            [],
        )

        return self.select_subprotocol(subprotocols)

    def select_subprotocol(
        self,
        subprotocols: Sequence[Subprotocol],
    ) -> Optional[Subprotocol]:
        """
        Pick a subprotocol among those offered by the client.

        If several subprotocols are supported by both the client and the server,
        pick the first one in the list declared the server.

        If the server doesn't support any subprotocols, continue without a
        subprotocol, regardless of what the client offers.

        If the server supports at least one subprotocol and the client doesn't
        offer any, abort the handshake with an HTTP 400 error.

        You provide a ``select_subprotocol`` argument to :class:`ServerProtocol`
        to override this logic. For example, you could accept the connection
        even if client doesn't offer a subprotocol, rather than reject it.

        Here's how to negotiate the ``chat`` subprotocol if the client supports
        it and continue without a subprotocol otherwise::

            def select_subprotocol(protocol, subprotocols):
                if "chat" in subprotocols:
                    return "chat"

        Args:
            subprotocols: list of subprotocols offered by the client.

        Returns:
            Optional[Subprotocol]: Selected subprotocol, if a common subprotocol
            was found.

            :obj:`None` to continue without a subprotocol.

        Raises:
            NegotiationError: custom implementations may raise this exception
                to abort the handshake with an HTTP 400 error.

        """
        # Server doesn't offer any subprotocols.
        if not self.available_subprotocols:  # None or empty list
            return None

        # Server offers at least one subprotocol but client doesn't offer any.
        if not subprotocols:
            raise NegotiationError("missing subprotocol")

        # Server and client both offer subprotocols. Look for a shared one.
        proposed_subprotocols = set(subprotocols)
        for subprotocol in self.available_subprotocols:
            if subprotocol in proposed_subprotocols:
                return subprotocol

        # No common subprotocol was found.
        raise NegotiationError(
            "invalid subprotocol; expected one of "
            + ", ".join(self.available_subprotocols)
        )

    def reject(
        self,
        status: http.HTTPStatus,
        text: str,
    ) -> Response:
        """
        Create a handshake response to reject the connection.

        A short plain text response is the best fallback when failing to
        establish a WebSocket connection.

        You must send the handshake response with :meth:`send_response`.

        You can modify it before sending it, for example to alter HTTP headers.

        Args:
            status: HTTP status code.
            text: HTTP response body; will be encoded to UTF-8.

        Returns:
            Response: WebSocket handshake response event to send to the client.

        """
        body = text.encode()
        headers = Headers(
            [
                ("Date", email.utils.formatdate(usegmt=True)),
                ("Connection", "close"),
                ("Content-Length", str(len(body))),
                ("Content-Type", "text/plain; charset=utf-8"),
            ]
        )
        response = Response(status.value, status.phrase, headers, body)
        # When reject() is called from accept(), handshake_exc is already set.
        # If a user calls reject(), set handshake_exc to guarantee invariant:
        # "handshake_exc is None if and only if opening handshake succeeded."
        if self.handshake_exc is None:
            self.handshake_exc = InvalidStatus(response)
        self.logger.info("connection failed (%d %s)", status.value, status.phrase)
        return response

    def send_response(self, response: Response) -> None:
        """
        Send a handshake response to the client.

        Args:
            response: WebSocket handshake response event to send.

        """
        if self.debug:
            code, phrase = response.status_code, response.reason_phrase
            self.logger.debug("> HTTP/1.1 %d %s", code, phrase)
            for key, value in response.headers.raw_items():
                self.logger.debug("> %s: %s", key, value)
            if response.body is not None:
                self.logger.debug("> [body] (%d bytes)", len(response.body))

        self.writes.append(response.serialize())

        if response.status_code == 101:
            assert self.state is CONNECTING
            self.state = OPEN
        else:
            self.send_eof()
            self.parser = self.discard()
            next(self.parser)  # start coroutine

    def parse(self) -> Generator[None, None, None]:
        if self.state is CONNECTING:
            try:
                request = yield from Request.parse(
                    self.reader.read_line,
                )
            except Exception as exc:
                self.handshake_exc = exc
                self.send_eof()
                self.parser = self.discard()
                next(self.parser)  # start coroutine
                yield

            if self.debug:
                self.logger.debug("< GET %s HTTP/1.1", request.path)
                for key, value in request.headers.raw_items():
                    self.logger.debug("< %s: %s", key, value)

            self.events.append(request)

        yield from super().parse()


class ServerConnection(ServerProtocol):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        warnings.warn(
            "ServerConnection was renamed to ServerProtocol",
            DeprecationWarning,
        )
        super().__init__(*args, **kwargs)
