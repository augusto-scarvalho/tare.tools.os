"""A new direct auditor session yields a validated role artifact through the public CLI."""
import pytest
from relay.console_backend import Selection,command


@pytest.mark.parametrize('role',['auditor','explorer','implementer'])
def test_direct_auditor_gets_structured_output_without_changing_other_roles(tmp_path,role):
    selected=Selection(str(tmp_path),mode='session',role=role,prompt='Inspect the proposed change.',deployment='test',write_paths=['a.py'])
    args=command(selected)
    assert ('--typed-output' in args)==(role=='auditor')
    assert args[args.index('--role')+1]==role
