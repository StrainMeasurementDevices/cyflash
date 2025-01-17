import logging
import typing
from builtins import super
from builtins import range
import struct
import time

from cyflash.cyacd import ChecksumType


class InvalidPacketError(Exception):
    pass


class BootloaderError(Exception):
    pass


class BootloaderTimeoutError(BootloaderError):
    pass


class BootloaderKeyError(BootloaderError):
    STATUS = 0x01

    def __init__(self):
        super().__init__("The provided security key was incorrect")


class VerificationError(BootloaderError):
    STATUS = 0x02

    def __init__(self):
        super().__init__("The flash verification failed.")


class IncorrectLength(BootloaderError):
    STATUS = 0x03

    def __init__(self):
        super().__init__("The amount of data available is outside the expected range")


class InvalidData(BootloaderError):
    STATUS = 0x04

    def __init__(self):
        super().__init__("The data is not of the proper form")


class InvalidCommand(BootloaderError):
    STATUS = 0x05

    def __init__(self):
        super().__init__("Command unsupported on target device")


class UnexpectedDevice(BootloaderError):
    STATUS = 0x06


class UnsupportedBootloaderVersion(BootloaderError):
    STATUS = 0x07


class InvalidChecksum(BootloaderError):
    STATUS = 0x08


class InvalidArray(BootloaderError):
    STATUS = 0x09


class InvalidFlashRow(BootloaderError):
    STATUS = 0x0A


class ProtectedFlash(BootloaderError):
    STATUS = 0x0B


class InvalidApp(BootloaderError):
    STATUS = 0x0C


class TargetApplicationIsActive(BootloaderError):
    STATUS = 0x0D

    def __init__(self):
        super().__init__("The application is currently marked as active or golden image")


class CallbackResponseInvalid(BootloaderError):
    STATUS = 0x0E


class UnknownError(BootloaderError):
    STATUS = 0x0F


class BootloaderResponse(object):
    FORMAT = ""
    ARGS = ()

    ERRORS = {klass.STATUS: klass for klass in [
        BootloaderKeyError,
        VerificationError,
        IncorrectLength,
        InvalidData,
        InvalidCommand,
        InvalidChecksum,
        UnexpectedDevice,
        UnsupportedBootloaderVersion,
        InvalidArray,
        InvalidFlashRow,
        ProtectedFlash,
        InvalidApp,
        TargetApplicationIsActive,
        CallbackResponseInvalid,
        UnknownError
    ]}

    def __init__(self, data):
        try:
            unpacked = struct.unpack(self.FORMAT, data)
        except struct.error as e:
            raise InvalidPacketError("Cannot unpack packet data '{}': {}".format(data, e))
        for arg, value in zip(self.ARGS, unpacked):
            if arg:
                setattr(self, arg, value)

    @classmethod
    def decode(cls, data, checksum_func):
        start, status, length = struct.unpack("<BBH", data[:4])
        if start != 0x01:
            raise InvalidPacketError("Expected Start Of Packet signature 0x01, found 0x{0:01X}".format(start))

        expected_dlen = len(data) - 7
        if length != expected_dlen:
            raise InvalidPacketError("Expected packet data length {} actual {}".format(length, expected_dlen))

        checksum, end = struct.unpack("<HB", data[-3:])
        data = data[:length + 4]
        if end != 0x17:
            raise InvalidPacketError("Invalid end of packet code 0x{0:02X}, expected 0x17".format(end))
        calculated_checksum = checksum_func(data)
        if checksum != calculated_checksum:
            raise InvalidPacketError(
                "Invalid packet checksum 0x{0:02X}, expected 0x{1:02X}".format(checksum, calculated_checksum))


        # TODO Handle status 0x0D: The application is currently marked as active

        if (status != 0x00):
            response_class = cls.ERRORS.get(status)
            if response_class:
                raise response_class()
            else:
                raise InvalidPacketError("Unknown status code 0x{0:02X}".format(status))

        data = data[4:]
        return cls(data)


class BootloaderCommand(object):
    COMMAND = None
    FORMAT = ""
    ARGS = ()
    RESPONSE = None

    def __init__(self, **kwargs):
        for arg in kwargs:
            if arg not in self.ARGS:
                raise TypeError("Argument {} not in command arguments".format(arg))
        self.args = [kwargs[arg] for arg in self.ARGS]

    @property
    def data(self):
        return struct.pack(self.FORMAT, *self.args)


class BooleanResponse(BootloaderResponse):
    FORMAT = "B"
    ARGS = ("status",)


class EmptyResponse(BootloaderResponse):
    pass


class VerifyChecksumCommand(BootloaderCommand):
    COMMAND = 0x31
    RESPONSE = BooleanResponse


class GetFlashSizeResponse(BootloaderResponse):
    FORMAT = "<HH"
    ARGS = ("first_row", "last_row")


class GetFlashSizeCommand(BootloaderCommand):
    COMMAND = 0x32
    FORMAT = "B"
    ARGS = ("array_id",)
    RESPONSE = GetFlashSizeResponse


class GetAppStatusResponse(BootloaderResponse):
    FORMAT = "<BB"
    ARGS = ("app_valid", "app_active")


class GetAppStatusCommand(BootloaderCommand):
    COMMAND = 0x33
    FORMAT = "B"
    ARGS = ("application_id",)
    RESPONSE = GetAppStatusResponse


class EraseRowCommand(BootloaderCommand):
    COMMAND = 0x34
    FORMAT = "<BH"
    ARGS = ("array_id", "row_id")
    RESPONSE = EmptyResponse


class SyncBootloaderCommand(BootloaderCommand):
    COMMAND = 0x35
    RESPONSE = EmptyResponse


class SetAppActive(BootloaderCommand):
    COMMAND = 0x36
    FORMAT = "B"
    ARGS = ("application_id",)
    RESPONSE = EmptyResponse


class SendDataCommand(BootloaderCommand):
    COMMAND = 0x37
    RESPONSE = EmptyResponse

    def __init__(self, data):
        self._data = data
        super(SendDataCommand, self).__init__()

    @property
    def data(self):
        return self._data


class EnterBootloaderResponse(BootloaderResponse):
    FORMAT = "<IBHB"
    ARGS = ("silicon_id", "silicon_rev", "bl_version", "bl_version_2")


class EnterBootloaderCommand(BootloaderCommand):
    COMMAND = 0x38
    RESPONSE = EnterBootloaderResponse

    def __init__(self, key):
        self._key = key
        super(EnterBootloaderCommand, self).__init__()

    @property
    def data(self):
        if self._key is None:
            return super(EnterBootloaderCommand, self).data
        return super(EnterBootloaderCommand, self).data + struct.pack("<BBBBBB",
                                                                      *self._key)


class ProgramRowCommand(BootloaderCommand):
    COMMAND = 0x39
    FORMAT = "<BH"
    ARGS = ("array_id", "row_id")
    RESPONSE = EmptyResponse

    def __init__(self, data, **kwargs):
        self._data = data
        super(ProgramRowCommand, self).__init__(**kwargs)

    @property
    def data(self):
        return super(ProgramRowCommand, self).data + self._data


class ChecksumResponse(BootloaderResponse):
    FORMAT = "<B"
    ARGS = ("checksum",)


class VerifyRowCommand(BootloaderCommand):
    COMMAND = 0x3A
    FORMAT = "<BH"
    ARGS = ("array_id", "row_id")
    RESPONSE = ChecksumResponse


class ExitBootloaderCommand(BootloaderCommand):
    COMMAND = 0x3B
    RESPONSE = EmptyResponse


class GetMetadataResponse(BootloaderResponse):
    # TODO: metadata format differs in PSOC3 and 4/5
    FORMAT = "<BIII7xBBHHH28x"
    ARGS = (
        "checksum",
        "bootloadable_addr",
        "bootloader_last_row",
        "bootloadable_len",
        "active",
        "verified",
        "app_version",
        "app_id",
        "custom_id",
    )

    def __str__(self):
        sb = []
        for key in self.__dict__:
            sb.append("{key}='{value}'".format(key=key, value=self.__dict__[key]))

        return ', '.join(sb)

    def __repr__(self):
        return self.__str__()


class GetPSOC5MetadataResponse(BootloaderResponse):
    # TODO: metadata format differs in PSOC3 and 4/5
    FORMAT = "<BIHxxIxxxBBHHHI28x"
    ARGS = (
        "checksum",
        "bootloadable_addr",
        "bootloader_last_row",
        "bootloadable_len",
        "active",
        "verified",
        "bootloader_version",
        "app_id",
        "app_version",
        "app_custom_id",
    )

    def __str__(self):
        sb = []
        for key in self.__dict__:
            sb.append("{key}='{value}'".format(key=key, value=self.__dict__[key]))

        return ', '.join(sb)

    def __repr__(self):
        return self.__str__()


class GetMetadataCommand(BootloaderCommand):
    COMMAND = 0x3C
    FORMAT = "<B"
    ARGS = ("application_id",)
    RESPONSE = GetMetadataResponse


class GetPSOC5MetadataCommand(BootloaderCommand):
    COMMAND = 0x3C
    FORMAT = "<B"
    ARGS = ("application_id",)
    RESPONSE = GetPSOC5MetadataResponse


class SerialTransport(object):
    """
        A serial transport
    """
    def __init__(self, f):
        """
            Args:
                f: The serial object. This class can be a pySerial's Serial object, or any class
                   with the functions `write` and `read`
        """
        self.f = f
        self._log = logging.getLogger('cyflash-serial-transport')

    def send(self, data):
        for part in bytearray(data):
            self._log.debug("Sent out: 0x{:02x}".format(part))
        self.f.write(data)

    def recv(self):
        data = self.f.read(4)
        if len(data) < 4:
            raise BootloaderTimeoutError("Timed out waiting for Bootloader response.")
        size = struct.unpack("<H", data[-2:])[0]
        data += self.f.read(size + 3)
        for part in bytearray(data):
            self._log.debug("Read: 0x{:02x}".format(part))
        if len(data) < size + 7:
            raise BootloaderTimeoutError("Timed out waiting for Bootloader response.")
        return data


class CANbusTransport(object):
    MESSAGE_CLASS = None

    def __init__(self, transport, frame_id, timeout, echo_frames, wait_send_ms):
        self.transport = transport
        self.frame_id = frame_id
        self.timeout = timeout
        self.echo_frames = echo_frames
        self.wait_send_s = wait_send_ms / 1000.0
        self._last_sent_frame = None

    def send(self, data):
        start = 0
        maxlen = len(data)
        while (start < maxlen):
            remaining = maxlen - start

            if (remaining > 8):
                msg = self.MESSAGE_CLASS(
                    extended_id=False,
                    arbitration_id=self.frame_id,
                    data=data[start:start + 8]
                )
            else:
                msg = self.MESSAGE_CLASS(
                    extended_id=False,
                    arbitration_id=self.frame_id,
                    data=data[start:]
                )

            # Flush input mailbox(es)
            while (self.transport.recv(timeout=0)):
                pass

            self.transport._send(msg)
            self._last_sent_frame = msg
            if (self.echo_frames):
                # Read back the echo message
                while (True):
                    frame = self.transport.recv(self.timeout)
                    if (not frame):
                        raise BootloaderTimeoutError("Did not receive echo frame within {} timeout".format(self.timeout))
                    # Don't check the frame arbitration ID, it may be used for varying purposes
                    if (frame.data[:frame.dlc] != msg.data[:msg.dlc]):
                        continue
                    # Ok, got a good frame
                    break
            elif (self.wait_send_s > 0.0):
                time.sleep(self.wait_send_s)

            start += 8

    def recv(self):
        # Response packets read from the Bootloader have the following structure:
        # Start of Packet (0x01): 1 byte
        # Status Code: 1 byte
        # Data Length: 2 bytes
        # Data: N bytes of data
        # Checksum: 2 bytes
        # End of Packet (0x17): 1 byte

        data = bytearray()
        # Read first frame, contains data length
        while True:
            frame = self.transport.recv(self.timeout)
            if not frame:
                raise BootloaderTimeoutError("Timed out waiting for Bootloader 1st response frame")

            if frame.arbitration_id != self.frame_id:
                continue

            # Don't check the frame arbitration ID, it may be used for varying purposes

            if len(frame.data) < 4:
                raise BootloaderTimeoutError("Unexpected response data: length {}, minimum is 4".format(len(frame.data)))

            if frame.data[0] != 0x01:
                raise BootloaderTimeoutError("Unexpected start of frame data: 0x{0:02X}, expected 0x01".format(frame.data[0]))

            break

        data += frame.data[:frame.dlc]

        # 4 initial bytes, reported size, 3 tail
        total_size = 4 + (struct.unpack("<H", data[2:4])[0]) + 3
        while len(data) < total_size:
            frame = self.transport.recv(self.timeout)
            if not frame:
                raise BootloaderTimeoutError("Timed out waiting for Bootloader response frame")

            if self.echo_frames and (frame.arbitration_id != self.frame_id):
                # Got a frame from another device, ignore
                continue

            data += frame.data[:frame.dlc]

        return data


class BootloaderSession(object):
    """
      The bootloader session object
    """

    def __init__(self, transport: typing.Union[SerialTransport, CANbusTransport], checksum_type: ChecksumType):
        """
            Args:
                transport: The transport to send the data over. Right now only SerialTransport and CANbusTransport are
                           supported
                checksum_type: The checksum type read from the firmware file (see :class:`cyacd.BootloaderData` for more
                               details
        """
        self._log = logging.getLogger('BootloaderSession')
        self.transport = transport
        if checksum_type == ChecksumType.crc16:
            self.checksum_func = self.crc16_checksum
        elif checksum_type == ChecksumType.sum_2complement:
            self.checksum_func = self.sum_2complement_checksum
        else:
            raise UserWarning("Invalid checksum type")

    def enter_bootloader(self, key: list = None):
        """
            Enters the bootloader

            Args:
                key (int, optional): The secret key for the bootloader, as a list of 6 hex codes.

            Returns:
                A tuple of:
                  - The silicon ID
                  - The silicon rev
                  - The bootloader rev
        """
        self._log.debug('Entering bootloader')
        response = self._send(EnterBootloaderCommand(key))
        return response.silicon_id, response.silicon_rev, response.bl_version | (response.bl_version_2 << 16)

    def application_status(self, application_id):
        response = self._send(GetAppStatusCommand(application_id=application_id))
        return response.app_valid, response.app_active

    def exit_bootloader(self):
        """
            Exits the bootloader
        """
        self._send(ExitBootloaderCommand(), read=False)

    def get_flash_size(self, array_id):
        response = self._send(GetFlashSizeCommand(array_id=array_id))
        return response.first_row, response.last_row

    def verify_checksum(self):
        return bool(self._send(VerifyChecksumCommand()).status)

    def get_metadata(self, application_id=0):
        return self._send(GetMetadataCommand(application_id=application_id))

    def get_psoc5_metadata(self, application_id=0):
        return self._send(GetPSOC5MetadataCommand(application_id=application_id))

    def program_row(self, array_id, row_id, rowdata, chunk_size):
        chunked = [rowdata[i:i + chunk_size] for i in range(0, len(rowdata), chunk_size)]
        for chunk in chunked[0:-1]:
            self._send(SendDataCommand(chunk))
        self._send(ProgramRowCommand(chunked[-1], array_id=array_id, row_id=row_id))

    def erase_row(self, array_id, row_id):
        self._send(EraseRowCommand(array_id=array_id, row_id=row_id))

    def get_row_checksum(self, array_id, row_id):
        return self._send(VerifyRowCommand(array_id=array_id, row_id=row_id)).checksum

    def set_application_active(self, application_id):
        self._send(SetAppActive(application_id=application_id))

    def _send(self, command, read=True):
        """
            Internal function that structures the data to be sent over the transport
        """
        data = command.data
        packet = b"\x01" + struct.pack("<BH", command.COMMAND, len(data)) + data
        packet = packet + struct.pack('<H', self.checksum_func(packet)) + b"\x17"
        self.transport.send(packet)
        if read:
            response = self.transport.recv()
            return command.RESPONSE.decode(response, self.checksum_func)
        else:
            return None

    @staticmethod
    def crc16_checksum(data):
        crc = 0xffff

        for b in data:
            if not isinstance(b, int):
                b = ord(b)
            for i in range(8):
                if (crc & 1) ^ (b & 1):
                    crc = (crc >> 1) ^ 0x8408
                else:
                    crc >>= 1
                b >>= 1

        crc = (crc << 8) | (crc >> 8)
        return ~crc & 0xffff

    @staticmethod
    def sum_2complement_checksum(data):
        if type(data) is str:
            return (1 + ~sum([ord(c) for c in data])) & 0xFFFF
        elif type(data) in (bytearray, bytes):
            return (1 + ~sum(data)) & 0xFFFF
