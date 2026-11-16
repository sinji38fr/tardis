import base64
import hashlib
import os
import json

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, ec, rsa
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes

PREFIX = b'TARDIS1:'


def oaep():
    return padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=b'TARDIS')


class Vault:
    def __init__(self, directory, has_keys=False, read_only=False):
        if not read_only:
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        path = directory / 'master.key'
        if not path.exists():
            if has_keys or read_only:
                raise RuntimeError('Clé maître absente : restaurer le volume de secrets avant de démarrer.')
            try:
                with open(path, 'xb') as file:
                    os.chmod(path, 0o600)
                    file.write(Fernet.generate_key())
            except FileExistsError:
                pass
        self.cipher = Fernet(path.read_bytes().strip())
        private_path = directory / 'envelope.pem'
        if read_only and has_keys and not private_path.exists():
            raise RuntimeError('Clé privée de coffre absente.')
        if not private_path.exists() and not read_only:
            key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            with open(private_path, 'xb') as handle:
                os.chmod(private_path, 0o600)
                handle.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        self.private_key = (serialization.load_pem_private_key(private_path.read_bytes(), password=None)
                            if private_path.exists() else None)

    def decrypt(self, ciphertext):
        if not ciphertext.startswith(PREFIX):
            return self.cipher.decrypt(ciphertext)
        if self.private_key is None:
            raise RuntimeError('Clé privée de coffre absente.')
        envelope = json.loads(ciphertext[len(PREFIX):])
        key = self.private_key.decrypt(base64.b64decode(envelope['key']), oaep())
        return Fernet(key).decrypt(envelope['data'].encode())

    def import_key(self, text, passphrase):
        data = text.strip().encode()
        password = passphrase.encode() if passphrase else None
        loader = (serialization.load_ssh_private_key if b'BEGIN OPENSSH PRIVATE KEY' in data
                  else serialization.load_pem_private_key)
        try:
            key = loader(data, password=password)
            if not isinstance(key, (ed25519.Ed25519PrivateKey, ec.EllipticCurvePrivateKey, rsa.RSAPrivateKey)):
                raise ValueError('unsupported')
            public = key.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                                   serialization.PublicFormat.OpenSSH).decode()
            # The passphrase is only needed at import: normalize, then encrypt with the vault key.
            normalized = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                           serialization.NoEncryption())
        except (ValueError, TypeError, UnsupportedAlgorithm):
            raise ValueError('Clé privée invalide, format non pris en charge ou phrase secrète incorrecte.') from None
        digest = hashlib.sha256(base64.b64decode(public.split()[1])).digest()
        return {'fingerprint': 'SHA256:' + base64.b64encode(digest).decode().rstrip('='),
                'key_type': public.split()[0], 'public_key': public,
                'ciphertext': self.cipher.encrypt(normalized)}


class PublicVault:
    """Import-only Web capability: no master key and no decryption method."""
    def __init__(self, directory):
        self.public_key = serialization.load_pem_public_key((directory / 'envelope.pub').read_bytes())

    def encrypt(self, plaintext):
        key = Fernet.generate_key()
        envelope = {'key': base64.b64encode(self.public_key.encrypt(key, oaep())).decode(),
                    'data': Fernet(key).encrypt(plaintext).decode()}
        return PREFIX + json.dumps(envelope).encode()

    def import_key(self, text, passphrase):
        # Reuse the validation/normalization path without storing a private master key.
        validator = object.__new__(Vault)
        temporary = Fernet(Fernet.generate_key())
        validator.cipher = temporary
        record = validator.import_key(text, passphrase)
        record['ciphertext'] = self.encrypt(temporary.decrypt(record['ciphertext']))
        return record
