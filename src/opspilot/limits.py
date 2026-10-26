"""OpsPilot safety policy, not universal infrastructure sizing recommendations.

These are application retrieval/output limits, not hard wall-clock or process
memory guarantees. SDKs may buffer a response (or a PostgreSQL row) first.
Central serialized MCP wire-size caps are deliberately deferred.
"""

from functools import wraps
from typing import Annotated

from pydantic import Field

POSTGRES_CONNECT_TIMEOUT_SECONDS = 5
POSTGRES_STATEMENT_TIMEOUT_MS = 10_000
DOCKER_REQUEST_TIMEOUT_SECONDS = 10
KUBERNETES_CONNECT_TIMEOUT_SECONDS = 5
KUBERNETES_READ_TIMEOUT_SECONDS = 10
MAX_QUERY_ROWS = 500
MAX_LIST_ITEMS = 500
LIST_PAGE_SIZE = 100
DEFAULT_LOG_TAIL = 100
MAX_LOG_TAIL = 1_000
MAX_VALUE_BYTES = 64 * 1024
MAX_LOG_BYTES = 256 * 1024
MAX_OBSERVATION_BYTES = 1024 * 1024
MAX_DIAGNOSTIC_FINDINGS = 100

LogTail = Annotated[int, Field(strict=True, ge=1, le=MAX_LOG_TAIL)]


class ObservationError(ValueError):
    """Safe application failure; messages must never interpolate observations."""


class ObservationLimitError(ObservationError):
    def __init__(self):
        super().__init__("Observation exceeds an OpsPilot safety limit")


class ObservationTimeoutError(ObservationError):
    def __init__(self):
        super().__init__("Infrastructure observation timed out")


def validate_positive_integer(value, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ObservationError("Expected a positive integer within the safety limit")
    return value


def validate_tail(value):
    try:
        return validate_positive_integer(value, MAX_LOG_TAIL)
    except ObservationError:
        raise ObservationError("tail must be an integer between 1 and 1000") from None


def utf8_size(text):
    """Count UTF-8 bytes without making a second full encoded buffer."""
    return sum(len(text[start:start + 4096].encode("utf-8"))
               for start in range(0, len(text), 4096))


def check_log(text):
    if utf8_size(text) > MAX_LOG_BYTES:
        raise ObservationLimitError()
    return text


class ObservationBudget:
    """Incremental logical payload budget, including keys and container overhead.

    This is deliberately not a reproduction of MCP's JSON serialization. Scalar
    checks occur after SDK conversion; JSON escaping and MCP duplication can grow
    the final wire representation.
    """

    def __init__(self, maximum=MAX_OBSERVATION_BYTES, value_maximum=MAX_VALUE_BYTES):
        self.maximum = maximum
        self.value_maximum = value_maximum
        self.used = 0

    def _charge(self, size):
        if self.used + size > self.maximum:
            raise ObservationLimitError()
        self.used += size

    def start_list(self):
        """Charge the container once before incrementally adding list items."""
        self._charge(2)
        return self

    def add_item(self, value):
        """Use the same item overhead for streamed and complete lists."""
        self._charge(1)
        return self.add(value)

    def add(self, value):
        if isinstance(value, dict):
            self._charge(2)
            for key, child in value.items():
                self.add(key)
                self._charge(2)
                self.add(child)
        elif isinstance(value, (list, tuple)):
            self.start_list()
            for child in value:
                self.add_item(child)
        else:
            size = len(value) if isinstance(value, (bytes, bytearray, memoryview)) else utf8_size(str(value))
            if size > self.value_maximum:
                raise ObservationLimitError()
            self._charge(size + (2 if isinstance(value, str) else 0))
        return value


def check_value(value):
    """Bound a database field, including a composite JSON/array value."""
    if isinstance(value, (dict, list, tuple)):
        return ObservationBudget(maximum=MAX_VALUE_BYTES).add(value)
    return ObservationBudget(maximum=MAX_VALUE_BYTES + 2).add(value)


def checked_observation(value):
    return ObservationBudget().add(value)


def bounded_tool_result(function=None, *, logs=False):
    """Lightweight pre-MCP-conversion check, preserving successful shapes."""
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            from mcp.server.mcpserver.exceptions import ToolError
            try:
                result = fn(*args, **kwargs)
                if logs:
                    check_log(result)
                    return ObservationBudget(value_maximum=MAX_LOG_BYTES).add(result)
                return checked_observation(result)
            except ObservationError as exc:
                raise ToolError(str(exc)) from None
        return wrapped
    return decorate(function) if function is not None else decorate
