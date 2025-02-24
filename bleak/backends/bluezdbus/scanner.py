import logging
import sys
from typing import Callable, Coroutine, Dict, List, Optional
from warnings import warn

from dbus_fast import Variant

if sys.version_info[:2] < (3, 8):
    from typing_extensions import Literal, TypedDict
else:
    from typing import Literal, TypedDict

from ...exc import BleakError
from ..device import BLEDevice
from ..scanner import AdvertisementData, AdvertisementDataCallback, BaseBleakScanner
from .advertisement_monitor import OrPatternLike
from .defs import Device1
from .manager import get_global_bluez_manager

logger = logging.getLogger(__name__)


class BlueZDiscoveryFilters(TypedDict, total=False):
    UUIDs: List[str]
    RSSI: int
    Pathloss: int
    Transport: str
    DuplicateData: bool
    Discoverable: bool
    Pattern: str


class BlueZScannerArgs(TypedDict, total=False):
    """
    :class:`BleakScanner` args that are specific to the BlueZ backend.
    """

    filters: BlueZDiscoveryFilters
    """
    Filters to pass to the adapter SetDiscoveryFilter D-Bus method.

    Only used for active scanning.
    """

    or_patterns: List[OrPatternLike]
    """
    Or patterns to pass to the AdvertisementMonitor1 D-Bus interface.

    Only used for passive scanning.
    """


class BleakScannerBlueZDBus(BaseBleakScanner):
    """The native Linux Bleak BLE Scanner.

    For possible values for `filters`, see the parameters to the
    ``SetDiscoveryFilter`` method in the `BlueZ docs
    <https://git.kernel.org/pub/scm/bluetooth/bluez.git/tree/doc/adapter-api.txt?h=5.48&id=0d1e3b9c5754022c779da129025d493a198d49cf>`_

    Args:
        detection_callback:
            Optional function that will be called each time a device is
            discovered or advertising data has changed.
        service_uuids:
            Optional list of service UUIDs to filter on. Only advertisements
            containing this advertising data will be received. Specifying this
            also enables scanning while the screen is off on Android.
        scanning_mode:
            Set to ``"passive"`` to avoid the ``"active"`` scanning mode.
        **bluez:
            Dictionary of arguments specific to the BlueZ backend.
        **adapter (str):
            Bluetooth adapter to use for discovery.
    """

    def __init__(
        self,
        detection_callback: Optional[AdvertisementDataCallback] = None,
        service_uuids: Optional[List[str]] = None,
        scanning_mode: Literal["active", "passive"] = "active",
        *,
        bluez: BlueZScannerArgs = {},
        **kwargs,
    ):
        super(BleakScannerBlueZDBus, self).__init__(detection_callback, service_uuids)

        self._scanning_mode = scanning_mode

        # kwarg "device" is for backwards compatibility
        self._adapter = kwargs.get("adapter", kwargs.get("device", "hci0"))
        self._adapter_path: str = f"/org/bluez/{self._adapter}"

        # map of d-bus object path to d-bus object properties
        self._devices: Dict[str, Device1] = {}

        # callback from manager for stopping scanning if it has been started
        self._stop: Optional[Callable[[], Coroutine]] = None

        # Discovery filters

        self._filters: Dict[str, Variant] = {}

        self._filters["Transport"] = Variant("s", "le")
        self._filters["DuplicateData"] = Variant("b", False)

        if self._service_uuids:
            self._filters["UUIDs"] = Variant("as", self._service_uuids)

        filters = kwargs.get("filters")

        if filters is None:
            filters = bluez.get("filters")
        else:
            warn(
                "the 'filters' kwarg is deprecated, use 'bluez' kwarg instead",
                FutureWarning,
                stacklevel=2,
            )

        if filters is not None:
            self.set_scanning_filter(filters=filters)

        self._or_patterns = bluez.get("or_patterns")

        if self._scanning_mode == "passive" and service_uuids:
            logger.warning(
                "service uuid filtering is not implemented for passive scanning, use bluez or_patterns as a workaround"
            )

        if self._scanning_mode == "passive" and not self._or_patterns:
            raise BleakError("passive scanning mode requires bluez or_patterns")

    async def start(self):
        manager = await get_global_bluez_manager()

        self._devices.clear()

        if self._scanning_mode == "passive":
            self._stop = await manager.passive_scan(
                self._adapter_path,
                self._or_patterns,
                self._handle_advertising_data,
                self._handle_device_removed,
            )
        else:
            self._stop = await manager.active_scan(
                self._adapter_path,
                self._filters,
                self._handle_advertising_data,
                self._handle_device_removed,
            )

    async def stop(self):
        if self._stop:
            # avoid reentrancy
            stop, self._stop = self._stop, None

            await stop()

    def set_scanning_filter(self, **kwargs):
        """Sets OS level scanning filters for the BleakScanner.

        For possible values for `filters`, see the parameters to the
        ``SetDiscoveryFilter`` method in the `BlueZ docs
        <https://git.kernel.org/pub/scm/bluetooth/bluez.git/tree/doc/adapter-api.txt?h=5.48&id=0d1e3b9c5754022c779da129025d493a198d49cf>`_

        See variant types here: <https://python-dbus-next.readthedocs.io/en/latest/type-system/>

        Keyword Args:
            filters (dict): A dict of filters to be applied on discovery.

        """
        for k, v in kwargs.get("filters", {}).items():
            if k == "UUIDs":
                self._filters[k] = Variant("as", v)
            elif k == "RSSI":
                self._filters[k] = Variant("n", v)
            elif k == "Pathloss":
                self._filters[k] = Variant("n", v)
            elif k == "Transport":
                self._filters[k] = Variant("s", v)
            elif k == "DuplicateData":
                self._filters[k] = Variant("b", v)
            elif k == "Discoverable":
                self._filters[k] = Variant("b", v)
            elif k == "Pattern":
                self._filters[k] = Variant("s", v)
            else:
                logger.warning("Filter '%s' is not currently supported." % k)

    @property
    def discovered_devices(self) -> List[BLEDevice]:
        discovered_devices = []

        for path, props in self._devices.items():
            uuids = props.get("UUIDs", [])
            manufacturer_data = props.get("ManufacturerData", {})
            discovered_devices.append(
                BLEDevice(
                    props["Address"],
                    props["Alias"],
                    {"path": path, "props": props},
                    props.get("RSSI", 0),
                    uuids=uuids,
                    manufacturer_data=manufacturer_data,
                )
            )
        return discovered_devices

    # Helper methods

    def _handle_advertising_data(self, path: str, props: Device1) -> None:
        """
        Handles advertising data received from the BlueZ manager instance.

        Args:
            path: The D-Bus object path of the device.
            props: The D-Bus object properties of the device.
        """

        self._devices[path] = props

        if self._callback is None:
            return

        # Get all the information wanted to pack in the advertisement data
        _local_name = props.get("Name")
        _manufacturer_data = {
            k: bytes(v) for k, v in props.get("ManufacturerData", {}).items()
        }
        _service_data = {k: bytes(v) for k, v in props.get("ServiceData", {}).items()}
        _service_uuids = props.get("UUIDs", [])

        # Get tx power data
        tx_power = props.get("TxPower")

        # Pack the advertisement data
        advertisement_data = AdvertisementData(
            local_name=_local_name,
            manufacturer_data=_manufacturer_data,
            service_data=_service_data,
            service_uuids=_service_uuids,
            platform_data=props,
            tx_power=tx_power,
        )

        device = BLEDevice(
            props["Address"],
            props["Alias"],
            {"path": path, "props": props},
            props.get("RSSI", 0),
            uuids=_service_uuids,
            manufacturer_data=_manufacturer_data,
        )

        self._callback(device, advertisement_data)

    def _handle_device_removed(self, device_path: str) -> None:
        """
        Handles a device being removed from BlueZ.
        """
        try:
            del self._devices[device_path]
        except KeyError:
            # The device will not have been added to self._devices if no
            # advertising data was received, so this is expected to happen
            # occasionally.
            pass
