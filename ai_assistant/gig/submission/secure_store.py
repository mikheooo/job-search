"""Windows DPAPI user-bound storage for P8 API observations/proposal artifacts."""
import base64
import ctypes
import json
import os
from pathlib import Path
from ctypes import wintypes
from datetime import datetime, timezone


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(raw, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('DPAPI requires Windows; use approved encrypted storage on this host')
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source, target = Blob(len(raw), buffer), Blob()
    library = ctypes.WinDLL('crypt32', use_last_error=True)
    method = library.CryptUnprotectData if decrypt else library.CryptProtectData
    method.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                      ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    method.restype = wintypes.BOOL
    # UI_FORBIDDEN; no machine-wide flag: only the same signed-in OS user can decrypt.
    if not method(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise RuntimeError('Encrypted artifact operation failed')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(target.data)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps({'format': 'DPAPI_CURRENT_USER_V1',
        'ciphertext': base64.b64encode(_crypt(json.dumps(value, ensure_ascii=False).encode())).decode()}), encoding='utf8')
    os.replace(temp, path)


def save_bytes(path, value):
    """Persist opaque bytes under the same user-bound DPAPI envelope."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps({'format': 'DPAPI_CURRENT_USER_V1',
        'ciphertext': base64.b64encode(_crypt(bytes(value))).decode()}), encoding='utf8')
    os.replace(temp, path)


def load_bytes(path):
    """Decrypt opaque bytes; plaintext or unknown formats fail closed."""
    wrapper = json.loads(Path(path).read_text(encoding='utf8'))
    if wrapper.get('format') != 'DPAPI_CURRENT_USER_V1':
        raise ValueError('Expected encrypted P8 artifact')
    return _crypt(base64.b64decode(wrapper['ciphertext']), decrypt=True)


def load(path):
    path = Path(path)
    wrapper = json.loads(path.read_text(encoding='utf8'))
    if wrapper.get('format') != 'DPAPI_CURRENT_USER_V1':
        raise ValueError('Expected encrypted P8 artifact')
    payload = json.loads(_crypt(base64.b64decode(wrapper['ciphertext']), decrypt=True))
    if isinstance(payload, dict) and 'retention_expires_at' in payload:
        try:
            expiry = datetime.fromisoformat(payload['retention_expires_at'].replace('Z', '+00:00'))
            if expiry.tzinfo is None or expiry.utcoffset() is None:
                raise ValueError('timezone required')
        except (TypeError, ValueError):
            raise ValueError('Encrypted artifact retention expiry is invalid') from None
        if expiry <= datetime.now(timezone.utc):
            path.unlink(missing_ok=True)
            raise ValueError('Encrypted artifact retention window expired; artifact was deleted')
    return payload


def save_retained(path, value, *, expires_at):
    """Save encrypted data with an enforced expiry checked by the matching loader."""
    try:
        expiry = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
        if expiry.tzinfo is None or expiry.utcoffset() is None or expiry <= datetime.now(timezone.utc):
            raise ValueError('future timezone-aware expiry required')
    except (AttributeError, TypeError, ValueError):
        raise ValueError('A future timezone-aware retention expiry is required') from None
    save(path, {'retention_expires_at': expires_at, 'value': value})


def load_retained(path, *, now=None):
    """Load an unexpired retained artifact; delete it when its window has elapsed."""
    path = Path(path)
    payload = load(path)
    try:
        expiry = datetime.fromisoformat(payload['retention_expires_at'].replace('Z', '+00:00'))
        if expiry.tzinfo is None or expiry.utcoffset() is None:
            raise ValueError('timezone required')
    except (KeyError, TypeError, ValueError):
        raise ValueError('Encrypted artifact retention expiry is missing or invalid') from None
    if expiry <= (now or datetime.now(timezone.utc)):
        path.unlink(missing_ok=True)
        raise ValueError('Encrypted artifact retention window expired; artifact was deleted')
    return payload['value']


def retained_expiry(path):
    """Return the valid expiry of a retained artifact, or fail closed."""
    payload = load(path)
    if not isinstance(payload, dict) or not payload.get('retention_expires_at'):
        raise ValueError('Encrypted artifact has no retention expiry')
    return payload['retention_expires_at']


def purge_expired_artifacts(directory):
    """Sweep retained DPAPI artifacts in a managed output directory."""
    directory = Path(directory)
    if not directory.exists():
        return 0
    removed = 0
    for path in directory.glob('*.dpapi.json'):
        try:
            load(path)
        except Exception as exc:  # unreadable/cross-user items are preserved
            if isinstance(exc, ValueError) and 'retention window expired' in str(exc):
                removed += 1
            # A non-expiring legacy artifact or a corrupt/unreadable wrapper is
            # preserved; callers never silently delete user data.
    return removed
