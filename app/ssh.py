import base64
import hashlib
import subprocess

from cryptography.hazmat.primitives.serialization import load_ssh_public_key


def public_metadata(public):
    parts = public.strip().split()
    if len(parts) != 2 or any('\n' in value or '\r' in value for value in parts):
        raise ValueError('Clé publique d’hôte invalide.')
    load_ssh_public_key(public.encode())
    digest = hashlib.sha256(base64.b64decode(parts[1], validate=True)).digest()
    return 'SHA256:' + base64.b64encode(digest).decode().rstrip('=')


def scan_host(server):
    command = ['ssh-keyscan', '-T', '5', '-p', str(server['port']), '-t', 'ed25519,ecdsa,rsa', server['host']]
    try:
        result = subprocess.run(command, capture_output=True, timeout=20, check=False)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        raise ValueError('Impossible de lire les clés SSH du serveur (délai ou outil indisponible).') from None
    keys = []
    for line in result.stdout.decode('utf-8', errors='replace').splitlines():
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) != 3:
            continue
        public = ' '.join(parts[1:])
        keys.append({'public_key': public, 'fingerprint': public_metadata(public)})
    if not keys:
        raise ValueError('Aucune clé SSH reçue : vérifier l’adresse, le port et le réseau.')
    return keys


def ssh_options(server, identity, known_hosts):
    return ['-F', '/dev/null', '-p', str(server['port']), '-i', str(identity),
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={known_hosts}',
            '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'ConnectTimeout=15',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3']


def known_host_line(server, public_key):
    # OpenSSH uses an unadorned host at the default port. Bracket notation
    # identifies a host AND a nonstandard port and does not match a port-22 lookup.
    host = server['host'] if server['port'] == 22 else f"[{server['host']}]:{server['port']}"
    return f'{host} {public_key}\n'
