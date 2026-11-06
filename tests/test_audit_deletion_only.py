"""An accepting audit of a change that only deletes files can rest on the review diff."""
import pytest

from compute_plane.native_journal import save_json
from relay import native_artifacts
from relay.conversation_review import audit_result

DIFF = {'repository': 'review', 'path': 'diff.patch', 'sha256': 'd'*64}
APP = {'repository': 'candidate', 'path': 'app.py', 'sha256': 'a'*64}


def accepted(tmp_path, sources):
    path = tmp_path/'artifact.json'
    save_json(path, {'schema': 'tare.tools/native-artifact/1', 'role': 'auditor', 'kind': native_artifacts.KINDS['auditor'],
                     'authority': 'NONE', 'session_id': 's', 'call_id': 's/1', 'profile_digest': 'b'*64,
                     'payload': {'verdict': 'ACCEPT', 'summary': 'The diff deletes legacy.py only.', 'findings': [],
                                 'remaining': [], 'sources': sources}})
    return {'status': 'PROPOSED', 'artifact_file': str(path), 'session_id': 's', 'profile_digest': 'b'*64}


def test_a_deletion_only_change_is_accepted_on_the_review_diff_and_a_mixed_one_is_not(tmp_path):
    config, unit = {'repositories': {'repo': str(tmp_path)}}, {'repository': 'repo'}
    # legacy.py is deleted: the candidate holds no changed file, only the plan's app.py beside the diff.
    result = audit_result(accepted(tmp_path, [APP, DIFF]), config, unit, {'legacy.py'}, [APP, DIFF], [])
    assert result['verdict'] == 'ACCEPT'
    edited = {**APP, 'path': 'main.py'}  # a changed file the candidate still holds must be cited
    with pytest.raises(ValueError, match='must cite an observed changed candidate file'):
        audit_result(accepted(tmp_path, [APP, DIFF]), config, unit, {'legacy.py', 'main.py'}, [edited, APP, DIFF], [])
