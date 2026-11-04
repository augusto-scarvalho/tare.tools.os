"""Native composition preserves the existing unordered exact criterion set."""
from types import SimpleNamespace

import pytest

from relay.native_work_context import work_packet


def test_criterion_order_is_not_a_new_admission_rule():
    descriptor = SimpleNamespace(work_id='w', sha256='a'*64, execution_repository='r', tracked_paths=('src',),
        verification_checks=[SimpleNamespace(criterion=c,command=('python','-V')) for c in ('unit','lint')])
    node={'exit_criteria':['lint','unit']}
    packet,_=work_packet(descriptor,node,{'r':'/explicit/root'})
    assert set(packet['criteria']) == {'lint','unit'}
    assert set(packet['verification_commands']) == {'lint','unit'}
    node['exit_criteria']=['unit','lint','unit']
    with pytest.raises(ValueError,match='NATIVE_WORK_CRITERIA_MISMATCH'):
        work_packet(descriptor,node,{'r':'/explicit/root'})
