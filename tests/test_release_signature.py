"""Release signatures (ADR-075): the pure-Python Ed25519 verifier, the verify-signature command, the key script and
the release workflow's shape. Offline: OpenSSL runs locally on throwaway keys in tmp_path."""
import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from relay import native_product as product

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_signing_key', ROOT/'scripts/ops/release_signing_key.py')
keys = importlib.util.module_from_spec(spec)
spec.loader.exec_module(keys)

RFC_VECTORS = [  # RFC 8032 section 7.1: (name, public key, message, signature), all hex
    ('TEST 1', 'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a',
    '',
     'e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b'),
    ('TEST 2', '3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c',
    ('72'),
     '92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00'),
    ('TEST 3', 'fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025',
    ('af82'),
     '6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a'),
    ('TEST 1024', '278117fc144c72340f67d0f2316e8386ceffbf2b2428c9c51fef7c597f1d426e',
    ('08b8b2b733424243760fe426a4b54908632110a66c2f6591eabd3345e3e4eb98'
     'fa6e264bf09efe12ee50f8f54e9f77b1e355f6c50544e23fb1433ddf73be84d8'
     '79de7c0046dc4996d9e773f4bc9efe5738829adb26c81b37c93a1b270b20329d'
     '658675fc6ea534e0810a4432826bf58c941efb65d57a338bbd2e26640f89ffbc'
     '1a858efcb8550ee3a5e1998bd177e93a7363c344fe6b199ee5d02e82d522c4fe'
     'ba15452f80288a821a579116ec6dad2b3b310da903401aa62100ab5d1a36553e'
     '06203b33890cc9b832f79ef80560ccb9a39ce767967ed628c6ad573cb116dbef'
     'efd75499da96bd68a8a97b928a8bbc103b6621fcde2beca1231d206be6cd9ec7'
     'aff6f6c94fcd7204ed3455c68c83f4a41da4af2b74ef5c53f1d8ac70bdcb7ed1'
     '85ce81bd84359d44254d95629e9855a94a7c1958d1f8ada5d0532ed8a5aa3fb2'
     'd17ba70eb6248e594e1a2297acbbb39d502f1a8c6eb6f1ce22b3de1a1f40cc24'
     '554119a831a9aad6079cad88425de6bde1a9187ebb6092cf67bf2b13fd65f270'
     '88d78b7e883c8759d2c4f5c65adb7553878ad575f9fad878e80a0c9ba63bcbcc'
     '2732e69485bbc9c90bfbd62481d9089beccf80cfe2df16a2cf65bd92dd597b07'
     '07e0917af48bbb75fed413d238f5555a7a569d80c3414a8d0859dc65a46128ba'
     'b27af87a71314f318c782b23ebfe808b82b0ce26401d2e22f04d83d1255dc51a'
     'ddd3b75a2b1ae0784504df543af8969be3ea7082ff7fc9888c144da2af58429e'
     'c96031dbcad3dad9af0dcbaaaf268cb8fcffead94f3c7ca495e056a9b47acdb7'
     '51fb73e666c6c655ade8297297d07ad1ba5e43f1bca32301651339e22904cc8c'
     '42f58c30c04aafdb038dda0847dd988dcda6f3bfd15c4b4c4525004aa06eeff8'
     'ca61783aacec57fb3d1f92b0fe2fd1a85f6724517b65e614ad6808d6f6ee34df'
     'f7310fdc82aebfd904b01e1dc54b2927094b2db68d6f903b68401adebf5a7e08'
     'd78ff4ef5d63653a65040cf9bfd4aca7984a74d37145986780fc0b16ac451649'
     'de6188a7dbdf191f64b5fc5e2ab47b57f7f7276cd419c17a3ca8e1b939ae49e4'
     '88acba6b965610b5480109c8b17b80e1b7b750dfc7598d5d5011fd2dcc5600a3'
     '2ef5b52a1ecc820e308aa342721aac0943bf6686b64b2579376504ccc493d97e'
     '6aed3fb0f9cd71a43dd497f01f17c0e2cb3797aa2a2f256656168e6c496afc5f'
     'b93246f6b1116398a346f1a641f3b041e989f7914f90cc2c7fff357876e506b5'
     '0d334ba77c225bc307ba537152f3f1610e4eafe595f6d9d90d11faa933a15ef1'
     '369546868a7f3a45a96768d40fd9d03412c091c6315cf4fde7cb68606937380d'
     'b2eaaa707b4c4185c32eddcdd306705e4dc1ffc872eeee475a64dfac86aba41c'
     '0618983f8741c5ef68d3a101e8a3b8cac60c905c15fc910840b94c00a0b9d0'),
     '0aab4c900501b3e24d7cdf4663326a3a87df5e4843b2cbdb67cbf6e460fec350aa5371b1508f9f4528ecea23c436d94b5e8fcd4f681e30a6ac00a9704a188a03'),
]


def find_openssl():
    """An OpenSSL 3 that signs Ed25519 raw (-rawin), found as the key script finds it; None when there is none."""
    path = keys.find_openssl()
    if not path:
        return None
    try:
        done = subprocess.run([path, 'version'], capture_output=True, timeout=30)
    except OSError:
        return None
    return path if done.stdout.startswith(b'OpenSSL 3') else None


def openssl_key(openssl, folder):
    """A throwaway Ed25519 key: (PEM path, raw public key hex)."""
    pem = Path(folder)/'key.pem'
    pem.write_bytes(subprocess.run([openssl, 'genpkey', '-algorithm', 'ed25519'], capture_output=True,
                                   check=True).stdout)
    der = subprocess.run([openssl, 'pkey', '-pubout', '-outform', 'DER', '-in', str(pem)], capture_output=True,
                         check=True).stdout
    return pem, der[-32:].hex()


def sign(openssl, pem, archive, folder):
    message, signature = Path(folder)/'message', Path(folder)/'sig.bin'
    message.write_bytes(product.signed_message(archive))
    subprocess.run([openssl, 'pkeyutl', '-sign', '-inkey', str(pem), '-rawin', '-in', str(message),
                    '-out', str(signature)], check=True, capture_output=True)
    return signature.read_bytes()


@pytest.mark.parametrize('name, public, message, signature', RFC_VECTORS, ids=[v[0] for v in RFC_VECTORS])
def test_rfc_8032_vectors_verify_and_break_with_one_changed_bit(name, public, message, signature):
    public, message, signature = bytes.fromhex(public), bytes.fromhex(message), bytes.fromhex(signature)
    assert product.ed25519_verify(public, message, signature)
    assert not product.ed25519_verify(public, message + b'\0', signature)
    assert not product.ed25519_verify(public, message, bytes([signature[0] ^ 1]) + signature[1:])
    assert not product.ed25519_verify(public, message, signature[:63] + bytes([signature[63] ^ 1]))


def test_verification_is_fast_enough_for_one_check_per_update():
    import time
    _, public, message, signature = RFC_VECTORS[-1]
    started = time.perf_counter()
    assert product.ed25519_verify(bytes.fromhex(public), bytes.fromhex(message), bytes.fromhex(signature))
    assert time.perf_counter() - started < 1


def test_the_signed_message_is_the_prefix_and_the_hex_digest_of_the_file(tmp_path):
    import hashlib
    archive = tmp_path/'bundle.zip'
    archive.write_bytes(b'bundle bytes')
    assert product.SIGNED_PREFIX == b'tare.tools product bundle\n'
    assert product.signed_message(archive) == b'tare.tools product bundle\n' + \
        hashlib.sha256(b'bundle bytes').hexdigest().encode()


def test_openssl_signatures_verify_and_one_changed_byte_does_not(tmp_path):
    openssl = find_openssl()
    if not openssl:
        pytest.skip('no OpenSSL 3 with Ed25519 signing')
    pem, public = openssl_key(openssl, tmp_path)
    archive = tmp_path/'bundle.zip'
    archive.write_bytes(b'PK product bundle ' * 100)
    signature = sign(openssl, pem, archive, tmp_path)
    assert len(signature) == 64
    text = base64.b64encode(signature).decode()
    assert len(text) == 88
    assert product.verify_signature(archive, text, [public])
    assert product.verify_signature(archive, '\n  ' + text + '\r\n', [public])  # as a file or a shell leaves it
    assert product.verify_signature(archive, text, ['00'*32, public])  # any listed key is accepted
    changed = bytearray(archive.read_bytes())
    changed[7] ^= 1
    (tmp_path/'changed.zip').write_bytes(changed)
    assert not product.verify_signature(tmp_path/'changed.zip', text, [public])
    assert not product.verify_signature(archive, base64.b64encode(
        bytes([signature[5] ^ 1]).join([signature[:5], signature[6:]])).decode(), [public])
    assert not product.verify_signature(archive, text, [public[:-1] + format(int(public[-1], 16) ^ 1, 'x')])
    assert not product.verify_signature(archive, text, [])


def test_malformed_input_is_false_and_never_raises(tmp_path):
    _, public, message, signature = RFC_VECTORS[1]
    public, message, signature = bytes.fromhex(public), bytes.fromhex(message), bytes.fromhex(signature)
    assert product.ed25519_verify(public, message, signature)
    for bad_public, bad_signature in ((public[:31], signature), (public + b'\0', signature), (b'', signature),
                                      (public, signature[:63]), (public, signature + b'\0'), (public, b''),
                                      (None, signature), (public, None), (public, 'text')):
        assert product.ed25519_verify(bad_public, message, bad_signature) is False
    # Malleability: S + L is the same point equation but not a canonical signature.
    high_s = int.from_bytes(signature[32:], 'little') + product._L
    assert product.ed25519_verify(public, message, signature[:32] + high_s.to_bytes(32, 'little')) is False
    # y = 2 is not on the curve (no square root), and y >= p does not decode either.
    for not_a_point in ((2).to_bytes(32, 'little'), (product._P).to_bytes(32, 'little'), b'\xff'*32):
        assert product._decode_point(not_a_point) is None
        assert product.ed25519_verify(not_a_point, message, signature) is False
        assert product.ed25519_verify(public, message, not_a_point + signature[32:]) is False
    archive = tmp_path/'bundle.zip'
    archive.write_bytes(b'x')
    good = base64.b64encode(signature).decode()
    for text in ('', '   ', 'not base64!', good[:-4], good + 'AAAA', base64.b64encode(b'short').decode(), 'é'):
        assert product.verify_signature(archive, text, [public.hex()]) is False
    assert product.verify_signature(archive, good, ['zz', '', 'ab'*31, public.hex()]) is False  # bad keys are skipped
    assert product.verify_signature(archive, good, ()) is False
    assert product.verify_signature(archive, good) is False  # this test's key is not one of RELEASE_KEYS
    assert len(product.RELEASE_KEYS) == 2 and all(len(bytes.fromhex(key)) == 32 for key in product.RELEASE_KEYS)


def run_command(*args):
    # The module by path with a bare interpreter: no PYTHONPATH, no --home, no installation, as install.py runs.
    return subprocess.run([sys.executable, '-I', str(ROOT/'relay/native_product.py'), *args],
                          capture_output=True, text=True, timeout=120)


def test_verify_signature_command_runs_bare_and_reports_by_exit_code(tmp_path):
    openssl = find_openssl()
    if not openssl:
        pytest.skip('no OpenSSL 3 with Ed25519 signing')
    pem, public = openssl_key(openssl, tmp_path)
    archive = tmp_path/'bundle.zip'
    archive.write_bytes(b'PK product bundle')
    signature = tmp_path/'bundle.zip.sig'
    signature.write_text(base64.b64encode(sign(openssl, pem, archive, tmp_path)).decode() + '\n')
    done = run_command('verify-signature', str(archive), str(signature), '--key', public)
    assert (done.returncode, done.stderr) == (0, '') and 'signature valid' in done.stdout
    done = run_command('verify-signature', str(archive), str(signature), '--key', '00'*32, '--key', public)
    assert done.returncode == 0
    done = run_command('verify-signature', str(archive), str(signature), '--key', '11'*32)
    assert done.returncode == 1 and 'NOT valid' in done.stdout and done.stdout.count('\n') == 1
    done = run_command('verify-signature', str(archive), str(signature))  # no keys committed yet
    assert done.returncode == 1 and 'NOT valid' in done.stdout
    archive.write_bytes(b'PK another bundle')
    assert run_command('verify-signature', str(archive), str(signature), '--key', public).returncode == 1
    done = run_command('verify-signature', str(tmp_path/'missing.zip'), str(signature), '--key', public)
    assert done.returncode == 1 and 'tare installation' in done.stderr


# The key script, with its subprocess runner replaced.

class FakeTools:
    """gh, git and openssl as the key script calls them; keys are recognizable text, never real."""
    def __init__(self, inside_git=False):
        self.calls, self.inside_git, self.count = [], inside_git, 0

    def __call__(self, argv, input=None):
        self.calls.append((list(argv), input))
        tool = Path(argv[0]).stem
        if tool == 'openssl' and argv[1] == 'version':
            return 0, b'OpenSSL 3.0.13 30 Jan 2024\n', ''
        if tool == 'openssl' and argv[1] == 'genpkey':
            self.count += 1
            return 0, f'-----BEGIN PRIVATE KEY-----\nSECRET{self.count}\n-----END PRIVATE KEY-----\n'.encode(), ''
        if tool == 'openssl' and argv[1] == 'pkey':  # a DER header, then 32 bytes derived from the PEM given
            return 0, b'DER-HEADER-12' + (b'%02d' % int(input.split(b'SECRET')[1][:1])) * 16, ''
        if tool == 'git':
            return (0, b'true\n', '') if self.inside_git else (128, b'', 'not a git repository')
        return 0, b'', ''

    def with_input(self, *words):
        return [(argv, data) for argv, data in self.calls if all(word in argv for word in words)]


CI_PUBLIC, RECOVERY_PUBLIC = (b'01'*16).hex(), (b'02'*16).hex()  # what FakeTools derives for its two keys


def setup_with(tmp_path, tools, **kwargs):
    return keys.setup(tmp_path/'recovery.pem', runner=tools, openssl='openssl', **kwargs)


def test_key_script_stores_the_ci_key_through_stdin_and_the_recovery_key_in_the_file(tmp_path, capsys):
    tools = FakeTools()
    assert keys.main(['--recovery-out', str(tmp_path/'recovery.pem'), '--repo', 'o/r'], runner=tools) == 0
    shown = capsys.readouterr()
    assert (CI_PUBLIC in shown.out) and (RECOVERY_PUBLIC in shown.out)
    assert 'SECRET' not in shown.out + shown.err and 'PRIVATE KEY' not in shown.out + shown.err
    secret, = tools.with_input('gh', 'secret', 'set')
    assert secret[0] == ['gh', 'secret', 'set', 'RELEASE_SIGNING_KEY', '--env', 'release-signing', '--repo', 'o/r']
    assert b'SECRET1' in secret[1]  # the CI key, on stdin
    assert not any(b'SECRET' in ' '.join(argv).encode() for argv, _ in tools.calls)  # never in a command line
    assert [data for argv, data in tools.calls if data and b'SECRET2' in data and argv[0] == 'gh'] == []
    assert b'SECRET2' in (tmp_path/'recovery.pem').read_bytes()
    assert b'SECRET1' not in (tmp_path/'recovery.pem').read_bytes()
    assert [p.name for p in tmp_path.iterdir()] == ['recovery.pem']


def test_key_script_restricts_the_environment_to_protected_branches(tmp_path):
    tools = FakeTools()
    setup_with(tmp_path, tools, repo='o/r', environment='env-x')
    (argv, body), = tools.with_input('gh', 'api')
    assert argv == ['gh', 'api', '-X', 'PUT', 'repos/o/r/environments/env-x', '--input', '-']
    assert json.loads(body) == {'deployment_branch_policy': {'protected_branches': True,
                                                             'custom_branch_policies': False}}
    assert tools.with_input('gh', 'secret')[0][0][5:7] == ['env-x', '--repo']
    assert tools.calls.index(tools.with_input('gh', 'api')[0]) < tools.calls.index(tools.with_input('gh', 'secret')[0])


def test_key_script_never_overwrites_nor_writes_inside_a_git_working_tree(tmp_path, capsys):
    existing = tmp_path/'recovery.pem'
    existing.write_text('keep me')
    tools = FakeTools()
    with pytest.raises(keys.SetupError, match='exists'):
        setup_with(tmp_path, tools)
    assert existing.read_text() == 'keep me' and not tools.with_input('gh', 'secret')
    existing.unlink()
    tools = FakeTools(inside_git=True)
    assert keys.main(['--recovery-out', str(existing)], runner=tools) == 1
    assert 'Git working tree' in capsys.readouterr().err
    assert not existing.exists() and not tools.with_input('gh', 'api') and not tools.with_input('gh', 'secret')


def test_key_script_leaves_no_recovery_file_when_the_secret_cannot_be_set(tmp_path):
    class Failing(FakeTools):
        def __call__(self, argv, input=None):
            return (1, b'', 'HTTP 403') if argv[:3] == ['gh', 'secret', 'set'] else super().__call__(argv, input)
    with pytest.raises(keys.SetupError, match='HTTP 403'):
        setup_with(tmp_path, Failing())
    assert not (tmp_path/'recovery.pem').exists()


def test_key_script_asks_for_openssl_3_and_says_when_it_is_missing(tmp_path):
    class Old(FakeTools):
        def __call__(self, argv, input=None):
            return (0, b'LibreSSL 3.3.6', '') if argv[1:] == ['version'] else super().__call__(argv, input)
    with pytest.raises(keys.SetupError, match='OpenSSL 3'):
        setup_with(tmp_path, Old())
    assert keys.find_openssl(which=lambda name: None, environ={}) is None
    assert keys.find_openssl(which=lambda name: '/usr/bin/openssl', environ={}) == '/usr/bin/openssl'
    program_files = tmp_path/'PF'
    (program_files/'Git/usr/bin').mkdir(parents=True)
    (program_files/'Git/usr/bin/openssl.exe').write_bytes(b'')
    assert keys.find_openssl(which=lambda name: None, environ={'ProgramFiles': str(program_files)}) == \
        str(program_files/'Git/usr/bin/openssl.exe')


def test_key_script_public_keys_are_the_last_32_bytes_of_the_der_and_verify_with_the_real_openssl(tmp_path):
    openssl = find_openssl()
    if not openssl:
        pytest.skip('no OpenSSL 3 with Ed25519 signing')
    pem, public = keys.generate(keys.run, openssl)
    (tmp_path/'key.pem').write_bytes(pem)
    archive = tmp_path/'bundle.zip'
    archive.write_bytes(b'PK')
    text = base64.b64encode(sign(openssl, tmp_path/'key.pem', archive, tmp_path)).decode()
    assert product.verify_signature(archive, text, [public])


# The workflow, read as text: no YAML library in the repository.

WORKFLOW = (ROOT/'.github/workflows/release.yml').read_text(encoding='utf-8').replace('\r\n', '\n')


def job(name):
    """The text of one top-level job (jobs are indented two spaces under `jobs:`)."""
    lines = WORKFLOW.split('\njobs:\n')[1].split('\n')
    start = lines.index(f'  {name}:')
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith('  ') and not lines[i].startswith('   ')
                and lines[i].strip().endswith(':')), len(lines))
    return '\n'.join(lines[start:end])


def test_only_the_sign_job_can_see_the_signing_secret():
    sign_job, publish_job = job('sign'), job('publish')
    assert 'RELEASE_SIGNING_KEY' not in WORKFLOW.replace(sign_job, '')
    assert 'secrets.RELEASE_SIGNING_KEY' in sign_job and 'secrets.' not in publish_job
    assert 'secrets.' not in sign_job.replace('secrets.RELEASE_SIGNING_KEY', '')
    secret_step = sign_job.split('- name: Sign the bundle')[1].split('\n      - name:')[0]
    assert 'RELEASE_SIGNING_KEY' not in sign_job.replace(secret_step, '')  # one step gets it, no job-wide env
    assert 'RELEASE_SIGNING_KEY: ${{ secrets.RELEASE_SIGNING_KEY }}' in secret_step
    assert 'set -euo pipefail' in secret_step and 'umask 077' in secret_step and 'trap' in secret_step
    assert 'echo' not in secret_step and 'shell: bash' in secret_step


def test_the_sign_job_runs_no_project_code_in_the_protected_environment():
    sign_job = job('sign')
    assert 'environment: release-signing' in sign_job
    assert 'needs: publish' in sign_job and "needs.publish.outputs.published == 'true'" in sign_job
    assert 'runs-on: ubuntu-latest' in sign_job and 'timeout-minutes:' in sign_job
    assert 'publish_product' not in sign_job and 'pip ' not in sign_job and 'setup-python' not in sign_job
    assert 'persist-credentials: false' in sign_job and 'ref: main' in sign_job
    assert 'contents: write' in sign_job and 'id-token' not in sign_job
    assert 'environment' not in job('publish')


def test_the_publish_job_makes_a_draft_and_the_release_goes_public_only_after_verification():
    publish_job, sign_job = job('publish'), job('sign')
    assert '--draft' in publish_job and 'id: publish' in publish_job
    assert 'published: ${{ steps.publish.outputs.published }}' in publish_job
    assert 'tag: ${{ steps.publish.outputs.tag }}' in publish_job
    assert 'attest-build-provenance' in publish_job and 'contents: write' in WORKFLOW.split('\njobs:')[0]
    order = [sign_job.index(text) for text in ('gh release download', 'openssl pkeyutl -sign',
                                               'verify-signature dist/bundle.zip dist/bundle.zip.sig',
                                               'gh release upload', '--draft=false')]
    assert order == sorted(order)
    assert sign_job.count('--draft=false') == 1
    assert '--draft=false' not in publish_job
