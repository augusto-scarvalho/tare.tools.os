"""Set up the keys that sign product releases (ADR-075). The maintainer runs this once, by hand.

  python scripts/ops/release_signing_key.py --recovery-out PATH [--repo OWNER/REPO] [--environment release-signing]
                                            [--replace]

It CHANGES THE REPOSITORY'S SETTINGS: it creates or updates the GitHub environment (only protected branches may use
it) and sets its secret RELEASE_SIGNING_KEY, the CI signing key, which replaces any earlier one. Needs `gh` (logged in,
with admin rights on the repository) and OpenSSL 3 (on Windows: on PATH, or the one in Git for Windows).

Two Ed25519 keys are generated in memory. The CI key goes to the secret through gh's stdin and is never written to disk
or printed. The recovery key's PEM is written to --recovery-out (never over an existing file, never inside a Git
working tree) and is the only copy: keep it offline. The script prints only the two PUBLIC keys, in hex: add both to
RELEASE_KEYS in relay/native_product.py.

Once the secret exists the script refuses to run without --replace, because replacing it DESTROYS the current CI key
(it exists nowhere else) and installed copies accept only the keys they already list. With --replace it sets a new CI
key and issues a new recovery key; the next release, which adds the new public keys to RELEASE_KEYS next to the old
ones, is then signed by the new key and installed copies refuse it. Sign that one release by hand with the OLD
recovery key (the commands below) so they install it; later releases verify with the new CI key. Have the old
recovery key at hand before passing --replace, and drop the old public keys only in a later release.

If the CI key is lost or exposed, sign a release by hand with the recovery key (the commands the `sign` job runs),
from a folder with the release's bundle.zip, after deleting the bad bundle.zip.sig if there is one:

  gh release download TAG --repo OWNER/REPO --pattern bundle.zip
  printf 'tare.tools product bundle\\n%s' "$(sha256sum bundle.zip | cut -d' ' -f1)" > message
  openssl pkeyutl -sign -inkey RECOVERY.pem -rawin -in message -out sig.bin
  base64 -w0 sig.bin > bundle.zip.sig
  python relay/native_product.py verify-signature bundle.zip bundle.zip.sig
  gh release upload TAG bundle.zip.sig --repo OWNER/REPO
  gh release edit TAG --draft=false --repo OWNER/REPO
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

REPO = 'augusto-scarvalho/tare.tools.os'
SECRET = 'RELEASE_SIGNING_KEY'


class SetupError(Exception):
    pass


def run(argv, input=None):
    """Run a command; (return code, stdout bytes, stderr text). Tests replace this."""
    try:
        done = subprocess.run(argv, input=input, capture_output=True)
    except FileNotFoundError:
        raise SetupError(f'{argv[0]} not found; install it and put it on PATH') from None
    return done.returncode, done.stdout, done.stderr.decode('utf-8', 'replace').strip()


def find_openssl(which=shutil.which, environ=os.environ):
    found = which('openssl')
    if found:
        return found
    for variable in ('ProgramFiles', 'ProgramW6432'):
        if environ.get(variable):
            candidate = Path(environ[variable])/'Git'/'usr'/'bin'/'openssl.exe'
            if candidate.is_file():
                return str(candidate)
    return None


def checked(runner, argv, input=None):
    code, out, err = runner(argv, input)
    if code:
        # Only stderr of the failed command: it never carries a key, which travels on stdin and stdout.
        raise SetupError(f"{' '.join(argv[:3])} failed: {err or code}")
    return out


def generate(runner, openssl):
    """A new Ed25519 key as PEM bytes, and its raw public key as hex."""
    pem = checked(runner, [openssl, 'genpkey', '-algorithm', 'ed25519'])
    der = checked(runner, [openssl, 'pkey', '-pubout', '-outform', 'DER'], pem)
    if len(der) < 32:
        raise SetupError('openssl did not return an Ed25519 public key')
    return pem, der[-32:].hex()  # the DER SubjectPublicKeyInfo ends with the 32 raw bytes


def check_recovery_path(runner, path):
    if path.exists():
        raise SetupError(f'{path} exists; refusing to overwrite it')
    parent = path.resolve().parent
    if not parent.is_dir():
        raise SetupError(f'{parent} is not a folder')
    code, out, _ = runner(['git', '-C', str(parent), 'rev-parse', '--is-inside-work-tree'], None)
    if code == 0 and out.strip() == b'true':
        raise SetupError(f'{path} is inside a Git working tree; the recovery key must not be committed by accident')


def write_private(path, data, runner):
    """Create the file (never over another one) readable by its owner only."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as target:
        target.write(data)
    if os.name == 'nt' and os.environ.get('USERNAME'):  # the mode bits mean little on Windows: drop inherited access
        runner(['icacls', str(path), '/inheritance:r', '/grant:r', os.environ['USERNAME'] + ':F'], None)


def setup(recovery_out, repo=REPO, environment='release-signing', runner=None, openssl=None, replace=False):
    runner = runner or run
    recovery_out = Path(recovery_out)
    openssl = openssl or find_openssl()
    if not openssl or not checked(runner, [openssl, 'version']).startswith(b'OpenSSL 3'):
        raise SetupError('OpenSSL 3 not found (on Windows: put it on PATH or install Git for Windows)')
    checked(runner, ['gh', 'auth', 'status'])  # a missing gh fails in run(); this catches "not logged in"
    check_recovery_path(runner, recovery_out)
    checked(runner, ['gh', 'api', '-X', 'PUT', f'repos/{repo}/environments/{environment}', '--input', '-'],
            json.dumps({'deployment_branch_policy': {'protected_branches': True,
                                                     'custom_branch_policies': False}}).encode())
    listed = checked(runner, ['gh', 'secret', 'list', '--env', environment, '--repo', repo, '--json', 'name'])
    if not replace and any(row.get('name') == SECRET for row in json.loads(listed or b'[]')):
        raise SetupError(
            f'{SECRET} is already set in {environment}. Replacing it destroys the current CI key, and installed '
            'copies refuse releases signed by a new one until they install a release that lists its public key; '
            'that release must be signed by hand with the current recovery key (see --help). '
            'Pass --replace only with that recovery key at hand.')
    ci_pem, ci_public = generate(runner, openssl)
    recovery_pem, recovery_public = generate(runner, openssl)
    write_private(recovery_out, recovery_pem, runner)
    try:
        checked(runner, ['gh', 'secret', 'set', SECRET, '--env', environment, '--repo', repo], ci_pem)
    except SetupError:
        recovery_out.unlink(missing_ok=True)  # nothing relies on the recovery key yet
        raise
    return ci_public, recovery_public


def main(argv=None, runner=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--recovery-out', type=Path, required=True, help='file for the recovery key (PEM)')
    parser.add_argument('--repo', default=REPO)
    parser.add_argument('--environment', default='release-signing')
    parser.add_argument('--replace', action='store_true', help='replace a CI key that is already set; the next '
                        'release must then be signed by hand with the current recovery key')
    args = parser.parse_args(argv)
    try:
        ci_public, recovery_public = setup(args.recovery_out, args.repo, args.environment, runner,
                                           replace=args.replace)
    except (SetupError, OSError) as error:
        print('release_signing_key: ' + str(error), file=sys.stderr)
        return 1
    print(f'CI public key:       {ci_public}')
    print(f'recovery public key: {recovery_public}')
    print('Next: add both to RELEASE_KEYS in relay/native_product.py.')
    print(f'Keep {args.recovery_out} offline; it is the only copy of the recovery key.')
    print('Do not run this again to "refresh" the keys: it destroys this CI key (see --help, --replace).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
