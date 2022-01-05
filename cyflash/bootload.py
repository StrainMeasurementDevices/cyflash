"""
    The main bootloader class file

    This module is what contains the userspace bootloader host class
"""

import logging
import typing
from enum import Enum
from dataclasses import dataclass
from collections.abc import Callable

from cyflash import cyacd
from cyflash import protocol


class BootloaderSiliconMismatch(Exception):
    """
    Exception when the device silicon and firmware silicon don't match.

    The variable `what_is_mismatched` of this exception indicates what was mismatched: `rev` for the silicon revision
                or `id` for the silicon ID.
    """
    def __init__(self, what_is_mismatched):
        self.what_is_mismatched = what_is_mismatched
        super().__init__()


class BootloaderHostError(Exception):
    def __init__(self, msg):
        self.msg = msg
        super().__init__(msg)


class BootloaderHost(object):
    @dataclass
    class MetadataAppVersionError:
        type = 'app_version'
        device_version: int
        firmware_version: int

    @dataclass
    class MetadataIDError:
        type = 'id'
        device_id: int
        firmware_id: int

    def __init__(self, transport: typing.Union[protocol.SerialTransport, protocol.CANbusTransport], data: cyacd.BootloaderData,
                 chunck_size: int = 25, key: list = None, is_dual_app: bool = False, is_psoc5: bool = False):
        """
        Args:
            transport: The transport to send the data over. Right now only SerialTransport and CANbusTransport are
                       supported
            data: The firmware to upload as a :class:`BootloaderData` class.
            key: The bootloader's secret key if applicable. Defaults to None
            chunck_size: The size of a each transfer chuck. Defaults to 25
            is_dual_app: Whether the bootloader is a dual application. Defaults to False
        """
        self._log = logging.getLogger('Bootloader Host')
        self.transport = transport
        self.key = key
        self.data = data
        self.is_psoc5 = is_psoc5
        self.session = protocol.BootloaderSession(self.transport, self.data.checksum_type)
        self.chunk_size = chunck_size
        self.dual_app = is_dual_app
        self.row_ranges = {}

    def verify_checksum(self):
        """
        Checks the checksum in the bootloader

        Raises:
            BootloaderHostError: When the application checksum doesn't match
        """
        if not self.session.verify_checksum():
            raise BootloaderHostError("Checksum Error")

    def set_application_active(self, application_id):
        """
        Set the active application. Only applicable in a dual application bootloader

        Args:
             application_id (int): The application ID to switch to

        Raises:
             UserWarning: If this function is called with a single application bootloader
        """
        if not self.dual_app:
            raise UserWarning("Command only valid for dual application")
        self._log.info("Setting application %d as active.\n" % application_id)
        self.session.set_application_active(application_id)

    def get_application_inactive(self):
        """
        Gets the inactive application to flash the firmware to

        Raises:
            UserWarning: If this function is called with a single application bootloader
            BootloaderHostError: If this function is unable to find an inactive flash
        """
        if not self.dual_app:
            raise UserWarning("Command only valid for dual application")
        to_flash = None
        for app in [0, 1]:
            app_valid, app_active = self.session.application_status(app)
            self._log.debug("App %d: valid: %s, active: %s\n" % (app, app_valid, app_active))
            if app_active == 0:
                to_flash = app

        if to_flash is None:
            raise BootloaderHostError("Failed to find inactive app to flash. Aborting.")
        self._log.debug("Will flash app %d.\n" % to_flash)
        return to_flash

    def verify_row_ranges(self):
        """
        Verifies that the chip's rows will actually fit the firmware

        Raises:
            BootloaderHostError: When the row an the firmware is out of range of the chip's rows
        """
        for array_id, array in self.data.arrays.items():
            start_row, end_row = self.session.get_flash_size(array_id)
            self._log.debug("Array %d: first row %d, last row %d.\n" % (
                array_id, start_row, end_row))
            self.row_ranges[array_id] = (start_row, end_row)
            for row_number in array:
                if row_number < start_row or row_number > end_row:
                    err = "Row %d in array %d out of range. Aborting." % (row_number, array_id)
                    self._log.error(err)
                    raise BootloaderHostError(err)

    def enter_bootloader(self):
        """
        Enters bootloader mode

        Raises:
            BootloaderSiliconMismatch: If there is a mismatch between the chip's and the firmware's silicon ID and
                                       silicon rev
        """
        self._log.info("Initialising bootloader.\n")
        silicon_id, silicon_rev, bootloader_version = self.session.enter_bootloader(self.key)
        self._log.info("Silicon ID 0x%.8x, revision %d.\n" % (silicon_id, silicon_rev))
        if silicon_id != self.data.silicon_id:
            self._log.error("Silicon ID of device (0x%.8x) does not match firmware file (0x%.8x)"
                            % (silicon_id, self.data.silicon_id))
            raise BootloaderSiliconMismatch('id')
        if silicon_rev != self.data.silicon_rev:
            self._log.error("Silicon revision of device (0x%.2x) does not match firmware file (0x%.2x)"
                            % (silicon_rev, self.data.silicon_rev))
            raise BootloaderSiliconMismatch('rev')

    def exit_bootloader(self):
        """
        Exits the bootloader
        """
        self.session.exit_bootloader()

    def check_metadata(self, ignore_app_version: bool = False, ignore_app_id: bool = False) -> list:
        """
        Checks the metadata of the data and of the application on the device to ensure the application ID match and
        the firmware version is greater or equal to the firmware version on the device.

        Args:
            ignore_app_version (bool): To ignore the app version check
            ignore_app_id (bool): To ignore the app ID check

        Returns:
            A list including the errors, which are either :class:`MetadataIDError` or :class:`MetadataAppVersionError`.
            If the returned lenght of this list is zero, then no error exists

        Raises:
            protocol.InvalidApp
            protocol.BootloaderError: If there is an error with a bootloader
        """
        err_ret = []
        if self.is_psoc5:
            metadata = self.session.get_psoc5_metadata(0)
        else:
            metadata = self.session.get_metadata(0)
        self._log.debug("Device application_id %d, version %d.\n" % (
            metadata.app_id, metadata.app_version))

        # TODO: Make this less horribly hacky
        # Fetch from last row of last flash array
        metadata_row = self.data.arrays[max(self.data.arrays.keys())][self.row_ranges[max(self.data.arrays.keys())][1]]
        if self.is_psoc5:
            local_metadata = protocol.GetPSOC5MetadataResponse(metadata_row.data[192:192 + 56])
        else:
            local_metadata = protocol.GetMetadataResponse(metadata_row.data[64:120])

        if not ignore_app_version:
            if metadata.app_version > local_metadata.app_version:
                message = "Device application version is v%d.%d, but local application version is v%d.%d." % (
                    metadata.app_version >> 8, metadata.app_version & 0xFF,
                    local_metadata.app_version >> 8, local_metadata.app_version & 0xFF)
                self._log.warning(message)
                err_ret.append(self.MetadataAppVersionError(metadata.app_version, local_metadata.app_version))

        if not ignore_app_id:
            if metadata.app_id != local_metadata.app_id:
                message = "Device application ID is %d, but local application ID is %d." % (
                    metadata.app_id, local_metadata.app_id)
                self._log.warning(message)
                err_ret.append(self.MetadataIDError(metadata.app_id, local_metadata.app_id))

        return err_ret

    def write_rows(self, progress_def: Callable[[str, int, int], None] = None):
        """
        Writes the firmware rows to the device

        Args:
            progress_def: Optional callback that will be called per row to update the user application.
                          The callback must take 3 arguments, a string with a message, an integer with the
                          current row, and an integer with the total rows
        """
        if progress_def is None:
            progress_def = self.progress
        total = sum(len(x) for x in self.data.arrays.values())
        i = 0
        for array_id, array in self.data.arrays.items():
            for row_number, row in array.items():
                i += 1
                self.session.program_row(array_id, row_number, row.data, self.chunk_size)
                actual_checksum = self.session.get_row_checksum(array_id, row_number)
                if actual_checksum != row.checksum:
                    err = "Checksum does not match in array %d row %d. Expected %.2x, got %.2x! Aborting." % (
                            array_id, row_number, row.checksum, actual_checksum)
                    self._log.error(err)
                    raise BootloaderHostError(err)
                self.progress("Uploading data", i, total)
            self.progress()

    def progress(self, message=None, current=None, total=None):
        if not message:
            self._log.debug("\n")
        else:
            self._log.debug("\r%s (%d/%d)" % (message, current, total))
