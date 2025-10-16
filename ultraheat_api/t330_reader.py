"""
T330 Heat Meter Serial Communication Module

This module implements the communication protocol for Landis+Gyr T330 heat meters.
The communication follows a specific sequence:

1. Send version string request to establish communication
2. Send application reset command and wait for confirmation (0xE5 response)
3. Send SND_UD payload to initiate data transfer
4. Send baud rate switch command
5. Switch to 9600 baud and collect M-Bus data frames

Serial Configuration:
- Initial: 2400 baud, 8 data bits, Even parity, 1 stop bit
- Final: 9600 baud, 8 data bits, Even parity, 1 stop bit
- Timeouts: 1500ms for handshake sequences, 2000ms for data collection
"""
import logging
import time
from typing import Tuple
from datetime import datetime
from pathlib import Path

import serial
from serial import Serial

_LOGGER = logging.getLogger(__name__)


class T330Reader:
    """
    T330 Heat Meter Serial Communication Reader
    
    Implements the communication protocol for Landis+Gyr T330 heat meters
    over serial/optical interface. Handles the complete handshake sequence
    and data collection process.
    """
    
    def __init__(
        self,
        port: str,
        timeout: float = 2.0,
        retries: int = 3,
    ) -> None:
        """
        Initialize T330 reader.
        
        Args:
            port: Serial port path (e.g., '/dev/ttyUSB0', 'COM3')
            timeout: Communication timeout in seconds
            retries: Number of retry attempts for failed operations
        """
        self._port = port
        self.timeout = timeout
        self.retries = retries

    def read(self) -> Tuple[str, bytes]:
        """Execute the complete T330 communication sequence and return raw M-Bus data."""
        with self._connect_serial(baudrate=2400) as conn:
            # Set timeout for handshake sequences (1.5 seconds minimum)
            conn.timeout = max(1.5, float(self.timeout))
            self._sequence_1(conn)  # Version string request
            self._sequence_2(conn)  # Application reset
            self._sequence_3(conn)  # SND_UD payload
            raw_bytes = self._sequence_5_and_read(conn)  # Baud switch and data collection
        return "T330", raw_bytes

    def _connect_serial(self, baudrate: int) -> Serial:
        return Serial(
            self._port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_EVEN,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.timeout,
            xonxoff=False,
            rtscts=False,
        )

    def _write_and_read(self, conn: Serial, payload: bytes, read_size: int, tries: int, pad_zeros: int = 0) -> bytes:
        """
        Send a command frame to the meter and read the response.
        
        Args:
            conn: Serial connection to the meter
            payload: Command bytes to send
            read_size: Maximum bytes to read in response
            tries: Number of retry attempts
            pad_zeros: Number of null bytes to send before the payload (meter synchronization)
        
        Returns:
            Response bytes from the meter, or empty bytes if no response
        """
        zero_pad = b"\x00" * pad_zeros if pad_zeros > 0 else b""
        for attempt in range(tries):
            _LOGGER.debug(
                "T330: sending %d+%d bytes (attempt %s/%s)",
                len(zero_pad),
                len(payload),
                attempt + 1,
                tries,
            )
            conn.reset_input_buffer()
            conn.reset_output_buffer()
            if zero_pad:
                conn.write(zero_pad)
            written = conn.write(payload)
            if written != len(payload):
                _LOGGER.debug("T330: partial write %s/%s", written, len(payload))
            conn.flush()
            # Brief settling time to allow meter to process command
            time.sleep(0.01)
            _LOGGER.debug("T330: waiting for response (max %d bytes)", read_size)
            data = conn.read(read_size)
            if data:
                _LOGGER.debug("T330: received %d bytes: %s", len(data), data.hex())
                return data
            else:
                _LOGGER.debug("T330: no response received")
            # Wait between retry attempts
            time.sleep(0.3)
        _LOGGER.debug("T330: exhausted %d attempts, no response", tries)
        return b""

    def _sequence_1(self, conn: Serial) -> None:
        """
        Sequence 1: Request version string from the meter.
        
        Sends a version string request command and waits for a response containing "Nb+" 
        which indicates the meter is responding correctly.
        """
        seq = bytes([0x68, 0x05, 0x05, 0x68, 0x73, 0xFE, 0x51, 0x0F, 0x0F, 0xE0, 0x16])
        _LOGGER.debug("T330: sequence 1 - read version string")
        
        # Retry up to 10 times, looking for version string pattern "Nb+"
        for rpr_cnt in range(10, 0, -1):
            attempt_num = 11 - rpr_cnt
            _LOGGER.debug("T330: sequence 1 attempt %d/10", attempt_num)
            
            resp = self._write_and_read(conn, seq, read_size=50, tries=1, pad_zeros=200)
            if resp:
                _LOGGER.debug("T330: received %d bytes: %s", len(resp), resp.hex())
                
                # Check if response is all zeros (indicates no real communication)
                if resp == b'\x00' * len(resp):
                    _LOGGER.debug("T330: received all-zero response, meter not responding (attempt %d/10)", attempt_num)
                    # Continue to next attempt
                # Check for expected version string pattern in response  
                elif b"Nb+" in resp:
                    _LOGGER.debug("T330: version string found! Continue...")
                    return
                else:
                    _LOGGER.debug("T330: received data but no 'Nb+' pattern found (attempt %d/10)", attempt_num)
                    # Continue to next attempt
            else:
                _LOGGER.debug("T330: no response received (attempt %d/10)", attempt_num)
            
            # Add a delay between attempts
            if rpr_cnt > 1:  # Don't delay after the last attempt
                _LOGGER.debug("T330: waiting 0.5s before next attempt...")
                time.sleep(0.5)
        
        # If we get here, all 10 attempts failed
        _LOGGER.error("T330: All 10 sequence 1 attempts failed - version string not found")
        raise RuntimeError("T330: Version string not found. Exiting...")

    def _sequence_2(self, conn: Serial) -> None:
        """
        Sequence 2: Send application reset command.
        
        Resets the meter's application layer and waits for confirmation byte 0xE5.
        """
        seq = bytes([0x68, 0x04, 0x04, 0x68, 0x53, 0xFE, 0x50, 0x00, 0xA1, 0x16])
        _LOGGER.debug("T330: sequence 2 - application reset")
        
        # Retry up to 5 times, looking for confirmation byte 0xE5
        for rpr_cnt in range(5, 0, -1):
            resp = self._write_and_read(conn, seq, read_size=50, tries=1, pad_zeros=200)
            if resp:
                _LOGGER.debug("T330: received %d bytes", len(resp))
                if b"\xE5" in resp:
                    _LOGGER.debug("T330: Character E5 found! Continue...")
                    return
            _LOGGER.debug("T330: listen, try %d", rpr_cnt)
        
        raise RuntimeError("T330: E5 not found. Exiting...")

    def _sequence_3(self, conn: Serial) -> None:
        """
        Sequence 3: Send SND_UD (Send User Data) command.
        
        Initiates the data transfer session with the meter. Expects an 11-byte response.
        """
        seq = bytes([0x68, 0x07, 0x07, 0x68, 0x73, 0xFE, 0x51, 0x0F, 0x70, 0x00, 0x01, 0x42, 0x16])
        _LOGGER.debug("T330: sequence 3 - SND_UD with payload")
        
        # Retry up to 2 times, expecting exactly 11 bytes in response
        for rpr_cnt in range(2, 0, -1):
            resp = self._write_and_read(conn, seq, read_size=50, tries=1, pad_zeros=200)
            if resp:
                _LOGGER.debug("T330: received %d bytes", len(resp))
                if len(resp) == 11:
                    _LOGGER.debug("T330: 11 characters found! Continue...")
                    return
            _LOGGER.debug("T330: listen, try %d", rpr_cnt)
        
        raise RuntimeError("T330: 11-char response not found. Exiting...")

    def _sequence_5_and_read(self, conn: Serial) -> bytes:
        """
        Sequence 5: Send baud rate switch command and collect M-Bus data.
        
        Sends a short frame to signal baud rate change, then switches to 9600 baud
        and collects all available M-Bus data frames from the meter.
        """
        seq = bytes([0x10, 0x7C, 0xFE, 0x7A, 0x16])
        _LOGGER.debug("T330: sequence 5 - short frame to switch baud to 9600")
        
        # Send command with padding, no response expected at this baud rate
        zero_pad = b"\x00" * 200
        conn.reset_input_buffer()
        conn.reset_output_buffer()
        conn.write(zero_pad)
        count_out = conn.write(seq)
        conn.flush()
        _LOGGER.debug("T330: written %d bytes", count_out)
        if count_out != len(seq):
            _LOGGER.warning("T330: write incomplete")
        
        # Wait for meter to switch baud rate
        _LOGGER.debug("T330: waiting 1.5s for meter to switch baudrate")
        time.sleep(1.5)

        # Switch local UART to match meter's new baud rate (9600 8E1)
        conn.baudrate = 9600
        conn.parity = serial.PARITY_EVEN
        conn.bytesize = serial.EIGHTBITS
        conn.stopbits = serial.STOPBITS_ONE
        conn.timeout = 2.0  # 2 second timeout for data collection
        conn.xonxoff = False
        conn.rtscts = False
        _LOGGER.debug("T330: switched to %d baud", conn.baudrate)

        # Collect all M-Bus data frames from the meter
        # Continue reading until no more data is available
        buffer = bytearray()
        chars = 0
        zero_count_streak = 0
        
        while True:
            _LOGGER.debug("T330: attempting to read data")
            chunk = conn.read(10000)  # Read up to 10KB at a time
            count = len(chunk)
            
            if count > 0:
                buffer.extend(chunk)
                chars += count
                zero_count_streak = 0  # Reset consecutive no-data count
                _LOGGER.debug("T330: received %d total bytes", chars)
                # Continue reading when data is received - more may be available
                continue
            else:
                # No data received in this read attempt
                zero_count_streak += 1
                _LOGGER.debug("T330: no data, consecutive empty reads: %d", zero_count_streak)
                
                # Stop after multiple consecutive empty reads (meter finished sending)
                if zero_count_streak >= 10:
                    break
                    
                # Brief wait before next read attempt
                time.sleep(0.1)
                continue

        _LOGGER.debug("T330: collected %d bytes of M-Bus data", len(buffer))
        # Save debug capture when DEBUG logging is enabled
        self._dump_debug_capture(bytes(buffer))
        return bytes(buffer)

    def _dump_debug_capture(self, data: bytes) -> None:
        """
        Save raw M-Bus data to a timestamped file for debugging purposes.
        
        Only writes files when DEBUG logging is enabled. Files are saved to the tests/ 
        directory with format: LGUT330_YYYYMMDD_HHMM.bin
        
        Args:
            data: Raw M-Bus bytes received from the meter
        """
        if not data:
            return
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return
        try:
            project_root = Path(__file__).resolve().parent.parent
            tests_dir = project_root / "tests"
            tests_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M")
            filename = f"LGUT330_{ts}.bin"
            out_path = tests_dir / filename
            with open(out_path, "wb") as f:
                f.write(data)
            _LOGGER.debug("T330: wrote debug capture to %s (%d bytes)", out_path, len(data))
        except Exception as e:
            _LOGGER.debug("T330: failed to write debug capture: %s", e)


