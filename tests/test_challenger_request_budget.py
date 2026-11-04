"""The visible finite session budget also bounds inference; no hidden ninety-second cutoff."""
from relay.challenger import bounded
from test_native_agent import deployment
import pytest


@pytest.mark.parametrize('seconds',[15,900])
def test_request_and_read_wait_honor_the_selected_session_budget_without_retries(seconds):
    source=deployment();source['native_profile'].update(protocol='vendor-cli',endpoint='cli://claude')
    result=bounded(source,{'calls':30,'seconds':seconds},0)
    limits=result['native_execution_limits']
    assert limits['request_seconds']==limits['read_seconds']==limits['max_seconds']==seconds
    assert limits['connect_seconds']<=seconds
    assert limits['max_model_calls']==30 and limits['max_model_retries']==0
    assert 'native_execution_limits' not in source
