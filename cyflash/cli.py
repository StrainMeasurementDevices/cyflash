"""
PSoC bootloader command line tool

This Python file encompasses the command line utility cyflash.
If you need to use this package in your own program, then use bootload.py
"""

import argparse
import codecs
import time
import six
import sys
import logging
import logging.config

from builtins import input

from cyflash import bootload
from cyflash import protocol
from cyflash import cyacd


def auto_int(x):
    return int(x, 0)


parser = argparse.ArgumentParser(description="Bootloader tool for Cypress PSoC devices")

group = parser.add_mutually_exclusive_group(required=True)
group.add_argument(
    '--serial',
    action='store',
    dest='serial',
    metavar='PORT',
    default=None,
    help="Use a serial interface")
group.add_argument(
    '--canbus',
    action='store',
    dest='canbus',
    metavar='BUSTYPE',
    default=None,
    help="Use a CANbus interface (requires python-can)")

parser.add_argument(
    '--serial_baudrate',
    action='store',
    dest='serial_baudrate',
    metavar='BAUD',
    default=115200,
    type=int,
    help="Baud rate to use when flashing using serial (default 115200)")
parser.add_argument(
    '--parity',
    action='store',
    default='None',
    type=str,
    help="Desired parity (e.g. None, Even, Odd, Mark, or Space)")
parser.add_argument(
    '--stopbits',
    action='store',
    default='1',
    type=str,
    help="Desired stop bits (e.g. 1, 1.5, or 2)")
parser.add_argument(
    '--dtr',
    action='store_true',
    help="set DTR state true (default false)")
parser.add_argument(
    '--rts',
    action='store_true',
    help="set RTS state true (default false)")
parser.add_argument(
    '--canbus_baudrate',
    action='store',
    dest='canbus_baudrate',
    metavar='BAUD',
    default=125000,
    type=int,
    help="Baud rate to use when flashing using CANbus (default 125000)")
parser.add_argument(
    '--canbus_channel',
    action='store',
    dest='canbus_channel',
    metavar='CANBUS_CHANNEL',
    default=0,
    help="CANbus channel to be used")
parser.add_argument(
    '--canbus_id',
    action='store',
    dest='canbus_id',
    metavar='CANBUS_ID',
    default=0,
    type=auto_int,
    help="CANbus frame ID to be used")

group = parser.add_mutually_exclusive_group(required=False)
group.add_argument(
    '--canbus_echo',
    action='store_true',
    dest='canbus_echo',
    default=False,
    help="Use echoed back received CAN frames to keep the host in sync")
group.add_argument(
    '--canbus_wait',
    action='store',
    dest='canbus_wait',
    metavar='CANBUS_WAIT',
    default=5,
    type=int,
    help="Wait for CANBUS_WAIT ms amount of time after sending a frame if you're not using echo frames as a way to keep host in sync")

parser.add_argument(
    '--timeout',
    action='store',
    dest='timeout',
    metavar='SECS',
    default=5.0,
    type=float,
    help="Time to wait for a Bootloader response (default 5)")

group = parser.add_mutually_exclusive_group()
group.add_argument(
    '--downgrade',
    action='store_true',
    dest='downgrade',
    default=None,
    help="Don't prompt before flashing old firmware over newer")
group.add_argument(
    '--nodowngrade',
    action='store_false',
    dest='downgrade',
    default=None,
    help="Fail instead of prompting when device firmware is newer")

group = parser.add_mutually_exclusive_group()
group.add_argument(
    '--newapp',
    action='store_true',
    dest='newapp',
    default=None,
    help="Don't prompt before flashing an image with a different application ID")
group.add_argument(
    '--nonewapp',
    action='store_false',
    dest='newapp',
    default=None,
    help="Fail instead of flashing an image with a different application ID")

parser.add_argument(
    'logging_config',
    action='store',
    type=argparse.FileType(mode='r'),
    nargs='?',
    help="Python logging configuration file")

parser.add_argument(
    '--psoc5',
    action='store_true',
    dest='psoc5',
    default=False,
    help="Add tag to parse PSOC5 metadata")


def validate_key(string):
    if len(string) != 14:
        raise argparse.ArgumentTypeError("key is of unexpected length")

    try:
        val = int(string, base=16)
        key = []
        key.append((val >> 40) & 0xff)
        key.append((val >> 32) & 0xff)
        key.append((val >> 24) & 0xff)
        key.append((val >> 16) & 0xff)
        key.append((val >> 8) & 0xff)
        key.append(val & 0xff)
        return key
    except ValueError:
        raise argparse.ArgumentTypeError("key is of unexpected format")


parser.add_argument(
    '--key',
    action='store',
    dest='key',
    default=None,
    type=validate_key,
    help="Optional security key (six bytes, on the form 0xAABBCCDDEEFF)")

DEFAULT_CHUNKSIZE = 25
parser.add_argument(
    '-cs',
    '--chunk-size',
    action='store',
    dest='chunk_size',
    default=DEFAULT_CHUNKSIZE,
    type=int,
    help="Chunk size to use for transfers - default %d" % DEFAULT_CHUNKSIZE)

parser.add_argument(
    '--dual-app',
    action='store_true',
    dest='dual_app',
    default=False,
    help="The bootloader is dual-application - will mark the newly flashed app as active")

parser.add_argument(
    '-v',
    '--verbose',
    action='store_true',
    dest='verbose',
    default=False,
    help="Enable verbose debug output")

parser.add_argument(
    'image',
    action='store',
    type=argparse.FileType(mode='r'),
    help="Image to read flash data from")


class BootloaderError(Exception):
    pass


def make_session(args, checksum_type):
    if args.serial:
        import serial
        ser = serial.Serial()
        ser.port = args.serial
        ser.baudrate = args.serial_baudrate
        ser.parity = parity_convert(args.parity)
        mapping = {"1": serial.STOPBITS_ONE,
                   "1.5": serial.STOPBITS_ONE_POINT_FIVE,
                   "2": serial.STOPBITS_TWO,
                   }
        if not args.stopbits in mapping:
            print('\nillegal argument', args.stopbits, 'for stopbit using ONE STOPBIT instead\n')
        ser.stopbits = mapping.get(args.stopbits, serial.STOPBITS_ONE)
        ser.timeout = args.timeout
        ser.rts = args.dtr
        ser.dtr = args.rts
        ser.open()
        ser.flushInput()  # need to clear any garbage off the serial port
        ser.flushOutput()
        transport = protocol.SerialTransport(ser)
    elif args.canbus:
        import can
        # Remaining configuration options should follow python-can practices
        canbus = can.interface.Bus(bustype=args.canbus, channel=args.canbus_channel, bitrate=args.canbus_baudrate)
        # Wants timeout in ms, we have it in s
        transport = protocol.CANbusTransport(canbus, args.canbus_id, int(args.timeout * 1000), args.canbus_echo,
                                             args.canbus_wait)
        transport.MESSAGE_CLASS = can.Message
    else:
        raise BootloaderError("No valid interface specified")

    return protocol.BootloaderSession(transport, checksum_type)


def seek_permission(argument, message):
    if argument is not None:
        return lambda remote, local: argument
    else:
        def prompt(*args):
            while True:
                result = input(message % args)
                if result.lower().startswith('y'):
                    return True
                elif result.lower().startswith('n'):
                    return False

        return prompt


def parity_convert(value):
    import serial
    if value.lower() in ("none", "n"):
        parity = serial.PARITY_NONE
    elif value.lower() in ("even", "e"):
        parity = serial.PARITY_EVEN
    elif value.lower() in ("odd", "o"):
        parity = serial.PARITY_ODD
    else:
        parity = serial.PARITY_NONE
        print('\nillegal argument', value, 'for parity using', parity, 'instead\n')

    return parity


def main():
    logging.basicConfig(level=logging.DEBUG)
    args = parser.parse_args()

    if args.logging_config:
        logging.config.fileConfig(args.logging_config)

    if six.PY3:
        t0 = time.perf_counter()
    else:
        t0 = time.clock()
    data = cyacd.BootloaderData.read(args.image)
    session = make_session(args, data.checksum_type)
    bl = bootload.BootloaderHost(session, args)
    try:
        bl.bootload(
            data,
            seek_permission(
                args.downgrade,
                "Device version %d is greater than local version %d. Flash anyway? (Y/N)"),
            seek_permission(
                args.newapp,
                "Device app ID %d is different from local app ID %d. Flash anyway? (Y/N)"),
            args.psoc5)
    except (protocol.BootloaderError, BootloaderError) as e:
        print("Unhandled error: {}".format(e))
        return 1
    if (six.PY3):
        t1 = time.perf_counter()
    else:
        t1 = time.clock()
    print("Total running time {0:02.2f}s".format(t1 - t0))
    return 0


if __name__ == '__main__':
    sys.exit(main())
