from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys

from .records import DataError


CFBD_KEYCHAIN_SERVICE = "draftscope-cfbd"
CFBD_KEYCHAIN_ACCOUNT = "cfbd-api"
SECURITY_FRAMEWORK = Path("/System/Library/Frameworks/Security.framework/Security")
COREFOUNDATION_FRAMEWORK = Path(
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)
ERR_SEC_SUCCESS = 0
ERR_SEC_ITEM_NOT_FOUND = -25300


def keychain_available() -> bool:
    # Framework executables can live only in macOS's dyld shared cache and
    # therefore need not pass Path.exists(), even though ctypes can load them.
    return sys.platform == "darwin"


def load_cfbd_api_key_from_keychain() -> str | None:
    """Read DraftScope's scoped Keychain item without writing or displaying it."""

    if not keychain_available():
        return None
    try:
        return _load_with_security_framework()
    except DataError:
        return None


def resolve_cfbd_api_key(explicit: str | None = None) -> str | None:
    """Resolve an explicit key, then the environment, then macOS Keychain."""

    if explicit is not None:
        value = str(explicit).strip()
        return value or None
    environment = str(os.environ.get("CFBD_API_KEY") or "").strip()
    if environment:
        return environment
    return load_cfbd_api_key_from_keychain()


def store_cfbd_api_key_in_keychain(api_key: str) -> None:
    """Store a key through Security.framework, never a subprocess argument or second prompt."""

    if not keychain_available():
        raise DataError("`configure-key` requires macOS Keychain Services")
    secret = bytearray(api_key.encode("utf-8"))
    if not secret:
        raise DataError("Cannot store an empty CFBD credential")
    try:
        _store_with_security_framework(secret)
    finally:
        for index in range(len(secret)):
            secret[index] = 0


def _store_with_security_framework(secret: bytearray) -> None:
    """Add or update the scoped generic-password item via native Keychain Services."""

    security, core_foundation = _load_keychain_frameworks()

    void_pointer = ctypes.c_void_p
    uint32 = ctypes.c_uint32
    pointer_to_void = ctypes.POINTER(void_pointer)
    security.SecKeychainFindGenericPassword.argtypes = [
        void_pointer,
        uint32,
        ctypes.c_char_p,
        uint32,
        ctypes.c_char_p,
        ctypes.POINTER(uint32),
        pointer_to_void,
        pointer_to_void,
    ]
    security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    security.SecKeychainItemModifyAttributesAndData.argtypes = [
        void_pointer,
        void_pointer,
        uint32,
        void_pointer,
    ]
    security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
    security.SecKeychainAddGenericPassword.argtypes = [
        void_pointer,
        uint32,
        ctypes.c_char_p,
        uint32,
        ctypes.c_char_p,
        uint32,
        void_pointer,
        pointer_to_void,
    ]
    security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
    core_foundation.CFRelease.argtypes = [void_pointer]
    core_foundation.CFRelease.restype = None

    service = CFBD_KEYCHAIN_SERVICE.encode("utf-8")
    account = CFBD_KEYCHAIN_ACCOUNT.encode("utf-8")
    secret_buffer = (ctypes.c_char * len(secret)).from_buffer(secret)
    secret_pointer = ctypes.cast(secret_buffer, void_pointer)
    item = void_pointer()
    status = security.SecKeychainFindGenericPassword(
        None,
        len(service),
        service,
        len(account),
        account,
        None,
        None,
        ctypes.byref(item),
    )
    if status == ERR_SEC_SUCCESS:
        try:
            status = security.SecKeychainItemModifyAttributesAndData(
                item,
                None,
                len(secret),
                secret_pointer,
            )
        finally:
            if item.value:
                core_foundation.CFRelease(item)
    elif status == ERR_SEC_ITEM_NOT_FOUND:
        status = security.SecKeychainAddGenericPassword(
            None,
            len(service),
            service,
            len(account),
            account,
            len(secret),
            secret_pointer,
            None,
        )
    else:
        raise DataError(f"macOS Keychain lookup failed with status {status}; no key was saved")
    if status != ERR_SEC_SUCCESS:
        raise DataError(f"macOS Keychain write failed with status {status}; no key was saved")


def _load_with_security_framework() -> str | None:
    """Copy the scoped password through Keychain Services and free Apple's buffer."""

    security, _core_foundation = _load_keychain_frameworks()
    void_pointer = ctypes.c_void_p
    uint32 = ctypes.c_uint32
    pointer_to_void = ctypes.POINTER(void_pointer)
    security.SecKeychainFindGenericPassword.argtypes = [
        void_pointer,
        uint32,
        ctypes.c_char_p,
        uint32,
        ctypes.c_char_p,
        ctypes.POINTER(uint32),
        pointer_to_void,
        pointer_to_void,
    ]
    security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    security.SecKeychainItemFreeContent.argtypes = [void_pointer, void_pointer]
    security.SecKeychainItemFreeContent.restype = ctypes.c_int32

    service = CFBD_KEYCHAIN_SERVICE.encode("utf-8")
    account = CFBD_KEYCHAIN_ACCOUNT.encode("utf-8")
    password_length = uint32()
    password_data = void_pointer()
    status = security.SecKeychainFindGenericPassword(
        None,
        len(service),
        service,
        len(account),
        account,
        ctypes.byref(password_length),
        ctypes.byref(password_data),
        None,
    )
    if status == ERR_SEC_ITEM_NOT_FOUND:
        return None
    if status != ERR_SEC_SUCCESS:
        raise DataError(f"macOS Keychain read failed with status {status}")
    copied = bytearray()
    try:
        if not password_data.value or password_length.value == 0:
            return None
        copied.extend(ctypes.string_at(password_data, password_length.value))
        try:
            value = copied.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise DataError("Stored CFBD Keychain credential is not valid UTF-8") from exc
        return value or None
    finally:
        for index in range(len(copied)):
            copied[index] = 0
        if password_data.value:
            security.SecKeychainItemFreeContent(None, password_data)


def _load_keychain_frameworks() -> tuple[ctypes.CDLL, ctypes.CDLL]:
    try:
        return (
            ctypes.CDLL(str(SECURITY_FRAMEWORK)),
            ctypes.CDLL(str(COREFOUNDATION_FRAMEWORK)),
        )
    except OSError as exc:
        raise DataError("Could not load macOS Keychain Services") from exc
