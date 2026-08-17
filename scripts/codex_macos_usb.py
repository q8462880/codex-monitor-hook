#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""macOS 上访问未注册到 IOHID 的 Codex Micro 屏幕接口。"""

from __future__ import annotations

import ctypes
import sys
from typing import Any, Optional

from codex_macos_iokit import (
    CFUUIDBytes,
    IOKitBackend,
    MacOSUSBError,
    hex_result,
)

KERN_SUCCESS = 0
K_USB_OUT = 0
K_USB_INTERRUPT = 3
USB_INTERFACE_USER_CLIENT_UUID = (
    0x2D, 0x97, 0x86, 0xC6, 0x9E, 0xF3, 0x11, 0xD4,
    0xAD, 0x51, 0x00, 0x0A, 0x27, 0x05, 0x28, 0x61,
)
CF_PLUGIN_INTERFACE_UUID = (
    0xC2, 0x44, 0xE8, 0x58, 0x10, 0x9C, 0x11, 0xD4,
    0x91, 0xD4, 0x00, 0x50, 0xE4, 0xC6, 0x42, 0x6F,
)
USB_INTERFACE_100_UUID = (
    0x73, 0xC9, 0x7A, 0xE8, 0x9E, 0xF3, 0x11, 0xD4,
    0xB1, 0xD0, 0x00, 0x0A, 0x27, 0x05, 0x28, 0x61,
)


class MacOSRawHIDDevice:
    """以 interrupt OUT 方式独占 MI_02，不接触键盘和 RPC interface。"""

    def __init__(
        self, vendor_id: int, product_id: int, interface_number: int,
        report_size: int, backend: Optional[IOKitBackend] = None,
    ) -> None:
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.interface_number = interface_number
        self.report_size = report_size
        self.backend = backend
        self.interface = ctypes.c_void_p()
        self.pipe_ref = 0

    def open(self) -> None:
        if sys.platform != "darwin":
            raise MacOSUSBError("raw IOUSBLib transport is only available on macOS")
        backend = self.backend or IOKitBackend()
        self.backend = backend
        iterator = ctypes.c_uint32()
        matching = backend.iokit.IOServiceMatching(b"IOUSBHostInterface")
        result = backend.iokit.IOServiceGetMatchingServices(
            0, matching, ctypes.byref(iterator)
        )
        if result != KERN_SUCCESS:
            raise MacOSUSBError(f"USB enumeration failed: {hex_result(result)}")
        try:
            self._open_matching_service(iterator.value)
        finally:
            backend.iokit.IOObjectRelease(iterator.value)

    def _open_matching_service(self, iterator: int) -> None:
        assert self.backend is not None
        while True:
            service = self.backend.iokit.IOIteratorNext(iterator)
            if not service:
                break
            try:
                if self._matches(service):
                    self._open_service(service)
                    return
            finally:
                self.backend.iokit.IOObjectRelease(service)
        raise FileNotFoundError(
            f"USB interface {self.interface_number} not found for "
            f"{self.vendor_id:04X}:{self.product_id:04X}"
        )

    def _matches(self, service: int) -> bool:
        assert self.backend is not None
        expected = {
            "idVendor": self.vendor_id,
            "idProduct": self.product_id,
            "bInterfaceNumber": self.interface_number,
        }
        return all(
            self.backend.number_property(service, key) == value
            for key, value in expected.items()
        )

    def _open_service(self, service: int) -> None:
        assert self.backend is not None
        plugin = ctypes.c_void_p()
        score = ctypes.c_int32()
        result = self.backend.iokit.IOCreatePlugInInterfaceForService(
            service,
            self.backend.uuid(USB_INTERFACE_USER_CLIENT_UUID),
            self.backend.uuid(CF_PLUGIN_INTERFACE_UUID),
            ctypes.byref(plugin), ctypes.byref(score),
        )
        if result != KERN_SUCCESS or not plugin.value:
            raise MacOSUSBError(f"USB plugin creation failed: {hex_result(result)}")
        try:
            self._query_interface(plugin)
        finally:
            self.backend.iokit.IODestroyPlugInInterface(plugin)
        open_fn = self.backend.function(
            self.interface, 8, ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p)
        )
        result = open_fn(self.interface)
        if result != KERN_SUCCESS:
            self._release_interface()
            raise MacOSUSBError(f"USB interface open failed: {hex_result(result)}")
        try:
            self.pipe_ref = self._find_output_pipe()
        except Exception:
            self.close()
            raise

    def _query_interface(self, plugin: ctypes.c_void_p) -> None:
        assert self.backend is not None
        query = self.backend.function(
            plugin, 1,
            ctypes.CFUNCTYPE(
                ctypes.c_int32, ctypes.c_void_p, CFUUIDBytes,
                ctypes.POINTER(ctypes.c_void_p),
            ),
        )
        uuid = self.backend.cf.CFUUIDGetUUIDBytes(
            self.backend.uuid(USB_INTERFACE_100_UUID)
        )
        result = query(plugin, uuid, ctypes.byref(self.interface))
        if result != KERN_SUCCESS or not self.interface.value:
            raise MacOSUSBError(f"USB interface query failed: {hex_result(result)}")

    def _find_output_pipe(self) -> int:
        assert self.backend is not None
        get_count = self.backend.function(
            self.interface, 19,
            ctypes.CFUNCTYPE(
                ctypes.c_int32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8)
            ),
        )
        get_properties = self.backend.function(
            self.interface, 26,
            ctypes.CFUNCTYPE(
                ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint8,
                ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_uint8),
                ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_uint16),
                ctypes.POINTER(ctypes.c_uint8),
            ),
        )
        count = ctypes.c_uint8()
        result = get_count(self.interface, ctypes.byref(count))
        if result != KERN_SUCCESS:
            raise MacOSUSBError(f"USB endpoint count failed: {hex_result(result)}")
        for pipe in range(1, count.value + 1):
            direction, number = ctypes.c_uint8(), ctypes.c_uint8()
            transfer, interval = ctypes.c_uint8(), ctypes.c_uint8()
            packet_size = ctypes.c_uint16()
            result = get_properties(
                self.interface, pipe, ctypes.byref(direction), ctypes.byref(number),
                ctypes.byref(transfer), ctypes.byref(packet_size),
                ctypes.byref(interval),
            )
            if (
                result == KERN_SUCCESS
                and direction.value == K_USB_OUT
                and transfer.value == K_USB_INTERRUPT
                and packet_size.value >= self.report_size
            ):
                return pipe
        raise MacOSUSBError(
            f"no {self.report_size}-byte interrupt OUT endpoint on interface "
            f"{self.interface_number}"
        )

    def write(self, data: Any) -> int:
        if not self.interface.value or not self.pipe_ref:
            raise MacOSUSBError("USB interface is not open")
        raw = bytes(data)
        if len(raw) != self.report_size:
            raise ValueError(
                f"expected {self.report_size} bytes, received {len(raw)}"
            )
        assert self.backend is not None
        write_fn = self.backend.function(
            self.interface, 32,
            ctypes.CFUNCTYPE(
                ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint8,
                ctypes.c_void_p, ctypes.c_uint32,
            ),
        )
        buffer = ctypes.create_string_buffer(raw)
        result = write_fn(self.interface, self.pipe_ref, buffer, len(raw))
        if result != KERN_SUCCESS:
            raise MacOSUSBError(f"USB write failed: {hex_result(result)}")
        return len(raw)

    def set_nonblocking(self, _enabled: bool) -> None:
        """与 hidapi 设备保持相同的最小接口；本通道只执行同步写。"""

    def close(self) -> None:
        if not self.interface.value:
            return
        assert self.backend is not None
        close_fn = self.backend.function(
            self.interface, 9, ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p)
        )
        close_fn(self.interface)
        self._release_interface()
        self.pipe_ref = 0

    def _release_interface(self) -> None:
        if not self.interface.value or self.backend is None:
            return
        release = self.backend.function(
            self.interface, 3,
            ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p),
        )
        release(self.interface)
        self.interface = ctypes.c_void_p()
