"""
BlueZ D-Bus manager module
--------------------------

This module contains code for the global BlueZ D-Bus object manager that is
used internally by Bleak.
"""

import asyncio
import logging
import os
from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    Iterable,
    List,
    NamedTuple,
    Optional,
    Set,
    cast,
)

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio.message_bus import MessageBus

from ...exc import BleakError
from ..service import BleakGATTServiceCollection
from . import defs
from .advertisement_monitor import AdvertisementMonitor, OrPatternLike
from .characteristic import BleakGATTCharacteristicBlueZDBus
from .defs import Device1, GattService1, GattCharacteristic1, GattDescriptor1
from .descriptor import BleakGATTDescriptorBlueZDBus
from .service import BleakGATTServiceBlueZDBus
from .signals import MatchRules, add_match
from .utils import assert_reply, unpack_variants

logger = logging.getLogger(__name__)

AdvertisementCallback = Callable[[str, Device1], None]
"""
A callback that is called when advertisement data is received.

Args:
    arg0: The D-Bus object path of the device.
    arg1: The D-Bus properties of the device object.
"""


class CallbackAndState(NamedTuple):
    """
    Encapsulates an :data:`AdvertisementCallback` and some state.
    """

    callback: AdvertisementCallback
    """
    The callback.
    """

    adapter_path: str
    """
    The D-Bus object path of the adapter associated with the callback.
    """


DeviceRemovedCallback = Callable[[str], None]
"""
A callback that is called when a device is removed from BlueZ.

Args:
    arg0: The D-Bus object path of the device.
"""


class DeviceRemovedCallbackAndState(NamedTuple):
    """
    Encapsulates an :data:`DeviceRemovedCallback` and some state.
    """

    callback: DeviceRemovedCallback
    """
    The callback.
    """

    adapter_path: str
    """
    The D-Bus object path of the adapter associated with the callback.
    """


DeviceConnectedChangedCallback = Callable[[bool], None]
"""
A callback that is called when a device's "Connected" property changes.

Args:
    arg0: The current value of the "Connected" property.
"""

CharacteristicValueChangedCallback = Callable[[str, bytes], None]
"""
A callback that is called when a characteristics's "Value" property changes.

Args:
    arg0: The D-Bus object path of the characteristic.
    arg1: The current value of the "Value" property.
"""


class DeviceWatcher(NamedTuple):

    device_path: str
    """
    The D-Bus object path of the device.
    """

    on_connected_changed: DeviceConnectedChangedCallback
    """
    A callback that is called when a device's "Connected" property changes.
    """

    on_characteristic_value_changed: CharacteristicValueChangedCallback
    """
    A callback that is called when a characteristics's "Value" property changes.
    """


# set of org.bluez.Device1 property names that come from advertising data
_ADVERTISING_DATA_PROPERTIES = {
    "AdvertisingData",
    "AdvertisingFlags",
    "ManufacturerData",
    "Name",
    "ServiceData",
    "UUIDs",
}


class BlueZManager:
    """
    BlueZ D-Bus object manager.

    Use :func:`bleak.backends.bluezdbus.get_global_bluez_manager` to get the global instance.
    """

    def __init__(self):
        self._bus: Optional[MessageBus] = None
        self._bus_lock = asyncio.Lock()

        # dict of object path: dict of interface name: dict of property name: property value
        self._properties: Dict[str, Dict[str, Dict[str, Any]]] = {}

        # The BlueZ APIs only maps children to parents, so we need to keep maps
        # to quickly find the children of a parent D-Bus object.

        # map of device d-bus object paths to set of service d-bus object paths
        self._service_map: Dict[str, Set[str]] = {}
        # map of service d-bus object paths to set of characteristic d-bus object paths
        self._characteristic_map: Dict[str, Set[str]] = {}
        # map of characteristic d-bus object paths to set of descriptor d-bus object paths
        self._descriptor_map: Dict[str, Set[str]] = {}

        self._advertisement_callbacks: List[CallbackAndState] = []
        self._device_removed_callbacks: List[DeviceRemovedCallbackAndState] = []
        self._device_watchers: Set[DeviceWatcher] = set()
        self._condition_callbacks: Set[Callable] = set()
        self._services_cache: Dict[str, BleakGATTServiceCollection] = {}

    async def async_init(self):
        """
        Connects to the D-Bus message bus and begins monitoring signals.

        It is safe to call this method multiple times. If the bus is already
        connected, no action is performed.
        """
        async with self._bus_lock:
            if self._bus and self._bus.connected:
                return

            self._services_cache = {}

            # We need to create a new MessageBus each time as
            # dbus-next will destory the underlying file descriptors
            # when the previous one is closed in its finalizer.
            bus = MessageBus(bus_type=BusType.SYSTEM)
            await bus.connect()

            try:
                # Add signal listeners

                bus.add_message_handler(self._parse_msg)

                rules = MatchRules(
                    interface=defs.OBJECT_MANAGER_INTERFACE,
                    member="InterfacesAdded",
                    arg0path="/org/bluez/",
                )
                reply = await add_match(bus, rules)
                assert_reply(reply)

                rules = MatchRules(
                    interface=defs.OBJECT_MANAGER_INTERFACE,
                    member="InterfacesRemoved",
                    arg0path="/org/bluez/",
                )
                reply = await add_match(bus, rules)
                assert_reply(reply)

                rules = MatchRules(
                    interface=defs.PROPERTIES_INTERFACE,
                    member="PropertiesChanged",
                    path_namespace="/org/bluez",
                )
                reply = await add_match(bus, rules)
                assert_reply(reply)

                # get existing objects after adding signal handlers to avoid
                # race condition

                reply = await bus.call(
                    Message(
                        destination=defs.BLUEZ_SERVICE,
                        path="/",
                        member="GetManagedObjects",
                        interface=defs.OBJECT_MANAGER_INTERFACE,
                    )
                )
                assert_reply(reply)

                # dictionaries are cleared in case AddInterfaces was received first
                # or there was a bus reset and we are reconnecting
                self._properties.clear()
                self._service_map.clear()
                self._characteristic_map.clear()
                self._descriptor_map.clear()

                for path, interfaces in reply.body[0].items():
                    props = unpack_variants(interfaces)
                    self._properties[path] = props

                    service_props = cast(
                        GattService1, props.get(defs.GATT_SERVICE_INTERFACE)
                    )

                    if service_props:
                        self._service_map.setdefault(
                            service_props["Device"], set()
                        ).add(path)

                    char_props = cast(
                        GattCharacteristic1,
                        props.get(defs.GATT_CHARACTERISTIC_INTERFACE),
                    )

                    if char_props:
                        self._characteristic_map.setdefault(
                            char_props["Service"], set()
                        ).add(path)

                    desc_props = cast(
                        GattDescriptor1, props.get(defs.GATT_DESCRIPTOR_INTERFACE)
                    )

                    if desc_props:
                        self._descriptor_map.setdefault(
                            desc_props["Characteristic"], set()
                        ).add(path)

                logger.debug(f"initial properties: {self._properties}")

            except BaseException:
                # if setup failed, disconnect
                bus.disconnect()
                raise

            # Everything is setup, so save the bus
            self._bus = bus

    async def active_scan(
        self,
        adapter_path: str,
        filters: Dict[str, Variant],
        advertisement_callback: AdvertisementCallback,
        device_removed_callback: DeviceRemovedCallback,
    ) -> Callable[[], Coroutine]:
        """
        Configures the advertisement data filters and starts scanning.

        Args:
            adapter_path: The D-Bus object path of the adapter to use for scanning.
            filters: A dictionary of filters to pass to ``SetDiscoveryFilter``.
            advertisement_callback:
                A callable that will be called when new advertisement data is received.
            device_removed_callback:
                A callable that will be called when a device is removed from BlueZ.

        Returns:
            An async function that is used to stop scanning and remove the filters.
        """
        async with self._bus_lock:
            # If the adapter doesn't exist, then the message calls below would
            # fail with "method not found". This provides a more informative
            # error message.
            if adapter_path not in self._properties:
                raise BleakError(f"adapter '{adapter_path.split('/')[-1]}' not found")

            callback_and_state = CallbackAndState(advertisement_callback, adapter_path)
            self._advertisement_callbacks.append(callback_and_state)

            device_removed_callback_and_state = DeviceRemovedCallbackAndState(
                device_removed_callback, adapter_path
            )
            self._device_removed_callbacks.append(device_removed_callback_and_state)

            try:
                # Apply the filters
                reply = await self._bus.call(
                    Message(
                        destination=defs.BLUEZ_SERVICE,
                        path=adapter_path,
                        interface=defs.ADAPTER_INTERFACE,
                        member="SetDiscoveryFilter",
                        signature="a{sv}",
                        body=[filters],
                    )
                )
                assert_reply(reply)

                # Start scanning
                reply = await self._bus.call(
                    Message(
                        destination=defs.BLUEZ_SERVICE,
                        path=adapter_path,
                        interface=defs.ADAPTER_INTERFACE,
                        member="StartDiscovery",
                    )
                )
                assert_reply(reply)

                async def stop() -> None:
                    async with self._bus_lock:
                        reply = await self._bus.call(
                            Message(
                                destination=defs.BLUEZ_SERVICE,
                                path=adapter_path,
                                interface=defs.ADAPTER_INTERFACE,
                                member="StopDiscovery",
                            )
                        )
                        assert_reply(reply)

                        # remove the filters
                        reply = await self._bus.call(
                            Message(
                                destination=defs.BLUEZ_SERVICE,
                                path=adapter_path,
                                interface=defs.ADAPTER_INTERFACE,
                                member="SetDiscoveryFilter",
                                signature="a{sv}",
                                body=[{}],
                            )
                        )
                        assert_reply(reply)

                        self._advertisement_callbacks.remove(callback_and_state)
                        self._device_removed_callbacks.remove(
                            device_removed_callback_and_state
                        )

                return stop
            except BaseException:
                # if starting scanning failed, don't leak the callbacks
                self._advertisement_callbacks.remove(callback_and_state)
                self._device_removed_callbacks.remove(device_removed_callback_and_state)
                raise

    async def passive_scan(
        self,
        adapter_path: str,
        filters: List[OrPatternLike],
        advertisement_callback: AdvertisementCallback,
        device_removed_callback: DeviceRemovedCallback,
    ) -> Callable[[], Coroutine]:
        """
        Configures the advertisement data filters and starts scanning.

        Args:
            adapter_path: The D-Bus object path of the adapter to use for scanning.
            filters: A list of "or patterns" to pass to ``org.bluez.AdvertisementMonitor1``.
            advertisement_callback:
                A callable that will be called when new advertisement data is received.
            device_removed_callback:
                A callable that will be called when a device is removed from BlueZ.

        Returns:
            An async function that is used to stop scanning and remove the filters.
        """
        async with self._bus_lock:
            # If the adapter doesn't exist, then the message calls below would
            # fail with "method not found". This provides a more informative
            # error message.
            if adapter_path not in self._properties:
                raise BleakError(f"adapter '{adapter_path.split('/')[-1]}' not found")

            callback_and_state = CallbackAndState(advertisement_callback, adapter_path)
            self._advertisement_callbacks.append(callback_and_state)

            device_removed_callback_and_state = DeviceRemovedCallbackAndState(
                device_removed_callback, adapter_path
            )
            self._device_removed_callbacks.append(device_removed_callback_and_state)

            try:
                monitor = AdvertisementMonitor(filters)

                # this should be a unique path to allow multiple python interpreters
                # running bleak and multiple scanners within a single interpreter
                monitor_path = f"/org/bleak/{os.getpid()}/{id(monitor)}"

                reply = await self._bus.call(
                    Message(
                        destination=defs.BLUEZ_SERVICE,
                        path=adapter_path,
                        interface=defs.ADVERTISEMENT_MONITOR_MANAGER_INTERFACE,
                        member="RegisterMonitor",
                        signature="o",
                        body=[monitor_path],
                    )
                )

                if (
                    reply.message_type == MessageType.ERROR
                    and reply.error_name == "org.freedesktop.DBus.Error.UnknownMethod"
                ):
                    raise BleakError(
                        "passive scanning on Linux requires BlueZ >= 5.55 with --experimental enabled and Linux kernel >= 5.10"
                    )

                assert_reply(reply)

                # It is important to export after registering, otherwise BlueZ
                # won't use the monitor
                self._bus.export(monitor_path, monitor)

                async def stop():
                    async with self._bus_lock:
                        self._bus.unexport(monitor_path, monitor)

                        reply = await self._bus.call(
                            Message(
                                destination=defs.BLUEZ_SERVICE,
                                path=adapter_path,
                                interface=defs.ADVERTISEMENT_MONITOR_MANAGER_INTERFACE,
                                member="UnregisterMonitor",
                                signature="o",
                                body=[monitor_path],
                            )
                        )
                        assert_reply(reply)

                        self._advertisement_callbacks.remove(callback_and_state)
                        self._device_removed_callbacks.remove(
                            device_removed_callback_and_state
                        )

                return stop

            except BaseException:
                # if starting scanning failed, don't leak the callbacks
                self._advertisement_callbacks.remove(callback_and_state)
                self._device_removed_callbacks.remove(device_removed_callback_and_state)
                raise

    def add_device_watcher(
        self,
        device_path: str,
        on_connected_changed: DeviceConnectedChangedCallback,
        on_characteristic_value_changed: CharacteristicValueChangedCallback,
    ) -> DeviceWatcher:
        """
        Registers a device watcher to receive callbacks when device state
        changes or events are received.

        Args:
            device_path:
                The D-Bus object path of the device.
            on_connected_changed:
                A callback that is called when the device's "Connected"
                state changes.
            on_characteristic_value_changed:
                A callback that is called whenever a characteristic receives
                a notification/indication.

        Returns:
            A device watcher object that acts a token to unregister the watcher.
        """
        watcher = DeviceWatcher(
            device_path, on_connected_changed, on_characteristic_value_changed
        )

        self._device_watchers.add(watcher)
        return watcher

    def remove_device_watcher(self, watcher: DeviceWatcher) -> None:
        """
        Unregisters a device watcher.

        Args:
            The device watcher token that was returned by
            :meth:`add_device_watcher`.
        """
        self._device_watchers.remove(watcher)

    async def get_services(
        self, device_path: str, use_cached: bool
    ) -> BleakGATTServiceCollection:
        """
        Builds a new :class:`BleakGATTServiceCollection` from the current state.

        Args:
            device_path:
                The D-Bus object path of the Bluetooth device.
            use_cached:
                When ``True`` if there is a cached :class:`BleakGATTServiceCollection`,
                the method will not wait for ``"ServicesResolved"`` to become true
                and instead return the cached service collection immediately.

        Returns:
            A new :class:`BleakGATTServiceCollection`.
        """
        if use_cached:
            services = self._services_cache.get(device_path)
            if services is not None:
                logger.debug("Using cached services for %s", device_path)
                return services

        await self._wait_condition(device_path, "ServicesResolved", True)

        services = BleakGATTServiceCollection()

        for service_path in self._service_map.get(device_path, set()):
            service_props = cast(
                GattService1,
                self._properties[service_path][defs.GATT_SERVICE_INTERFACE],
            )

            service = BleakGATTServiceBlueZDBus(service_props, service_path)

            services.add_service(service)

            for char_path in self._characteristic_map.get(service_path, set()):
                char_props = cast(
                    GattCharacteristic1,
                    self._properties[char_path][defs.GATT_CHARACTERISTIC_INTERFACE],
                )

                char = BleakGATTCharacteristicBlueZDBus(
                    char_props,
                    char_path,
                    service.uuid,
                    service.handle,
                    # "MTU" property was added in BlueZ 5.62, otherwise fall
                    # back to minimum MTU according to Bluetooth spec.
                    char_props.get("MTU", 23) - 3,
                )

                services.add_characteristic(char)

                for desc_path in self._descriptor_map.get(char_path, set()):
                    desc_props = cast(
                        GattDescriptor1,
                        self._properties[desc_path][defs.GATT_DESCRIPTOR_INTERFACE],
                    )

                    desc = BleakGATTDescriptorBlueZDBus(
                        desc_props,
                        desc_path,
                        char.uuid,
                        char.handle,
                    )

                    services.add_descriptor(desc)

        self._services_cache[device_path] = services

        return services

    def get_device_name(self, device_path: str) -> str:
        """
        Gets the value of the "Name" property for a device.

        Args:
            device_path: The D-Bus object path of the device.

        Returns:
            The current property value.
        """
        return self._properties[device_path][defs.DEVICE_INTERFACE]["Name"]

    def is_connected(self, device_path: str) -> bool:
        """
        Gets the value of the "Connected" property for a device.

        Args:
            device_path: The D-Bus object path of the device.

        Returns:
            The current property value.
        """
        try:
            return self._properties[device_path][defs.DEVICE_INTERFACE]["Connected"]
        except KeyError:
            return False

    async def _wait_condition(
        self, device_path: str, property_name: str, property_value: Any
    ) -> None:
        """
        Waits for a condition to become true.

        Args:
            device_path: The D-Bus object path of a Bluetooth device.
            property_name: The name of the property to test.
            property_value: A value to compare the current property value to.
        """
        if (
            self._properties[device_path][defs.DEVICE_INTERFACE][property_name]
            == property_value
        ):
            return

        event = asyncio.Event()

        def callback():
            if (
                self._properties[device_path][defs.DEVICE_INTERFACE][property_name]
                == property_value
            ):
                event.set()

        self._condition_callbacks.add(callback)

        try:
            # can be canceled
            await event.wait()
        finally:
            self._condition_callbacks.remove(callback)

    def _parse_msg(self, message: Message):
        """
        Handles callbacks from dbus_fast.
        """

        if message.message_type != MessageType.SIGNAL:
            return

        logger.debug(
            "received D-Bus signal: %s.%s (%s): %s",
            message.interface,
            message.member,
            message.path,
            message.body,
        )

        # type hints
        obj_path: str
        interfaces_and_props: Dict[str, Dict[str, Variant]]
        interfaces: List[str]
        interface: str
        changed: Dict[str, Variant]
        invalidated: List[str]

        if message.member == "InterfacesAdded":
            obj_path, interfaces_and_props = message.body

            for interface, props in interfaces_and_props.items():
                unpacked_props = unpack_variants(props)
                self._properties.setdefault(obj_path, {})[interface] = unpacked_props

                if interface == defs.GATT_SERVICE_INTERFACE:
                    service_props = cast(GattService1, unpacked_props)
                    self._service_map.setdefault(service_props["Device"], set()).add(
                        obj_path
                    )
                elif interface == defs.GATT_CHARACTERISTIC_INTERFACE:
                    char_props = cast(GattCharacteristic1, unpacked_props)
                    self._characteristic_map.setdefault(
                        char_props["Service"], set()
                    ).add(obj_path)
                elif interface == defs.GATT_DESCRIPTOR_INTERFACE:
                    desc_props = cast(GattDescriptor1, unpacked_props)
                    self._descriptor_map.setdefault(
                        desc_props["Characteristic"], set()
                    ).add(obj_path)

                # If this is a device and it has advertising data properties,
                # then it should mean that this device just started advertising.
                # Previously, we just relied on RSSI updates to determine if
                # a device was actually advertising, but we were missing "slow"
                # devices that only advertise once and then go to sleep for a while.
                if interface == defs.DEVICE_INTERFACE:
                    self._run_advertisement_callbacks(
                        obj_path, cast(Device1, unpacked_props), unpacked_props.keys()
                    )
        elif message.member == "InterfacesRemoved":
            obj_path, interfaces = message.body

            for interface in interfaces:
                try:
                    del self._properties[obj_path][interface]
                except KeyError:
                    pass

                if interface == defs.DEVICE_INTERFACE:
                    self._services_cache.pop(obj_path, None)
                    try:
                        del self._service_map[obj_path]
                    except KeyError:
                        pass

                    for callback, adapter_path in self._device_removed_callbacks:
                        if obj_path.startswith(adapter_path):
                            callback(obj_path)
                elif interface == defs.GATT_SERVICE_INTERFACE:
                    try:
                        del self._characteristic_map[obj_path]
                    except KeyError:
                        pass
                elif interface == defs.GATT_CHARACTERISTIC_INTERFACE:
                    try:
                        del self._descriptor_map[obj_path]
                    except KeyError:
                        pass
        elif message.member == "PropertiesChanged":
            assert message.path is not None

            interface, changed, invalidated = message.body

            try:
                self_interface = self._properties[message.path][interface]
            except KeyError:
                # This can happen during initialization. The "PropertiesChanged"
                # handler is attached before "GetManagedObjects" is called
                # and so self._properties may not yet be populated.
                # This is not a problem. We just discard the property value
                # since "GetManagedObjects" will return a newer value.
                pass
            else:
                # update self._properties first

                self_interface.update(unpack_variants(changed))

                for name in invalidated:
                    del self_interface[name]

                # then call any callbacks so they will be called with the
                # updated state

                if interface == defs.DEVICE_INTERFACE:
                    # handle advertisement watchers

                    self._run_advertisement_callbacks(
                        message.path, cast(Device1, self_interface), changed.keys()
                    )

                    # handle device condition watchers
                    for condition_callback in self._condition_callbacks:
                        condition_callback()

                    # handle device connection change watchers

                    if "Connected" in changed:
                        for (
                            device_path,
                            on_connected_changed,
                            _,
                        ) in self._device_watchers.copy():
                            # callbacks may remove the watcher, hence the copy() above
                            if message.path == device_path:
                                on_connected_changed(self_interface["Connected"])

                elif interface == defs.GATT_CHARACTERISTIC_INTERFACE:
                    # handle characteristic value change watchers

                    if "Value" in changed:
                        for device_path, _, on_value_changed in self._device_watchers:
                            if message.path.startswith(device_path):
                                on_value_changed(message.path, self_interface["Value"])

    def _run_advertisement_callbacks(
        self, device_path: str, device: Device1, changed: Iterable[str]
    ) -> None:
        """
        Runs any registered advertisement callbacks.

        Args:
            device_path: The D-Bus object path of the remote device.
            device: The current D-Bus properties of the device.
            changed: A list of properties that have changed since the last call.
        """
        for (callback, adapter_path) in self._advertisement_callbacks:
            # filter messages from other adapters
            if not device_path.startswith(adapter_path):
                continue

            # TODO: this should be deep copy, not shallow
            callback(device_path, device.copy())


async def get_global_bluez_manager() -> BlueZManager:
    """
    Gets the initialized global BlueZ manager instance.
    """

    if not hasattr(get_global_bluez_manager, "instance"):
        setattr(get_global_bluez_manager, "instance", BlueZManager())

    instance: BlueZManager = getattr(get_global_bluez_manager, "instance")

    await instance.async_init()

    return instance
