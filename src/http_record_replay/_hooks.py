"""The transport hooks the log, the recorder and replay plug into.

One patch per HTTP library, on the one method every request of that library passes
through, so calls made by clients built deep inside an SDK are seen too:
- requests (`HTTPAdapter.send`): plain REST calls, the azure-* SDKs and azure-identity
- urllib3 (`HTTPConnectionPool.urlopen`), when it is called directly rather than through
  requests: botocore (boto3, the AWS SDK)
- httpx2 and httpx (`HTTPTransport.handle_request` and its async twin): the openai and
  anthropic SDKs, Semantic Kernel, AutoGen, the MCP client
- aiohttp: a `TraceConfig` added to every new `ClientSession` (aiohttp's own tracing
  hooks) for real calls, and `ClientSession._request` while a responder answers: the
  async azure-* SDK clients

How it works:
1. Each request becomes an Exchange, and every observer's `on_request()` is called.
2. With a responder set (replay), the responder's Answer stands in for the network; else
   the library sends the request as usual.
3. The response body is teed as the caller reads it, so a streamed reply passes through
   at its own pace; observers get `on_response()` once the body has been read, with the
   whole body (decompressed) up to the largest `capture_limit()` an observer asked for.
4. A failure (connection error, replay miss) goes to `on_error()`.

The patches are in place only while an observer or a responder is set.
"""

import atexit
import contextvars
import io
import itertools
import json
import threading
import time
import zlib
from typing import NamedTuple

from . import redact
from ._patches import Patches

STREAMED_BODY = "<streamed body, not logged>"


class Answer(NamedTuple):
    """A response given by a responder in place of the network."""

    status: int
    reason: str
    headers: list
    body: bytes


class Observer:
    """Base class for the log and the recorder: every method may be overridden."""

    def on_request(self, exchange):
        pass

    def on_response(self, exchange, body, note):
        """body: bytes (decompressed), or a str note when it couldn't be decoded; note:
        why the body is incomplete (not read to the end, ...), else None."""

    def on_error(self, exchange, error):
        pass

    def capture_limit(self, exchange):
        """Bytes of the response body this observer needs (float("inf") for all)."""
        return 0


_patches = Patches()
_observers = []
_state = {"responder": None}
_lock = threading.Lock()
_numbers = itertools.count(1)
_pending = set()
# Set while requests' send runs, so its inner urllib3 call isn't seen twice.
_inside_send = contextvars.ContextVar("http_record_replay_inside_send", default=False)


def add_observer(observer):
    if observer not in _observers:
        _observers.append(observer)
    _refresh()


def remove_observer(observer):
    if observer in _observers:
        _observers.remove(observer)
    _refresh()


def set_responder(responder):
    """responder(exchange) -> Answer, or raises; None goes back to real calls."""
    _state["responder"] = responder
    _refresh()


def active():
    return bool(_patches)


def _refresh():
    wanted = bool(_observers) or _state["responder"] is not None
    if wanted and not _patches:
        _patch_requests()
        _patch_urllib3()
        for name in ("httpx2", "httpx"):
            _patch_httpx(name)
        _patch_aiohttp()
    elif not wanted and _patches:
        _patches.restore()


def header_value(headers, name):
    """The first value of header `name` (any case) in a list of (name, value) pairs."""
    lower = name.lower()
    return next((value for key, value in headers if key.lower() == lower), None)


def _text_headers(headers):
    """(name, value) pairs as str: botocore, for one, sends some values as bytes."""

    def text(item):
        return item.decode("latin-1") if isinstance(item, (bytes, bytearray)) else str(item)

    return [(text(name), text(value)) for name, value in headers]


# --------------------------------------------------------------------------------------
# One HTTP call.


class Exchange:
    """One request and its response, as sent and received (the URL with its secret
    parameters redacted)."""

    def __init__(self, method, url, headers, body):
        self.number = next(_numbers)
        self.method = method
        self.url = redact.url(str(url))
        self.request_headers = _text_headers(headers)
        self.request_body = body  # bytes, None, or a note such as STREAMED_BODY
        self.sent = len(body) if isinstance(body, bytes) else 0
        self.start = time.perf_counter()
        self.seconds = None
        self.done = False
        self.status = None
        self.reason = ""
        self.headers = []
        self.encoding = None  # Content-Encoding the teed bytes still have, if any
        self.chunks = []
        self.captured = 0
        self.received = 0
        self.capture_limit = 0
        self.notes = {}  # observers' own per-exchange state
        with _lock:
            _pending.add(self)
        for observer in _observers.copy():  # an observer may remove itself
            observer.on_request(self)

    @property
    def request_content_type(self):
        return header_value(self.request_headers, "content-type")

    @property
    def content_type(self):
        return header_value(self.headers, "content-type")

    def response(self, status, reason, headers, encoding=None):
        self.status, self.reason, self.headers = status, reason or "", _text_headers(headers)
        self.encoding = encoding
        self.capture_limit = max((observer.capture_limit(self) for observer in _observers), default=0)

    def record(self, chunk):
        self.received += len(chunk)
        if self.captured < self.capture_limit:
            self.chunks.append(chunk)
            self.captured += len(chunk)

    def finish(self, note=None):
        if self.done:
            return
        self.done = True
        self.seconds = time.perf_counter() - self.start
        with _lock:
            _pending.discard(self)
        body = _decode(b"".join(self.chunks), self.encoding)
        for observer in _observers.copy():  # an observer may remove itself
            observer.on_response(self, body, note)

    def failed(self, error):
        if self.done:
            return
        self.done = True
        self.seconds = time.perf_counter() - self.start
        with _lock:
            _pending.discard(self)
        for observer in _observers.copy():  # an observer may remove itself
            observer.on_error(self, error)


def finish_pending():
    """Finish the calls whose body was never read to the end (at exit)."""
    with _lock:
        pending = list(_pending)
    for exchange in sorted(pending, key=lambda exchange: exchange.number):
        exchange.finish(note="body not read by the script" if exchange.status is not None else "no response")


atexit.register(finish_pending)


def _decode(data, encoding):
    """Undo gzip/deflate on body bytes that are still compressed (a raw transport
    stream)."""
    if not data or not encoding:
        return data
    encoding = encoding.lower()
    # decompressobj also handles a body cut at its capture limit; deflate is
    # zlib-wrapped or raw, depending on the server.
    window_bits = {"gzip": [16 + zlib.MAX_WBITS], "deflate": [zlib.MAX_WBITS, -zlib.MAX_WBITS]}.get(encoding, [])
    for bits in window_bits:
        try:
            return zlib.decompressobj(bits).decompress(data)
        except zlib.error:
            continue
    return f"<{encoding}-compressed body, {len(data)} bytes>"


def _request_body(body):
    if isinstance(body, str):
        return body.encode()
    if body is None or isinstance(body, bytes):
        return body
    if isinstance(body, (bytearray, memoryview)):
        return bytes(body)
    return STREAMED_BODY


def _answer(exchange):
    """The responder's answer, with the failure passed to the observers."""
    try:
        return _state["responder"](exchange)
    except Exception as error:
        exchange.failed(error)
        raise


# --------------------------------------------------------------------------------------
# requests: wrap HTTPAdapter.send, and tee the raw urllib3 response so the body is seen
# as it is read.


def _patch_requests():
    try:
        from requests.adapters import HTTPAdapter
        from urllib3.response import HTTPResponse
    except ImportError:
        return
    original = HTTPAdapter.send

    def send(self, request, **kwargs):
        exchange = Exchange(request.method, request.url, request.headers.items(), _request_body(request.body))
        if _state["responder"] is not None:
            answer = _answer(exchange)
            raw = HTTPResponse(
                body=io.BytesIO(answer.body),
                headers=answer.headers,
                status=answer.status,
                reason=answer.reason,
                preload_content=False,
            )
            response = self.build_response(request, raw)
        else:
            token = _inside_send.set(True)
            try:
                response = original(self, request, **kwargs)
            except Exception as error:
                exchange.failed(error)
                raise
            finally:
                _inside_send.reset(token)
        exchange.response(response.status_code, response.reason, response.headers.items())
        response.raw = _RawTee(response.raw, exchange)
        return response

    _patches.set(HTTPAdapter, "send", send)


class _RawTee:
    """Stands in for urllib3's HTTPResponse under requests: passes every read through
    and keeps a copy for the observers."""

    def __init__(self, raw, exchange):
        object.__setattr__(self, "_raw", raw)
        object.__setattr__(self, "_exchange", exchange)

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __setattr__(self, name, value):
        setattr(self._raw, name, value)

    def _keep(self, chunk, decode_content):
        _keep_chunk(self._exchange, self._raw, chunk, decode_content)

    def stream(self, amt=2**16, decode_content=None):
        for chunk in self._raw.stream(amt, decode_content=decode_content):
            self._keep(chunk, decode_content)
            yield chunk
        self._exchange.finish()

    def read(self, amt=None, decode_content=None, **kwargs):
        data = self._raw.read(amt, decode_content=decode_content, **kwargs)
        self._keep(data, decode_content)
        if amt is None or not data:
            self._exchange.finish()
        return data

    def close(self):
        self._exchange.finish(note="closed before the body was read" if not self._exchange.received else None)
        self._raw.close()


def _keep_chunk(exchange, raw, chunk, decode_content):
    decoded = decode_content if decode_content is not None else getattr(raw, "decode_content", True)
    if not decoded:
        exchange.encoding = header_value(exchange.headers, "content-encoding")
    exchange.record(chunk)


# --------------------------------------------------------------------------------------
# urllib3 called directly (botocore): wrap HTTPConnectionPool.urlopen, and tee the
# response's read methods on the instance, so the caller still gets a real HTTPResponse.


def _patch_urllib3():
    try:
        from urllib3.connectionpool import HTTPConnectionPool
        from urllib3.response import HTTPResponse
    except ImportError:
        return
    original = HTTPConnectionPool.urlopen

    def urlopen(self, method, url, *args, **kwargs):
        if _inside_send.get():
            return original(self, method, url, *args, **kwargs)
        body = kwargs.get("body", args[0] if args else None)
        headers = kwargs.get("headers", args[1] if len(args) > 1 else None) or {}
        preload_content = kwargs.get("preload_content", True)
        decode_content = kwargs.get("decode_content", True)
        exchange = Exchange(method, _full_url(self, url), headers.items(), _request_body(body))
        if _state["responder"] is not None:
            answer = _answer(exchange)
            response = HTTPResponse(
                body=io.BytesIO(answer.body),
                headers=answer.headers,
                status=answer.status,
                reason=answer.reason,
                preload_content=preload_content,
                decode_content=decode_content,
            )
        else:
            token = _inside_send.set(True)  # urlopen calls itself on retries and redirects
            try:
                response = original(self, method, url, *args, **kwargs)
            except Exception as error:
                exchange.failed(error)
                raise
            finally:
                _inside_send.reset(token)
        exchange.response(response.status, response.reason, response.headers.items())
        if preload_content:
            # The body has been read into the response already.
            if not decode_content:
                exchange.encoding = header_value(exchange.headers, "content-encoding")
            exchange.record(response.data or b"")
            exchange.finish()
        else:
            _tee_urllib3_response(response, exchange)
        return response

    _patches.set(HTTPConnectionPool, "urlopen", urlopen)


def _full_url(pool, url):
    if "://" in url:
        return url
    default_port = {"http": 80, "https": 443}.get(pool.scheme)
    host = f"[{pool.host}]" if ":" in pool.host else pool.host
    port = "" if pool.port in (None, default_port) else f":{pool.port}"
    return f"{pool.scheme}://{host}{port}{url}"


def _tee_urllib3_response(response, exchange):
    """Wrap read, read1, read_chunked and stream on this response object: stream() reads
    through read() or read_chunked(), so each chunk is kept once."""
    original_read, original_stream = response.read, response.stream
    original_read1 = getattr(response, "read1", None)
    original_read_chunked = getattr(response, "read_chunked", None)

    def read(amt=None, decode_content=None, *args, **kwargs):
        data = original_read(amt, decode_content, *args, **kwargs)
        _keep_chunk(exchange, response, data, decode_content)
        if amt is None or not data:
            exchange.finish()
        return data

    def read1(amt=None, decode_content=None):
        data = original_read1(amt, decode_content)
        _keep_chunk(exchange, response, data, decode_content)
        if not data:
            exchange.finish()
        return data

    def read_chunked(amt=None, decode_content=None):
        for chunk in original_read_chunked(amt, decode_content):
            _keep_chunk(exchange, response, chunk, decode_content)
            yield chunk
        exchange.finish()

    def stream(amt=2**16, decode_content=None):
        yield from original_stream(amt, decode_content)
        exchange.finish()

    response.read, response.stream = read, stream
    if original_read1 is not None:
        response.read1 = read1
    if original_read_chunked is not None:
        response.read_chunked = read_chunked


# --------------------------------------------------------------------------------------
# httpx2 / httpx: wrap the transports, and tee the response stream so the body is seen
# as it is read.


def _patch_httpx(module_name):
    try:
        module = __import__(module_name)
    except ImportError:
        return
    sync_cls, async_cls = module.HTTPTransport, module.AsyncHTTPTransport
    original_sync, original_async = sync_cls.handle_request, async_cls.handle_async_request

    class SyncTee(module.SyncByteStream):
        def __init__(self, stream, exchange):
            self.stream, self.exchange = stream, exchange

        def __iter__(self):
            for chunk in self.stream:
                self.exchange.record(chunk)
                yield chunk
            self.exchange.finish()

        def close(self):
            self.exchange.finish()
            self.stream.close()

    class AsyncTee(module.AsyncByteStream):
        def __init__(self, stream, exchange):
            self.stream, self.exchange = stream, exchange

        async def __aiter__(self):
            async for chunk in self.stream:
                self.exchange.record(chunk)
                yield chunk
            self.exchange.finish()

        async def aclose(self):
            self.exchange.finish()
            await self.stream.aclose()

    class AnswerStream(module.SyncByteStream, module.AsyncByteStream):
        # A stream, not content=: httpx reads content= at once, so the tee would never
        # see the body being read. A stream is read when the caller reads it, as a real
        # one is.
        def __init__(self, data):
            self.data = data

        def __iter__(self):
            yield self.data

        async def __aiter__(self):
            yield self.data

    def items(headers):  # names as sent on the wire; multi_items() would lower-case them
        return [(name.decode("latin-1"), value.decode("latin-1")) for name, value in headers.raw]

    def answered_response(request, answer):
        extensions = {"reason_phrase": answer.reason.encode()} if answer.reason else {}
        return module.Response(
            answer.status,
            headers=answer.headers,
            stream=AnswerStream(answer.body),
            request=request,
            extensions=extensions,
        )

    def tee(exchange, response, tee_class):
        encoding = response.headers.get("content-encoding")
        exchange.response(response.status_code, response.reason_phrase, items(response.headers), encoding)
        response.stream = tee_class(response.stream, exchange)
        return response

    def handle_request(self, request):
        exchange = Exchange(request.method, request.url, items(request.headers), request.read())
        if _state["responder"] is not None:
            return tee(exchange, answered_response(request, _answer(exchange)), SyncTee)
        try:
            response = original_sync(self, request)
        except Exception as error:
            exchange.failed(error)
            raise
        return tee(exchange, response, SyncTee)

    async def handle_async_request(self, request):
        exchange = Exchange(request.method, request.url, items(request.headers), await request.aread())
        if _state["responder"] is not None:
            return tee(exchange, answered_response(request, _answer(exchange)), AsyncTee)
        try:
            response = await original_async(self, request)
        except Exception as error:
            exchange.failed(error)
            raise
        return tee(exchange, response, AsyncTee)

    _patches.set(sync_cls, "handle_request", handle_request)
    _patches.set(async_cls, "handle_async_request", handle_async_request)


# --------------------------------------------------------------------------------------
# aiohttp: real calls through a TraceConfig on every new ClientSession (the request is
# seen when the response headers arrive, the body when `read()` returns it); answered
# calls through ClientSession._request. Sessions made before the hooks went in aren't
# seen.


def _patch_aiohttp():
    try:
        import aiohttp
    except ImportError:
        return
    session_cls, response_cls = aiohttp.ClientSession, aiohttp.ClientResponse
    original_init, original_request = session_cls.__init__, session_cls._request
    original_release, original_close = response_cls.release, response_cls.close

    async def on_request_start(session, ctx, params):
        ctx.headers, ctx.body = list(params.headers.items()), []

    async def on_request_headers_sent(session, ctx, params):
        ctx.headers = list(params.headers.items())  # with the headers aiohttp adds itself

    async def on_request_chunk_sent(session, ctx, params):
        ctx.body.append(params.chunk if isinstance(params.chunk, bytes) else bytes(params.chunk))

    def start(ctx, params):
        return Exchange(params.method, params.url, getattr(ctx, "headers", []), b"".join(getattr(ctx, "body", [])))

    async def on_request_end(session, ctx, params):
        if not active():
            return
        exchange = ctx.exchange = start(ctx, params)
        response = params.response
        # azure-core turns automatic decompression off and decompresses itself: the
        # bytes read are still compressed.
        encoding = None if session.auto_decompress else response.headers.get("Content-Encoding")
        headers = [(name.decode("latin-1"), value.decode("latin-1")) for name, value in response.raw_headers]
        exchange.response(response.status, response.reason, headers, encoding)
        response._http_record_replay_exchange = exchange

    async def on_response_chunk_received(session, ctx, params):
        exchange = getattr(ctx, "exchange", None)
        if exchange is not None and not exchange.done:
            exchange.record(params.chunk)  # read() reports the whole body at once
            exchange.finish()

    async def on_request_exception(session, ctx, params):
        if active():
            start(ctx, params).failed(params.exception)

    def unread(response):
        exchange = getattr(response, "_http_record_replay_exchange", None)
        if exchange is not None and not exchange.done:
            exchange.finish(note="body not read with read(): not logged")

    def init(self, *args, **kwargs):
        trace = aiohttp.TraceConfig()
        trace.on_request_start.append(on_request_start)
        trace.on_request_headers_sent.append(on_request_headers_sent)
        trace.on_request_chunk_sent.append(on_request_chunk_sent)
        trace.on_request_end.append(on_request_end)
        trace.on_response_chunk_received.append(on_response_chunk_received)
        trace.on_request_exception.append(on_request_exception)
        kwargs["trace_configs"] = [*(kwargs.get("trace_configs") or []), trace]
        original_init(self, *args, **kwargs)

    def release(self):
        unread(self)
        return original_release(self)

    def close(self):
        unread(self)
        return original_close(self)

    async def request(self, method, str_or_url, **kwargs):
        if _state["responder"] is None:
            return await original_request(self, method, str_or_url, **kwargs)
        return _answered_aiohttp_request(aiohttp, method, str_or_url, kwargs)

    _patches.set(session_cls, "__init__", init)
    _patches.set(session_cls, "_request", request)
    _patches.set(response_cls, "release", release)
    _patches.set(response_cls, "close", close)


def _answered_aiohttp_request(aiohttp, method, str_or_url, kwargs):
    from multidict import CIMultiDict, CIMultiDictProxy
    from yarl import URL

    url = URL(str_or_url)
    if kwargs.get("params"):
        url = url.extend_query(kwargs["params"])
    data = kwargs.get("data")
    if kwargs.get("json") is not None:
        body = json.dumps(kwargs["json"]).encode()
    elif isinstance(data, dict):
        from urllib.parse import urlencode

        body = urlencode(data).encode()
    else:
        body = _request_body(data)
    header_list = list((kwargs.get("headers") or {}).items())
    exchange = Exchange(method, url, header_list, body)
    answer = _answer(exchange)
    exchange.response(answer.status, answer.reason, answer.headers)
    exchange.record(answer.body)
    exchange.finish()
    headers = CIMultiDictProxy(CIMultiDict(answer.headers))
    return _AnsweredAiohttpResponse(aiohttp, method, url, answer, headers)


class _AnsweredContent:
    """The part of aiohttp's StreamReader that azure-core uses for streamed bodies."""

    def __init__(self, data):
        self._data = io.BytesIO(data)

    async def read(self, n=-1):
        return self._data.read(n)

    async def readany(self):
        return self._data.read()

    def at_eof(self):
        return self._data.tell() == len(self._data.getbuffer())


class _AnsweredAiohttpResponse:
    """Stands in for aiohttp.ClientResponse with a responder's answer."""

    def __init__(self, aiohttp, method, url, answer, headers):
        self._aiohttp = aiohttp
        self.method, self.url, self.status, self.reason = method, url, answer.status, answer.reason
        self.headers = headers
        self.raw_headers = tuple((n.encode("latin-1"), v.encode("latin-1")) for n, v in answer.headers)
        self.content_type = self.headers.get("Content-Type", "application/octet-stream").split(";")[0]
        self.content = _AnsweredContent(answer.body)
        self.ok = answer.status < 400
        self._body = answer.body

    async def read(self):
        return self._body

    async def text(self, encoding="utf-8"):
        return self._body.decode(encoding)

    async def json(self, **kwargs):
        return json.loads(self._body)

    def raise_for_status(self):
        if not self.ok:
            raise self._aiohttp.ClientResponseError(None, (), status=self.status, message=self.reason)

    def release(self):
        pass

    def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass
