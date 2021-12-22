"""
    The main bootloader class file

    This module is what contains the userspace bootloader host class
"""

import logging
import typing

import cyacd
import protocol


class BootloaderError(Exception):
    pass


class BootloaderSiliconMismatch(Exception):
    """
    Exception when the device silicon and firmware silicon don't match.

    The variable `what_is_mismatched` of this exception indicates what was mismatched: `rev` for the silicon revision
                or `id` for the silicon ID.
    """
    def __init__(self, what_is_mismatched):
        self.what_is_mismatched = what_is_mismatched


class BootloaderHost(object):
    def __init__(self, transport: typing.Union[protocol.SerialTransport, protocol.CANbusTransport], data: cyacd.BootloaderData,
                 chunck_size: int = 25, key: list = None, is_dual_app: bool = False):
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
        self.session = protocol.BootloaderSession(self.transport, self.data.checksum_type)
        self.chunk_size = chunck_size
        self.dual_app = is_dual_app
        self.row_ranges = {}

    # def bootload(self, downgrade, newapp, psoc5):
    #     self._log.info("Entering bootload.\n")
    #     self.enter_bootloader()
    #     if self.dual_app:
    #         self._log.info("Getting application status.\n")
    #         app_area_to_flash = self.application_status()
    #     self._log.info("Verifying row ranges.\n")
    #     self.verify_row_ranges()
    #     self._log.info("Checking metadata.\n")
    #     self.check_metadata(downgrade, newapp, psoc5)
    #     self._log.info("Starting flash operation.\n")
    #     self.write_rows()
    #     if not self.session.verify_checksum():
    #         raise BootloaderError("Flash checksum does not verify! Aborting.")
    #     else:
    #         self._log.info("Device checksum verifies OK.\n")
    #     if self.dual_app:
    #         self.set_application_active(app_area_to_flash)
    #     self._log.info("Rebooting device.\n")
    #     self.session.exit_bootloader()

    def set_application_active(self, application_id):
        self._log.info("Setting application %d as active.\n" % application_id)
        self.session.set_application_active(application_id)

    def application_status(self):
        to_flash = None
        for app in [0, 1]:
            app_valid, app_active = self.session.application_status(app)
            self._log.debug("App %d: valid: %s, active: %s\n" % (app, app_valid, app_active))
            if app_active == 0:
                to_flash = app

        if to_flash is None:
            raise BootloaderError("Failed to find inactive app to flash. Aborting.")
        self._log.debug("Will flash app %d.\n" % to_flash)
        return to_flash

    def verify_row_ranges(self):
        for array_id, array in self.data.arrays.items():
            start_row, end_row = self.session.get_flash_size(array_id)
            self._log.debug("Array %d: first row %d, last row %d.\n" % (
                array_id, start_row, end_row))
            self.row_ranges[array_id] = (start_row, end_row)
            for row_number in array:
                if row_number < start_row or row_number > end_row:
                    raise BootloaderError(
                        "Row %d in array %d out of range. Aborting."
                        % (row_number, array_id))

    def enter_bootloader(self):
        """
        Enters bootloader mode
        Raises:

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

    def check_metadata(self, downgrade, newapp, psoc5):
        try:
            if psoc5:
                metadata = self.session.get_psoc5_metadata(0)
            else:
                metadata = self.session.get_metadata(0)
            self._log.debug("Device application_id %d, version %d.\n" % (
                metadata.app_id, metadata.app_version))
        except protocol.InvalidApp:
            self._log.warning("No valid application on device.\n")
            return
        except protocol.BootloaderError as e:
            self._log.warning("Cannot read metadata from device: {}\n".format(e))
            return

        # TODO: Make this less horribly hacky
        # Fetch from last row of last flash array
        metadata_row = self.data.arrays[max(self.data.arrays.keys())][self.row_ranges[max(self.data.arrays.keys())][1]]
        if psoc5:
            local_metadata = protocol.GetPSOC5MetadataResponse(metadata_row.data[192:192 + 56])
        else:
            local_metadata = protocol.GetMetadataResponse(metadata_row.data[64:120])

        if metadata.app_version > local_metadata.app_version:
            message = "Device application version is v%d.%d, but local application version is v%d.%d." % (
                metadata.app_version >> 8, metadata.app_version & 0xFF,
                local_metadata.app_version >> 8, local_metadata.app_version & 0xFF)
            if not downgrade(metadata.app_version, local_metadata.app_version):
                raise ValueError(message + " Aborting.")

        if metadata.app_id != local_metadata.app_id:
            message = "Device application ID is %d, but local application ID is %d." % (
                metadata.app_id, local_metadata.app_id)
            if not newapp(metadata.app_id, local_metadata.app_id):
                raise ValueError(message + " Aborting.")

    def write_rows(self):
        total = sum(len(x) for x in self.data.arrays.values())
        i = 0
        for array_id, array in self.data.arrays.items():
            for row_number, row in array.items():
                i += 1
                self.session.program_row(array_id, row_number, row.data, self.chunk_size)
                actual_checksum = self.session.get_row_checksum(array_id, row_number)
                if actual_checksum != row.checksum:
                    raise BootloaderError(
                        "Checksum does not match in array %d row %d. Expected %.2x, got %.2x! Aborting." % (
                            array_id, row_number, row.checksum, actual_checksum))
                self.progress("Uploading data", i, total)
            self.progress()

    def progress(self, message=None, current=None, total=None):
        if not message:
            self._log.debug("\n")
        else:
            self._log.debug("\r%s (%d/%d)" % (message, current, total))
