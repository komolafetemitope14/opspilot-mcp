import pytest

from opspilot import limits


def test_fixed_policy():
    assert (limits.POSTGRES_CONNECT_TIMEOUT_SECONDS, limits.POSTGRES_STATEMENT_TIMEOUT_MS) == (5, 10_000)
    assert limits.DOCKER_REQUEST_TIMEOUT_SECONDS == 10
    assert (limits.KUBERNETES_CONNECT_TIMEOUT_SECONDS, limits.KUBERNETES_READ_TIMEOUT_SECONDS) == (5, 10)
    assert (limits.MAX_QUERY_ROWS, limits.MAX_LIST_ITEMS, limits.LIST_PAGE_SIZE) == (500, 500, 100)
    assert (limits.DEFAULT_LOG_TAIL, limits.MAX_LOG_TAIL) == (100, 1000)
    assert (limits.MAX_VALUE_BYTES, limits.MAX_LOG_BYTES, limits.MAX_OBSERVATION_BYTES) == (65536, 262144, 1048576)
    assert limits.MAX_DIAGNOSTIC_FINDINGS == 100


@pytest.mark.parametrize('value', [True, False, 0, -1, 1001, 1.0, '1', None])
def test_tail_rejects_non_strict_or_out_of_range(value):
    with pytest.raises(limits.ObservationError, match='integer between 1 and 1000'):
        limits.validate_tail(value)


@pytest.mark.parametrize('value', [1, 100, 1000])
def test_tail_boundaries(value):
    assert limits.validate_tail(value) == value


def test_utf8_and_value_boundaries():
    assert limits.utf8_size('aé🙂') == 7
    assert limits.utf8_size('é' * 5000) == 10000
    assert limits.checked_observation('é' * 32768) == 'é' * 32768
    with pytest.raises(limits.ObservationLimitError):
        limits.checked_observation('é' * 32768 + 'a')


def test_accumulated_cap_and_cap_plus_one():
    budget = limits.ObservationBudget(maximum=10)
    budget.add('abc')  # five including string delimiters
    budget.add('def')
    assert budget.used == 10
    with pytest.raises(limits.ObservationLimitError):
        budget.add(1)
    assert budget.used == 10


def test_keys_and_nested_values_are_accounted():
    budget = limits.ObservationBudget()
    budget.add({'é': ['🙂', None]})
    assert budget.used > 6
    with pytest.raises(limits.ObservationLimitError):
        limits.checked_observation({'x' * (limits.MAX_VALUE_BYTES + 1): 0})


@pytest.mark.parametrize('extra', [0, 1])
def test_log_byte_boundary(extra):
    text = 'é' * (limits.MAX_LOG_BYTES // 2) + 'x' * extra
    if extra:
        with pytest.raises(limits.ObservationLimitError):
            limits.check_log(text)
    else:
        assert limits.check_log(text) is text


def test_pre_return_check_preserves_shape_and_does_not_serialize():
    value = [{'name': 'web'}]
    assert limits.bounded_tool_result(lambda: value)() is value
    # This checks logical payload; JSON escaping/wire duplication are deferred.
    assert limits.checked_observation('\x00' * limits.MAX_VALUE_BYTES)


def test_safe_tool_error():
    from mcp.server.mcpserver.exceptions import ToolError
    @limits.bounded_tool_result
    def too_large():
        return {'secret': 'private' * limits.MAX_VALUE_BYTES}
    with pytest.raises(ToolError, match='^Observation exceeds an OpsPilot safety limit$'):
        too_large()


def test_positive_integer_validator_and_composite_value():
    assert limits.validate_positive_integer(2, 2) == 2
    for value in (True, False, 0, -1, 3, 2.0):
        with pytest.raises(limits.ObservationError):
            limits.validate_positive_integer(value, 2)
    with pytest.raises(limits.ObservationLimitError):
        limits.check_value(['x' * 1024] * 65)
    assert limits.check_value('x' * limits.MAX_VALUE_BYTES)


@pytest.mark.parametrize("extra", [-1, 0, 1])
def test_incremental_and_whole_list_agree_at_one_mib(extra):
    # 2 list bytes + 16 * (1 item + 9 dict/key/value overhead), plus
    # UTF-8 payloads: exactly the real policy ceiling, with bounded fields.
    tail = "é" * 32686 + "aa"
    tail = tail[:-1] if extra == -1 else tail + "a" * extra
    rows = [{"v": "é" * 32768} for _ in range(15)] + [{"v": tail}]

    def incremental():
        budget = limits.ObservationBudget().start_list()
        for row in rows:
            budget.add_item(row)
        return budget

    if extra == 1:
        with pytest.raises(limits.ObservationLimitError):
            incremental()
        with pytest.raises(limits.ObservationLimitError):
            limits.checked_observation(rows)
    else:
        assert incremental().used == limits.MAX_OBSERVATION_BYTES + extra
        assert limits.checked_observation(rows) is rows
        whole = limits.ObservationBudget()
        whole.add(rows)
        assert whole.used == incremental().used


def test_incremental_nested_list_accounting_matches_whole():
    rows = [{"é": [None, "🙂", [1, 2]]}, [], "tail"]
    incremental = limits.ObservationBudget().start_list()
    for row in rows:
        incremental.add_item(row)
    whole = limits.ObservationBudget()
    whole.add(rows)
    assert incremental.used == whole.used
