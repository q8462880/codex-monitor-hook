#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex Micro 原始 USB 传输所需的最小 IOUSBLib ctypes 封装。"""

from __future__ import annotations

import ctypes
from typing import Any, Optional


K_CF_STRING_ENCODING_UTF8 = 0x08000100
K_CF_NUMBER_INT_TYPE = 9


class CFUUIDBytes(ctypes.Structure):
    _fields_ = [(f"byte{i}", ctypes.c_uint8) for i in range(16)]


class MacOSUSBError(OSError):
    """原始 USB interface 打开或传输失败。"""


def hex_result(result: int) -> str:
    return f"0x{result & 0xFFFFFFFF:08X}"


class IOKitBackend:
    """只封装本项目需要的 IOUSBLib 同步调用。"""

    def __init__(self) -> None:
        self.iokit = ctypes.CDLL(
            "/System/Library/Frameworks/IOKit.framework/IOKit"
        )
        self.cf = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        self._configure_iokit()
        self._configure_core_foundation()

    def _configure_iokit(self) -> None:
        self.iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
        self.iokit.IOServiceMatching.restype = ctypes.c_void_p
        self.iokit.IOServiceGetMatchingServices.argtypes = [
            ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)
        ]
        self.iokit.IOServiceGetMatchingServices.restype = ctypes.c_int32
        self.iokit.IOIteratorNext.argtypes = [ctypes.c_uint32]
        self.iokit.IOIteratorNext.restype = ctypes.c_uint32
        self.iokit.IOObjectRelease.argtypes = [ctypes.c_uint32]
        self.iokit.IOObjectRelease.restype = ctypes.c_int32
        self.iokit.IORegistryEntryCreateCFProperty.argtypes = [
            ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32
        ]
        self.iokit.IORegistryEntryCreateCFProperty.restype = ctypes.c_void_p
        self.iokit.IOCreatePlugInInterfaceForService.argtypes = [
            ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_int32),
        ]
        self.iokit.IOCreatePlugInInterfaceForService.restype = ctypes.c_int32
        self.iokit.IODestroyPlugInInterface.argtypes = [ctypes.c_void_p]
        self.iokit.IODestroyPlugInInterface.restype = ctypes.c_int32

    def _configure_core_foundation(self) -> None:
        self.cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32
        ]
        self.cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        self.cf.CFNumberGetValue.argtypes = [
            ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p
        ]
        self.cf.CFNumberGetValue.restype = ctypes.c_bool
        self.cf.CFRelease.argtypes = [ctypes.c_void_p]
        self.cf.CFUUIDGetConstantUUIDWithBytes.argtypes = [
            ctypes.c_void_p, *([ctypes.c_uint8] * 16)
        ]
        self.cf.CFUUIDGetConstantUUIDWithBytes.restype = ctypes.c_void_p
        self.cf.CFUUIDGetUUIDBytes.argtypes = [ctypes.c_void_p]
        self.cf.CFUUIDGetUUIDBytes.restype = CFUUIDBytes

    def uuid(self, values: tuple[int, ...]) -> int:
        return int(self.cf.CFUUIDGetConstantUUIDWithBytes(None, *values))

    def number_property(self, service: int, name: str) -> Optional[int]:
        key = self.cf.CFStringCreateWithCString(
            None, name.encode("ascii"), K_CF_STRING_ENCODING_UTF8
        )
        if not key:
            return None
        try:
            prop = self.iokit.IORegistryEntryCreateCFProperty(
                service, key, None, 0
            )
        finally:
            self.cf.CFRelease(key)
        if not prop:
            return None
        try:
            value = ctypes.c_int()
            if not self.cf.CFNumberGetValue(
                prop, K_CF_NUMBER_INT_TYPE, ctypes.byref(value)
            ):
                return None
            return value.value
        finally:
            self.cf.CFRelease(prop)

    @staticmethod
    def function(interface: ctypes.c_void_p, index: int, prototype: Any) -> Any:
        """IOUSBLib 使用指向虚表的双指针；index 必须对应系统头文件。"""

        vtable = ctypes.c_void_p.from_address(interface.value).value
        if not vtable:
            raise MacOSUSBError("IOUSBLib virtual table unavailable")
        address = ctypes.c_void_p.from_address(
            vtable + index * ctypes.sizeof(ctypes.c_void_p)
        ).value
        if not address:
            raise MacOSUSBError(f"IOUSBLib function slot {index} unavailable")
        return prototype(address)
