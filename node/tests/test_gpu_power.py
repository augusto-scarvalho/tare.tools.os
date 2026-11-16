"""The board's power record and GET /v1/gpu/power: the average watts between two Unix times."""
import json
from urllib import error, request

import pytest

from test_gateway_gpu_coordination import gateway, serving
from tare_node.gpu_lease_run import PowerLog


def logged(readings, keep=3600.0):
    clock = [0.0]
    log = PowerLog(keep=keep, clock=lambda: clock[0])
    for at, watts in readings:
        clock[0] = at
        log.add(watts)
    return log


def test_the_average_is_over_the_readings_inside_the_interval_only():
    log = logged([(0, 40), (5, 300), (10, 340), (15, 360), (20, 40)])
    assert log.average(5, 15) == {'watts': 333.3, 'samples': 3}
    assert log.average(10, 10) == {'watts': 340, 'samples': 1}  # the ends count
    assert log.average(0, 100) == {'watts': 216.0, 'samples': 5}


def test_an_interval_without_a_reading_has_none_never_a_guess():
    log = logged([(100, 300)])
    assert log.average(0, 99) is None and log.average(101, 200) is None and PowerLog().average(0, 1e12) is None
    assert log.average(10, 5) is None  # until before since


def test_readings_older_than_the_keep_time_are_dropped():
    log = logged([(0, 40), (1800, 300), (3601, 340)], keep=3600)
    assert [w for _, w in log.readings] == [300, 340] and log.average(0, 10) is None


def test_the_route_answers_the_average_or_says_it_has_none(monkeypatch):
    log = logged([(1000, 100), (1005, 200)])
    monkeypatch.setattr(gateway, 'POWER', log)
    with serving() as rt:
        def get(query):
            with request.urlopen(rt.endpoint + '/v1/gpu/power' + query, timeout=5) as reply:
                return json.load(reply)
        assert get('?since=1000&until=1010') == {'watts': 150.0, 'samples': 2}
        assert get('?since=0&until=10') == {'watts': None, 'samples': 0}
        for bad in ('', '?since=1', '?since=a&until=2'):
            with pytest.raises(error.HTTPError) as refused:
                get(bad)
            assert refused.value.code == 400
