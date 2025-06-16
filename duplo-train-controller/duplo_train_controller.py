#!/usr/bin/env python3

import asyncio
import sys
from bleak import BleakScanner, BleakClient
from bleak.exc import BleakError
import struct
import logging
import select
import threading
import termios
import tty

# Configure logging - reduce Bleak's verbosity
logging.basicConfig(level=logging.INFO)
logging.getLogger("bleak").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

LEGO_MANUFACTURER_DATA = 0x0397

# LEGO Wireless Protocol 3.0.00
SERVICE_UUID = "00001623-1212-efde-1623-785feabcd123"
CHARACTERISTIC_UUID = "00001624-1212-efde-1623-785feabcd123"

# Port IDs - Updated based on actual device discovery
MOTOR_PORT = 0x32      # Port 50 - Duplo Train Motor
SPEAKER_PORT = 0x01
LED_PORT = 0x11
VOLTAGE_PORT = 0x3B
CURRENT_PORT = 0x3C

# Message types
MSG_HUB_PROPERTIES = 0x01
MSG_HUB_ACTIONS = 0x02
MSG_HUB_ATTACHED_IO = 0x04
MSG_PORT_INFORMATION_REQUEST = 0x21
MSG_PORT_MODE_INFORMATION_REQUEST = 0x22
MSG_PORT_OUTPUT_COMMAND = 0x81
MSG_PORT_INPUT_FORMAT_SETUP_SINGLE = 0x41

class DuploTrainController:
    def __init__(self):
        self.client = None
        self.device = None
        self.motor_port = None
        self.speaker_port = None
        self.led_port = None
        self.last_notification = None
        
    async def scan_for_trains(self):
        """Scan for LEGO Duplo trains"""
        logger.info("Scanning for LEGO Duplo trains...")
        devices = await BleakScanner.discover(timeout=10.0)
        
        lego_devices = []
        for device in devices:
            if device.metadata.get('manufacturer_data'):
                for mfr_id in device.metadata['manufacturer_data']:
                    if mfr_id == LEGO_MANUFACTURER_DATA:
                        lego_devices.append(device)
                        logger.info(f"Found LEGO device: {device.name} - {device.address}")
        
        return lego_devices
    
    async def notification_handler(self, sender, data):
        """Handle notifications from the train"""
        logger.debug(f"Notification from {sender}: {data.hex()}")
        
        # Store last notification for test evaluation
        self.last_notification = data
        
        # Parse message type
        if len(data) >= 3:
            msg_type = data[2]
            if msg_type == 0x04:  # Port information
                port = data[3]
                event = data[4]
                if event == 0x01:  # Attached
                    device_type = data[5] | (data[6] << 8) if len(data) > 6 else data[5]
                    logger.info(f"Device attached on port {port:02x}: type {device_type:04x}")
                    
                    # Track motor and other device ports
                    device_names = {
                        0x0029: "Duplo Train Motor",
                        0x0014: "Voltage Sensor",
                        0x002C: "Color & Distance Sensor", 
                        0x005A: "Duplo Train Base Speaker",
                        0x005B: "Duplo Train Base Light/LED"
                    }
                    
                    device_name = device_names.get(device_type, f"Unknown (0x{device_type:04x})")
                    logger.info(f"{device_name} found on port {port:02x}")
                    
                    if device_type == 0x0029:  # Duplo Train Motor
                        self.motor_port = port
                    elif device_type == 0x005B:  # LED Light
                        self.led_port = port
            elif msg_type == 0x05:  # Error message
                if len(data) >= 6:
                    cmd_type = data[3]
                    error_code = data[4]
                    logger.warning(f"Error response: cmd_type={cmd_type:02x}, error={error_code:02x}")
            elif msg_type == 0x82:  # Port Output Command Feedback
                if len(data) >= 5:
                    port = data[3]
                    feedback = data[4]
                    feedback_msgs = {
                        0x01: "Buffer Empty/Command In Progress",
                        0x05: "Command Discarded",
                        0x0A: "Command Completed",
                        0x10: "Idle"
                    }
                    feedback_msg = feedback_msgs.get(feedback, f"Unknown ({feedback:02x})")
                    logger.info(f"Motor feedback from port {port:02x}: {feedback_msg}")
            elif msg_type == 0x43:  # Port information response
                if len(data) >= 6:
                    port = data[3]
                    info_type = data[4]
                    if info_type == 0x01 and len(data) >= 11:  # Port capabilities
                        capabilities = data[5]
                        total_modes = data[6]
                        input_modes = data[7] | (data[8] << 8)
                        output_modes = data[9] | (data[10] << 8)
                        logger.info(f"Port {port:02x} capabilities: {total_modes} modes, cap={capabilities:02x}")
            elif msg_type == 0x44:  # Port mode information response
                if len(data) >= 6:
                    port = data[3]
                    mode = data[4]
                    info_type = data[5]
                    if info_type == 0x00:  # NAME
                        name = data[6:].decode('ascii', errors='ignore').rstrip('\x00')
                        logger.info(f"Port {port:02x} mode {mode}: {name}")
                    elif info_type == 0x80:  # VALUE FORMAT
                        if len(data) >= 11:
                            num_values = data[6]
                            data_type = data[7]
                            total_figures = data[8]
                            decimals = data[9]
                            logger.info(f"Port {port:02x} mode {mode} format: {num_values} values, type={data_type}")
            elif msg_type == 0x45:  # Port Value (Single)
                if len(data) >= 5:
                    port = data[3]
                    # The value data starts at byte 4, length depends on port
                    value_data = data[4:]
                    logger.debug(f"Port {port:02x} value: {value_data.hex()}")
    
    async def connect(self, device):
        """Connect to the train"""
        self.device = device
        self.client = BleakClient(device.address)
        
        try:
            await self.client.connect()
            logger.info(f"Connected to {device.name}")
            
            # Enable notifications
            await self.client.start_notify(CHARACTERISTIC_UUID, self.notification_handler)
            
            # Send hub properties request to activate the hub
            await self.activate_hub()
            
            return True
        except BleakError as e:
            logger.error(f"Failed to connect: {e}")
            return False
    
    async def activate_hub(self):
        """Send activation sequence to the hub"""
        # Just enable button notifications - don't query everything
        command = bytearray([
            MSG_HUB_PROPERTIES,  # Message type
            0x02,                # Property: Button
            0x02                 # Operation: Enable updates
        ])
        await self.send_command(command)
        await asyncio.sleep(0.5)
    
    async def query_port_information(self, port):
        """Query information about a specific port"""
        # Port Information Request
        command = bytearray([
            MSG_PORT_INFORMATION_REQUEST,  # Message type
            port,                          # Port ID
            0x01                          # Information Type: Port Value
        ])
        await self.send_command(command)
        await asyncio.sleep(0.1)
        
        # Also request mode combinations
        command = bytearray([
            MSG_PORT_INFORMATION_REQUEST,  # Message type  
            port,                          # Port ID
            0x02                          # Information Type: Mode Info
        ])
        await self.send_command(command)
        await asyncio.sleep(0.1)
    
    async def query_port_modes(self, port, mode=0):
        """Query mode information for a port"""
        # Request mode name
        command = bytearray([
            MSG_PORT_MODE_INFORMATION_REQUEST,  # Message type
            port,                               # Port ID
            mode,                               # Mode
            0x00                               # Information Type: NAME
        ])
        await self.send_command(command)
        await asyncio.sleep(0.1)
        
        # Request value format
        command = bytearray([
            MSG_PORT_MODE_INFORMATION_REQUEST,  # Message type
            port,                               # Port ID  
            mode,                               # Mode
            0x80                               # Information Type: VALUE FORMAT
        ])
        await self.send_command(command)
        await asyncio.sleep(0.1)
    
    async def disconnect(self):
        """Disconnect from the train"""
        if self.client and self.client.is_connected:
            await self.client.disconnect()
            logger.info("Disconnected from train")
    
    async def send_command(self, command):
        """Send a command to the train"""
        if not self.client or not self.client.is_connected:
            logger.error("Not connected to train")
            return False
        
        try:
            # Add message length header (excluding length bytes)
            length = len(command) + 2
            message = bytearray([length, 0x00]) + command
            
            logger.debug(f"Sending: {message.hex()}")
            await self.client.write_gatt_char(CHARACTERISTIC_UUID, message)
            return True
        except Exception as e:
            logger.error(f"Failed to send command: {e}")
            return False

    async def test2(self, speed):
        """Set motor speed (-100 to 100)"""
        speed = max(-100, min(100, speed))

        # Convert speed to signed byte
        if speed < 0:
            speed_byte = 256 + speed
        else:
            speed_byte = speed

        # Direct write to motor - simplified command
        command = bytearray([0x08, 0x00, 0x81, 0x01, 0x11, 0x51, 0x01, 0x09])

        await self.client.write_gatt_char(CHARACTERISTIC_UUID, command)
        logger.info(f"Set motor speed to {speed}")

    async def set_motor_speed(self, speed):
        """Set motor speed (-100 to 100)"""
        speed = max(-100, min(100, speed))
        
        # Convert speed to signed byte
        if speed < 0:
            speed_byte = (256 + speed) & 0xFF
        else:
            speed_byte = speed & 0xFF
        
        # Motor control command - Fixed to use actual speed_byte instead of hardcoded 0x32
        command = bytes([0x08, 0x00, 0x81, MOTOR_PORT, 0x01, 0x51, 0x00, speed_byte])
        logger.debug(f"Sending motor command: {command.hex()}")
        logger.debug(f"Speed requested: {speed}, speed_byte: 0x{speed_byte:02x}")
        await self.client.write_gatt_char(CHARACTERISTIC_UUID, command)
        logger.info(f"Set motor speed to {speed}")
    
    async def set_motor_simple(self, speed):
        """Simple motor control"""
        speed = max(-100, min(100, speed))
        
        # Convert to signed byte
        if speed < 0:
            speed_byte = (256 + speed) & 0xFF
        else:
            speed_byte = speed & 0xFF
            
        # Simple direct motor command
        command = bytearray([
            MSG_PORT_OUTPUT_COMMAND,  # 0x81
            MOTOR_PORT,               # 0x00
            0x01,                     # Startup info - must be 0x01 for Duplo
            0x51,                     # WriteDirectModeData
            0x00,                     # Mode
            speed_byte                # Speed
        ])
        
        await self.send_command(command)
        logger.info(f"Set motor speed (simple) to {speed}")
    
    async def stop(self):
        """Stop the train"""
        if self.client and self.client.is_connected:
            await self.set_motor_speed(0)
            logger.info("Train stopped")
        else:
            logger.debug("Train not connected - cannot stop")
    
    def evaluate_response(self, command_desc):
        """Evaluate the last notification response"""
        if not self.last_notification:
            return "No response received"
        
        data_hex = self.last_notification.hex()
        
        # Check for error responses
        if data_hex == "0500050106":
            return f"{command_desc} - ERROR: Invalid use (0x06)"
        elif data_hex == "0500050105":
            return f"{command_desc} - ERROR: Command not recognized (0x05)"
        elif data_hex == "0500050206":
            return f"{command_desc} - ERROR: Invalid use for Hub Action (0x06)"
        elif data_hex.startswith("050005"):
            # Generic error format
            if len(self.last_notification) >= 5:
                cmd_type = self.last_notification[3]
                error_code = self.last_notification[4]
                return f"{command_desc} - ERROR: Command type 0x{cmd_type:02x}, Error code 0x{error_code:02x}"
        elif data_hex.startswith("0500"):
            # Possible success or other message
            return f"{command_desc} - Response: {data_hex}"
        else:
            # Non-error response
            return f"{command_desc} - Success/Data: {data_hex}"
        
        return f"{command_desc} - Unknown response: {data_hex}"
    
    async def wait_for_response(self, timeout=0.3):
        """Wait for a response and clear the last notification"""
        self.last_notification = None
        await asyncio.sleep(timeout)
    
    async def play_sound(self, sound_id):
        """Play a sound on the train using Hub Actions"""
        # Sound is controlled via Hub Actions (0x02), not port commands
        # sound_id: 1-10 for different train sounds
        sound_id = max(1, min(10, sound_id))  # Clamp to valid range
        
        # Hub Action command: [length] [0x00] [0x02=HubAction] [0x01=PlaySound] [sound_id]
        command = bytes([0x04, 0x00, 0x02, 0x01, sound_id])
        
        logger.debug(f"Playing sound {sound_id}: {command.hex()}")
        await self.client.write_gatt_char(CHARACTERISTIC_UUID, command)
        logger.info(f"Played sound {sound_id}")
    
    async def set_light_color(self, color):
        """Set the light color (0-10)"""
        # Port 0x33 has device type 0x005B - Duplo LED
        # Try different approaches
        color = max(0, min(10, color))
        
        commands = [
            # Try with completion info like motor
            bytes([0x08, 0x00, 0x81, 0x33, 0x10, 0x51, 0x00, color]),
            # Try with different startup byte
            bytes([0x08, 0x00, 0x81, 0x33, 0x11, 0x51, 0x00, color]),
            # Try RGB mode (mode 1)
            bytes([0x08, 0x00, 0x81, 0x33, 0x11, 0x51, 0x01, color]),
        ]
        
        for i, command in enumerate(commands):
            logger.debug(f"Trying LED format {i}: {command.hex()}")
            await self.client.write_gatt_char(CHARACTERISTIC_UUID, command)
            await asyncio.sleep(0.5)

def load_working_commands():
    """Load confirmed working commands from file"""
    working_commands = set()
    try:
        with open("working_commands.list", "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    # Handle hex format
                    if len(line) == 18 and all(c in '0123456789abcdefABCDEF' for c in line):
                        try:
                            # Extract event and volume from hex command
                            # Format: 0900813411510107c8 where 07 is event, c8 is volume
                            event = int(line[14:16], 16)
                            volume = int(line[16:18], 16)
                            working_commands.add((event, volume))
                        except:
                            continue
                    # Handle old format (event,volume)
                    elif ',' in line:
                        try:
                            event, volume = line.split(',')
                            working_commands.add((int(event), int(volume)))
                        except:
                            continue
    except FileNotFoundError:
        pass
    return working_commands

async def interactive_control(controller):
    """Interactive control loop"""
    print("\nDuplo Train Control Commands:")
    print("\n=== WORKING COMMANDS ===")
    print("  f/F     - Forward (basic)")
    print("  b/B     - Backward (basic)")
    print("  F{0-255} - Forward with speed control (e.g., F50, F100)")
    print("  B{0-255} - Backward with speed control (e.g., B50, B100)")
    print("  C{0-24}  - Set LED color (e.g., C0=white, C10=green)")
    print("  S{0-255} - Play sound effect (e.g., S1, S10)")
    print("  E7       - Stop/brake")
    print("  s        - Stop (motor control)")
    print("  q        - Quit")
    print("\n=== EXPERIMENTAL ===")
    print("  1-5 - Play sound (testing)")
    print("  c   - Change LED color (testing)")
    print("\n=== DEBUG COMMANDS ===")
    print("  r   - Read color/distance sensor")
    print("  k   - Explore all Hub Action types")
    print("  2   - Test only Hub Action 0x02")
    print("  7   - Test only Hub Action 0x07")
    print("  Z   - Full port analysis (all ports including motor)")
    print("  V   - Read voltage sensor continuously (0x35)")
    print("  D   - Dual sensor monitoring (voltage + motion count)")
    print("  R   - Replay manual events with sensor monitoring")
    print("  W   - Test and verify working commands (combines files)")
    print("  Y   - Confirm working commands (Yes/No for each)")
    print("  P   - Play working commands (one by one with Enter)")
    print("  A   - Analyze working commands and test variations")
    print("  B   - Byte format testing (2-byte and 3-byte commands)")
    print("  N   - Next byte testing (3-byte with known working base)")
    print("  E0-E10 - Test specific event with 3-byte format")
    print("  T   - Test shutdown commands from file (one by one)")
    
    while True:
        try:
            cmd = input("\nEnter command: ").strip()
            
            if cmd == 'q':
                break
            elif cmd == 'f':
                # Forward using EVENTS mode (Event 1, Volume 1)
                cmd_bytes = bytes([0x09, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, 1, 1])
                await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                print("Forward")
            elif cmd == 'F':
                # Forward fast using EVENTS mode (Event 1, Volume 1)
                cmd_bytes = bytes([0x09, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, 1, 1])
                await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                print("Forward (fast)")
            elif cmd == 'b':
                # Backward using EVENTS mode (Event 2, Volume 1)
                cmd_bytes = bytes([0x09, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, 2, 1])
                await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                print("Backward")
            elif cmd == 'B':
                # Backward fast using EVENTS mode (Event 2, Volume 1)
                cmd_bytes = bytes([0x09, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, 2, 1])
                await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                print("Backward (fast)")
            elif cmd == 's':
                await controller.stop()
            elif cmd in '12345':
                sound_id = int(cmd)
                if sound_id in [1, 2]:
                    print(f"WARNING: Sound {sound_id} powers off the train!")
                    confirm = input("Continue? (y/n): ")
                    if confirm.lower() != 'y':
                        continue
                await controller.play_sound(sound_id)
            elif cmd == 'r':
                # Read color/distance sensor
                print("Reading color/distance sensor...")
                # Subscribe to port 0x36 (color/distance sensor)
                command = bytes([0x0A, 0x00, 0x41, 0x36, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01])
                await controller.client.write_gatt_char(CHARACTERISTIC_UUID, command)
                await asyncio.sleep(1)
                print("Check debug logs for sensor readings")
            elif cmd == 'c':
                color = input("Enter color (0-10): ")
                try:
                    await controller.set_light_color(int(color))
                except ValueError:
                    print("Invalid color value")
            elif cmd == 'k':
                print("Exploring Hub Action message types...")
                print("\nHub Action format: [0x04, 0x00, 0x02, action_type, value]")
                print("\nTesting different action types (0x00-0xFF):")
                
                # Test ranges of action types
                test_ranges = [
                    (0x00, 0x10, "System actions"),
                    (0x30, 0x40, "Mid-range actions"),
                    (0x50, 0x60, "Sound range"),
                    (0x80, 0x90, "High range")
                ]
                
                for start, end, description in test_ranges:
                    print(f"\n{description} (0x{start:02x}-0x{end:02x}):")
                    for action_type in range(start, end):
                        # Skip 0x01, 0x02, and 0x07 which power off or reset the train
                        if action_type in [0x01, 0x02, 0x07]:
                            print(f"\nSkipping action type 0x{action_type:02x} (powers off/resets)")
                            continue
                            
                        print(f"\nTesting action type 0x{action_type:02x}:")
                        
                        # Try a few different values for each action type
                        for value in [0x03, 0x05, 0x0A]:
                            cmd_bytes = bytes([0x04, 0x00, 0x02, action_type, value])
                            logger.debug(f"Hub Action: {cmd_bytes.hex()}")
                            
                            try:
                                await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                                await asyncio.sleep(0.3)
                            except Exception as e:
                                logger.error(f"Error with action 0x{action_type:02x}: {e}")
                                break
                        
                        result = input("Any effect? (sound/led/motor/power/none/skip): ")
                        if result == "skip":
                            break
                        if result != "none":
                            print(f"✓ Action type 0x{action_type:02x}: {result}")
                
                print("\nAlso trying Hub Properties (0x01) for LED control:")
                # Hub Properties that might control LED
                led_properties = [
                    (0x03, "Name"),
                    (0x08, "LED/Light"),
                    (0x0C, "RGB LED"),
                    (0x0D, "LED Pattern"),
                    (0x0E, "LED Intensity")
                ]
                
                for prop, name in led_properties:
                    print(f"\nTesting property 0x{prop:02x} ({name}):")
                    # Try setting property
                    for value in [0x00, 0x03, 0x05, 0x09, 0x0A]:
                        cmd_bytes = bytes([0x05, 0x00, 0x01, prop, 0x01, value])
                        logger.debug(f"Hub Property Set: {cmd_bytes.hex()}")
                        await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                        await asyncio.sleep(0.3)
                    
                    led_changed = input("Did LED change? (y/n): ")
                    if led_changed.lower() == 'y':
                        print(f"✓ LED controlled by property 0x{prop:02x}")
            elif cmd == '2':
                print("Testing Hub Action 0x02 with different values...")
                print("WARNING: This action may power off the train!")
                confirm = input("Continue? (y/n): ")
                if confirm.lower() == 'y':
                    for value in range(0, 11):
                        print(f"\nTesting action 0x02 with value {value}...")
                        cmd_bytes = bytes([0x04, 0x00, 0x02, 0x02, value])
                        logger.debug(f"Hub Action 0x02: {cmd_bytes.hex()}")
                        await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                        await asyncio.sleep(1)
                        effect = input("Effect? (power_off/sound/led/none): ")
                        if effect == "power_off":
                            print("Train powered off. You may need to reconnect.")
                            break
            elif cmd == 'Z':
                print("Full port analysis - querying all discovered ports...")
                print("\nThis will query:")
                print("- Port capabilities and total modes")
                print("- Mode names and value formats")
                print("- Input/output capabilities\n")
                
                # All known ports including motor
                all_ports = {
                    0x00: "Motor (Port A)",
                    0x32: "Motor (Alternate)",
                    0x33: "LED Light",
                    0x34: "Speaker/Sound",
                    0x35: "Voltage Sensor",
                    0x36: "Color/Distance Sensor"
                }
                
                for port, name in all_ports.items():
                    print(f"\n{'='*50}")
                    print(f"Analyzing Port 0x{port:02x} - {name}")
                    print(f"{'='*50}")
                    
                    # 1. Query port capabilities
                    print("\n1. Querying port capabilities...")
                    cmd_bytes = bytes([0x05, 0x00, 0x21, port, 0x01])  # Port Value
                    logger.debug(f"Port info request: {cmd_bytes.hex()}")
                    await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                    await asyncio.sleep(0.3)
                    
                    # 2. Query port mode combinations
                    print("2. Querying mode combinations...")
                    cmd_bytes = bytes([0x05, 0x00, 0x21, port, 0x02])  # Mode combinations
                    logger.debug(f"Mode combinations: {cmd_bytes.hex()}")
                    await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                    await asyncio.sleep(0.3)
                    
                    # 3. Query each mode's details (up to 8 modes typical)
                    print("3. Querying mode details...")
                    for mode in range(8):
                        # Get mode name
                        cmd_bytes = bytes([0x06, 0x00, 0x22, port, mode, 0x00])
                        logger.debug(f"Mode {mode} name: {cmd_bytes.hex()}")
                        await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                        await asyncio.sleep(0.2)
                        
                        # Get value format
                        cmd_bytes = bytes([0x06, 0x00, 0x22, port, mode, 0x80])
                        logger.debug(f"Mode {mode} format: {cmd_bytes.hex()}")
                        await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                        await asyncio.sleep(0.2)
                        
                        # Get RAW range
                        cmd_bytes = bytes([0x06, 0x00, 0x22, port, mode, 0x01])
                        logger.debug(f"Mode {mode} RAW range: {cmd_bytes.hex()}")
                        await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                        await asyncio.sleep(0.2)
                        
                        # Get SI range  
                        cmd_bytes = bytes([0x06, 0x00, 0x22, port, mode, 0x03])
                        logger.debug(f"Mode {mode} SI range: {cmd_bytes.hex()}")
                        await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                        await asyncio.sleep(0.2)
                    
                    print(f"\nCompleted analysis for port 0x{port:02x}")
                    print("Check debug logs for detailed responses!")
                    
                    # Pause between ports
                    if port != 0x36:  # Not the last port
                        input("\nPress Enter to continue to next port...")
                
                print("\n\nFull port analysis complete!")
                print("\nKey things to look for in the debug logs:")
                print("- Message type 0x43: Port information (capabilities, modes)")
                print("- Message type 0x44: Port mode information (names, formats)")
                print("- Error 0x05/0x06: Invalid port or mode")
                print("\nWorking ports will return detailed information.")
                print("Non-existent ports will return errors.")
            elif cmd == 'V':
                print("Reading voltage sensor (Port 0x35) - Press Ctrl+C to stop")
                print("\nSubscribing to voltage updates...")
                
                # Subscribe to voltage sensor Mode 0 (VLT L)
                # Format: [0x41=PortInputFormatSetupSingle, port, mode, delta, notification_enabled]
                cmd_bytes = bytes([0x0A, 0x00, 0x41, 0x35, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01])
                logger.debug(f"Subscribe to voltage: {cmd_bytes.hex()}")
                await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                
                print("\nVoltage readings will appear in debug log.")
                print("Look for notifications with port 0x35")
                print("Values are in millivolts (mV)")
                print("\nReading for 30 seconds...")
                
                try:
                    await asyncio.sleep(30)
                except KeyboardInterrupt:
                    pass
                
                # Unsubscribe
                print("\nUnsubscribing from voltage updates...")
                cmd_bytes = bytes([0x0A, 0x00, 0x41, 0x35, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00])
                await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                
            elif cmd == 'C':
                print("Reading color/motion sensor (Port 0x36) - Press Ctrl+C to stop")
                print("\nSelect mode:")
                print("0 - SPEED (motion detection)")
                print("1 - COUNT (event counter)")
                print("2 - VELO (velocity)")
                
                mode_input = input("Enter mode (0-2): ").strip()
                try:
                    mode = int(mode_input)
                    if mode not in [0, 1, 2]:
                        print("Invalid mode")
                        continue
                except ValueError:
                    print("Invalid input")
                    continue
                
                mode_names = ["SPEED", "COUNT", "VELO"]
                print(f"\nSubscribing to {mode_names[mode]} updates...")
                
                # Subscribe to selected mode
                cmd_bytes = bytes([0x0A, 0x00, 0x41, 0x36, mode, 0x01, 0x00, 0x00, 0x00, 0x01])
                logger.debug(f"Subscribe to port 0x36 mode {mode}: {cmd_bytes.hex()}")
                await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                
                print(f"\n{mode_names[mode]} readings will appear in debug log.")
                print("Look for notifications with port 0x36")
                print("\nReading for 30 seconds...")
                print("Try moving something in front of the sensor!")
                
                try:
                    await asyncio.sleep(30)
                except KeyboardInterrupt:
                    pass
                
                # Unsubscribe
                print("\nUnsubscribing from sensor updates...")
                cmd_bytes = bytes([0x0A, 0x00, 0x41, 0x36, mode, 0x01, 0x00, 0x00, 0x00, 0x00])
                await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                
            elif cmd == 'D':
                print("Dual Sensor Monitoring - EVENTS Mode Test")
                print("=" * 60)
                print("\nMonitoring both voltage and motion count sensors")
                print("Testing all 256x256 EVENTS combinations")
                
                # Load working commands to skip
                skip_commands = load_working_commands()
                if skip_commands:
                    print(f"Skipping {len(skip_commands)} confirmed working commands")
                    total_tests = 256 * 256 - len(skip_commands)
                else:
                    total_tests = 256 * 256
                
                print(f"Will test {total_tests} commands")
                print("This will take approximately 20-30 minutes\n")
                
                # Initialize tracking with thread-safe collections
                controller.voltage_baseline = None
                controller.count_baseline = None
                controller.last_5_commands = []
                controller.last_200_commands = []  # For spacebar tracking (increased to 200)
                controller.command_lock = threading.Lock()
                voltage_changes = set()
                motion_changes = set()
                manual_events = []  # Commands when spacebar pressed
                changes_lock = threading.Lock()
                original_handler = controller.notification_handler  # Save original handler
                
                # Enhanced notification handler
                notification_count = 0
                def enhanced_handler(sender, data):
                    nonlocal notification_count
                    notification_count += 1
                    controller.last_notification = data
                    hex_data = data.hex()
                    
                    # Skip the motor feedback messages
                    if hex_data.startswith("050082340a"):
                        return
                    
                    # Debug first 10 non-motor notifications
                    if notification_count <= 10:
                        print(f"\nDEBUG [{notification_count}]: {hex_data}")
                    
                    # Check message type properly
                    if len(data) >= 3:
                        msg_type = data[2]
                        if msg_type == 0x45:  # Port Value Single
                            port = data[3]
                            if port == 0x35 and len(data) >= 6:  # Voltage
                                voltage = int.from_bytes(data[4:6], 'little')
                                if controller.voltage_baseline is None:
                                    controller.voltage_baseline = voltage
                                    print(f"\nVoltage baseline set: {voltage}mV")
                                elif abs(voltage - controller.voltage_baseline) > 100:  # 100mV threshold
                                    print(f"\n>>> Voltage change: {voltage}mV (baseline: {controller.voltage_baseline}mV)")
                                    # Add last 5 commands with thread safety
                                    with controller.command_lock:
                                        commands_copy = controller.last_5_commands.copy()
                                    with changes_lock:
                                        for cmd in commands_copy:
                                            voltage_changes.add(cmd)
                                            
                            elif port == 0x36 and len(data) >= 8:  # Motion count
                                count = int.from_bytes(data[4:8], 'little', signed=True)
                                if controller.count_baseline is None:
                                    controller.count_baseline = count
                                    print(f"\nMotion baseline set: {count}")
                                elif count != controller.count_baseline:
                                    print(f"\n>>> Motion detected: count {controller.count_baseline} -> {count}")
                                    # Add last 5 commands with thread safety
                                    with controller.command_lock:
                                        commands_copy = controller.last_5_commands.copy()
                                    with changes_lock:
                                        for cmd in commands_copy:
                                            motion_changes.add(cmd)
                                    controller.count_baseline = count
                
                # Replace handler FIRST
                await controller.client.stop_notify(CHARACTERISTIC_UUID)
                await controller.client.start_notify(CHARACTERISTIC_UUID, enhanced_handler)
                await asyncio.sleep(0.5)
                
                # NOW subscribe to sensors with the new handler active
                print("Subscribing to sensors...")
                # Voltage sensor (port 0x35, mode 0)
                cmd_bytes = bytes([0x0A, 0x00, 0x41, 0x35, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01])
                await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                await asyncio.sleep(0.3)
                
                # Motion count sensor (port 0x36, mode 1 - COUNT)
                cmd_bytes = bytes([0x0A, 0x00, 0x41, 0x36, 0x01, 0x01, 0x00, 0x00, 0x00, 0x01])
                await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                await asyncio.sleep(0.5)
                
                # Get baseline readings
                print("Establishing baseline readings...")
                await asyncio.sleep(2)
                
                print("\nTesting EVENTS combinations...")
                print("Press SPACEBAR to record interesting events")
                print("Progress: 0.0%", end="", flush=True)
                
                test_count = 0
                disconnected = False
                shutdown_commands = []
                
                # Set stdin to non-blocking mode
                old_settings = termios.tcgetattr(sys.stdin)
                try:
                    tty.setcbreak(sys.stdin.fileno())
                    
                    # Test in smaller batches to allow sensor processing
                    for event in range(256):
                        for volume in range(256):
                            if disconnected:
                                break
                                
                            # Skip confirmed working commands
                            cmd_tuple = (event, volume)
                            if cmd_tuple in skip_commands:
                                continue
                            with controller.command_lock:
                                controller.last_5_commands.append(cmd_tuple)
                                if len(controller.last_5_commands) > 5:
                                    controller.last_5_commands.pop(0)
                                controller.last_200_commands.append(cmd_tuple)
                                if len(controller.last_200_commands) > 200:
                                    controller.last_200_commands.pop(0)
                            
                            # Check for spacebar press (non-blocking)
                            if select.select([sys.stdin], [], [], 0)[0]:
                                key = sys.stdin.read(1)
                                if key == ' ':
                                    print(f"\n>>> MANUAL EVENT recorded at Event={event}, Volume={volume}")
                                    with controller.command_lock:
                                        manual_events.append(controller.last_200_commands.copy())
                            
                            # Send EVENTS command
                            try:
                                cmd_bytes = bytes([0x09, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, event, volume])
                                await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes, response=False)
                            except Exception as e:
                                print(f"\n>>> DISCONNECTION detected at Event={event}, Volume={volume}")
                                print(f"Error: {e}")
                                disconnected = True
                                # Save last 20 commands before disconnection
                                with controller.command_lock:
                                    shutdown_commands = controller.last_200_commands[-20:]
                                break
                            
                            # Give time for sensor readings and BLE notifications
                            await asyncio.sleep(0.02)  # Increased delay
                            
                            test_count += 1
                            if test_count % 500 == 0:
                                # Longer pause every 500 commands to ensure processing
                                await asyncio.sleep(0.5)
                                progress = (test_count / total_tests) * 100
                                print(f"\rProgress: {progress:.1f}%", end="", flush=True)
                        
                        if disconnected:
                            break
                            
                finally:
                    # Restore terminal settings
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                
                print("\rProgress: 100.0%")
                
                # Only try to restore handler and unsubscribe if still connected
                if controller.client and controller.client.is_connected:
                    try:
                        # Restore original handler
                        await controller.client.stop_notify(CHARACTERISTIC_UUID)
                        await controller.client.start_notify(CHARACTERISTIC_UUID, original_handler)
                        
                        # Unsubscribe from sensors
                        print("\nUnsubscribing from sensors...")
                        cmd_bytes = bytes([0x0A, 0x00, 0x41, 0x35, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00])
                        await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                        cmd_bytes = bytes([0x0A, 0x00, 0x41, 0x36, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00])
                        await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                    except Exception as e:
                        print(f"\nError during cleanup: {e}")
                else:
                    print("\nTrain disconnected - skipping cleanup")
                
                # Show results
                print("\n" + "=" * 60)
                print("RESULTS")
                print("=" * 60)
                
                if voltage_changes:
                    print(f"\nCommands that caused VOLTAGE changes ({len(voltage_changes)} unique):")
                    for event, volume in sorted(voltage_changes):
                        print(f"  Event={event}, Volume={volume}")
                        print(f"    bytes([0x09, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, 0x{event:02x}, 0x{volume:02x}])")
                
                if motion_changes:
                    print(f"\nCommands that caused MOTION ({len(motion_changes)} unique):")
                    for event, volume in sorted(motion_changes):
                        print(f"  Event={event}, Volume={volume}")
                        print(f"    bytes([0x09, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, 0x{event:02x}, 0x{volume:02x}])")
                
                # Commands in both
                both = voltage_changes.intersection(motion_changes)
                if both:
                    print(f"\nCommands that caused BOTH voltage and motion changes ({len(both)}):")
                    for event, volume in sorted(both):
                        print(f"  Event={event}, Volume={volume}")
                
                # Manual events (spacebar pressed)
                if manual_events:
                    print(f"\nMANUAL EVENTS recorded ({len(manual_events)} events):")
                    for i, event_list in enumerate(manual_events):
                        print(f"\n  Manual Event {i+1} - Last 200 commands (showing last 20):")
                        for j, (event, volume) in enumerate(event_list[-20:]):  # Show last 20 of each
                            print(f"    [{j+1}] Event={event}, Volume={volume}")
                    
                    # Save manual events to file
                    try:
                        with open("manual_events.txt", "w") as f:
                            f.write("# Manual events - spacebar pressed during interesting commands\n")
                            for i, event_list in enumerate(manual_events):
                                f.write(f"\n# Manual Event {i+1} - Last 200 commands\n")
                                for event, volume in event_list:
                                    f.write(f"{event},{volume}\n")
                        print("\nManual events saved to manual_events.txt")
                    except Exception as e:
                        print(f"\nError saving manual events: {e}")
                
                # Shutdown commands
                if shutdown_commands:
                    print(f"\nSHUTDOWN COMMANDS (last 20 before disconnection):")
                    for i, (event, volume) in enumerate(shutdown_commands):
                        print(f"  [{i+1}] Event={event}, Volume={volume}")
                        print(f"       bytes([0x09, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, 0x{event:02x}, 0x{volume:02x}])")
                    
                # Always save shutdown commands to file if we have them
                if shutdown_commands:
                    try:
                        with open("shutdown_commands.txt", "w") as f:
                            f.write("# Shutdown commands - last 20 before disconnection\n")
                            for i, (event, volume) in enumerate(shutdown_commands):
                                f.write(f"{event},{volume}\n")
                        print("\nShutdown commands saved to shutdown_commands.txt")
                    except Exception as e:
                        print(f"\nError saving shutdown commands: {e}")
                
                if not voltage_changes and not motion_changes and not manual_events and not shutdown_commands:
                    print("\nNo sensor changes detected. Commands might only affect sound/LED.")
                
            elif cmd == 'R':
                print("Replay Manual Events with Sensor Monitoring")
                print("=" * 60)
                print("\nThis tests events from manual_events.txt with sensor monitoring")
                print("Press SPACEBAR to mark interesting commands (saves ±5 commands)")
                print("Testing at 10 commands per second (0.1s delay)\n")
                
                try:
                    # Read events from file
                    with open("manual_events.txt", "r") as f:
                        lines = f.readlines()
                    
                    # Parse all event,volume pairs
                    all_events = []
                    for line in lines:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            try:
                                event, volume = line.split(",")
                                all_events.append((int(event), int(volume)))
                            except:
                                continue
                    
                    # Remove duplicates while preserving order
                    seen = set()
                    unique_events = []
                    for ev in all_events:
                        if ev not in seen:
                            seen.add(ev)
                            unique_events.append(ev)
                    
                    # Load and skip working commands
                    skip_commands = load_working_commands()
                    if skip_commands:
                        print(f"Loaded {len(skip_commands)} working commands to skip")
                        unique_events = [ev for ev in unique_events if ev not in skip_commands]
                    
                    if not unique_events:
                        print("No valid events found in manual_events.txt (after skipping working commands)")
                        print("Run option 'D' first and press spacebar during interesting events")
                        continue
                    
                    print(f"Found {len(unique_events)} unique events to test")
                    confirm = input("\nProceed? (y/n): ")
                    
                    if confirm.lower() == 'y':
                        # Initialize tracking
                        controller.voltage_baseline = None
                        controller.count_baseline = None
                        controller.command_history = []  # All commands tested
                        controller.command_lock = threading.Lock()
                        interesting_ranges = set()  # Store ranges of interesting commands
                        voltage_changes = set()
                        motion_changes = set()
                        changes_lock = threading.Lock()
                        original_handler = controller.notification_handler
                        
                        # Enhanced notification handler
                        def enhanced_handler(sender, data):
                            controller.last_notification = data
                            hex_data = data.hex()
                            
                            if hex_data.startswith("050082340a"):
                                return
                            
                            if len(data) >= 3:
                                msg_type = data[2]
                                if msg_type == 0x45:  # Port Value Single
                                    port = data[3]
                                    if port == 0x35 and len(data) >= 6:  # Voltage
                                        voltage = int.from_bytes(data[4:6], 'little')
                                        if controller.voltage_baseline is None:
                                            controller.voltage_baseline = voltage
                                            print(f"\nVoltage baseline: {voltage}mV")
                                        elif abs(voltage - controller.voltage_baseline) > 100:
                                            print(f"\n>>> Voltage change: {voltage}mV (delta: {voltage - controller.voltage_baseline:+d})")
                                            with controller.command_lock:
                                                current_idx = len(controller.command_history) - 1
                                            with changes_lock:
                                                if current_idx >= 0:
                                                    voltage_changes.add(controller.command_history[current_idx])
                                                    
                                    elif port == 0x36 and len(data) >= 8:  # Motion count
                                        count = int.from_bytes(data[4:8], 'little', signed=True)
                                        if controller.count_baseline is None:
                                            controller.count_baseline = count
                                            print(f"\nMotion baseline: {count}")
                                        elif count != controller.count_baseline:
                                            print(f"\n>>> Motion detected: {controller.count_baseline} -> {count}")
                                            with controller.command_lock:
                                                current_idx = len(controller.command_history) - 1
                                            with changes_lock:
                                                if current_idx >= 0:
                                                    motion_changes.add(controller.command_history[current_idx])
                                            controller.count_baseline = count
                        
                        # Replace handler and subscribe to sensors
                        await controller.client.stop_notify(CHARACTERISTIC_UUID)
                        await controller.client.start_notify(CHARACTERISTIC_UUID, enhanced_handler)
                        await asyncio.sleep(0.5)
                        
                        print("Subscribing to sensors...")
                        # Voltage sensor
                        await controller.client.write_gatt_char(
                            CHARACTERISTIC_UUID,
                            bytes([0x0A, 0x00, 0x41, 0x35, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01])
                        )
                        await asyncio.sleep(0.3)
                        
                        # Motion count sensor
                        await controller.client.write_gatt_char(
                            CHARACTERISTIC_UUID,
                            bytes([0x0A, 0x00, 0x41, 0x36, 0x01, 0x01, 0x00, 0x00, 0x00, 0x01])
                        )
                        await asyncio.sleep(0.5)
                        
                        print("Establishing baselines...")
                        await asyncio.sleep(2)
                        
                        print("\nTesting events from manual_events.txt...")
                        print("Press SPACEBAR to mark interesting events\n")
                        
                        # Set stdin to non-blocking mode
                        old_settings = termios.tcgetattr(sys.stdin)
                        disconnected = False
                        
                        try:
                            tty.setcbreak(sys.stdin.fileno())
                            
                            for i, (event, volume) in enumerate(unique_events):
                                if disconnected:
                                    break
                                
                                # Add to history
                                with controller.command_lock:
                                    controller.command_history.append((event, volume))
                                
                                print(f"[{i+1}/{len(unique_events)}] Testing Event={event}, Volume={volume}...", end="", flush=True)
                                
                                # Check for spacebar (non-blocking)
                                if select.select([sys.stdin], [], [], 0)[0]:
                                    key = sys.stdin.read(1)
                                    if key == ' ':
                                        print("\n>>> MARKED as interesting!")
                                        with controller.command_lock:
                                            current_idx = len(controller.command_history) - 1
                                            # Add range from -5 to +5 (we'll catch the next 5 as we go)
                                            start_idx = max(0, current_idx - 5)
                                            for j in range(start_idx, min(current_idx + 6, len(unique_events))):
                                                if j < len(controller.command_history):
                                                    interesting_ranges.add(j)
                                
                                # Send command
                                try:
                                    cmd_bytes = bytes([0x09, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, event, volume])
                                    await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes, response=False)
                                except Exception as e:
                                    print(f"\n>>> DISCONNECTION at Event={event}, Volume={volume}")
                                    print(f"Error: {e}")
                                    disconnected = True
                                    break
                                
                                # 0.1 second delay (10 commands per second)
                                await asyncio.sleep(0.1)
                                
                                # Add to interesting range if within 5 commands of a marked event
                                with controller.command_lock:
                                    current_idx = len(controller.command_history) - 1
                                    for marked_idx in list(interesting_ranges):
                                        if marked_idx <= current_idx <= marked_idx + 5:
                                            interesting_ranges.add(current_idx)
                                
                                print(" done")
                                
                        finally:
                            # Restore terminal settings
                            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                        
                        # Cleanup
                        if controller.client and controller.client.is_connected:
                            try:
                                await controller.client.stop_notify(CHARACTERISTIC_UUID)
                                await controller.client.start_notify(CHARACTERISTIC_UUID, original_handler)
                                
                                # Unsubscribe
                                await controller.client.write_gatt_char(
                                    CHARACTERISTIC_UUID,
                                    bytes([0x0A, 0x00, 0x41, 0x35, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00])
                                )
                                await controller.client.write_gatt_char(
                                    CHARACTERISTIC_UUID,
                                    bytes([0x0A, 0x00, 0x41, 0x36, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00])
                                )
                            except Exception as e:
                                print(f"\nCleanup error: {e}")
                        
                        # Show results
                        print("\n" + "=" * 60)
                        print("RESULTS")
                        print("=" * 60)
                        
                        # Get interesting commands
                        interesting_commands = []
                        with controller.command_lock:
                            for idx in sorted(interesting_ranges):
                                if idx < len(controller.command_history):
                                    interesting_commands.append(controller.command_history[idx])
                        
                        if interesting_commands:
                            print(f"\nINTERESTING COMMANDS (spacebar pressed ±5):")
                            seen_interesting = set()
                            for event, volume in interesting_commands:
                                if (event, volume) not in seen_interesting:
                                    seen_interesting.add((event, volume))
                                    print(f"  Event={event}, Volume={volume}")
                                    if (event, volume) in voltage_changes:
                                        print("    -> Caused voltage change")
                                    if (event, volume) in motion_changes:
                                        print("    -> Caused motion")
                            
                            # Save to file
                            try:
                                with open("interesting_commands.txt", "w") as f:
                                    f.write("# Interesting commands marked during replay\n")
                                    for event, volume in sorted(seen_interesting):
                                        f.write(f"{event},{volume}\n")
                                print("\nInteresting commands saved to interesting_commands.txt")
                            except Exception as e:
                                print(f"\nError saving interesting commands: {e}")
                        
                        if voltage_changes:
                            print(f"\nCommands causing VOLTAGE changes ({len(voltage_changes)}):")
                            for event, volume in sorted(voltage_changes):
                                print(f"  Event={event}, Volume={volume}")
                        
                        if motion_changes:
                            print(f"\nCommands causing MOTION ({len(motion_changes)}):")
                            for event, volume in sorted(motion_changes):
                                print(f"  Event={event}, Volume={volume}")
                        
                        if disconnected:
                            print(f"\nDISCONNECTION occurred - check shutdown_commands.txt for the problematic command")
                
                except FileNotFoundError:
                    print("Error: manual_events.txt not found")
                    print("Run option 'D' first and press spacebar during interesting events")
                except Exception as e:
                    print(f"Error: {e}")
                    
            elif cmd == '7':
                print("Testing Hub Action 0x07 with different values...")
                print("WARNING: This action may reset/power off the train!")
                confirm = input("Continue? (y/n): ")
                if confirm.lower() == 'y':
                    for value in range(0, 11):
                        print(f"\nTesting action 0x07 with value {value}...")
                        cmd_bytes = bytes([0x04, 0x00, 0x02, 0x07, value])
                        logger.debug(f"Hub Action 0x07: {cmd_bytes.hex()}")
                        await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                        await asyncio.sleep(1)
                        effect = input("Effect? (reset/power_off/sound/led/none): ")
                        if effect in ["reset", "power_off"]:
                            print("Train reset/powered off. You may need to reconnect.")
                            break
            elif cmd == 'W':
                print("Batch Test Working Commands")
                print("=" * 50)
                print("\nThis combines interesting commands from multiple files")
                print("Tests commands in batches of 10 before asking for feedback")
                print("Saves possibly working commands with full byte sequences\n")
                
                # Read commands from multiple files
                all_commands = []
                files_to_read = ["interesting_commands.txt", "interesting_commands2.txt"]
                
                for filename in files_to_read:
                    try:
                        with open(filename, "r") as f:
                            lines = f.readlines()
                            for line in lines:
                                line = line.strip()
                                if line and not line.startswith("#"):
                                    try:
                                        event, volume = line.split(",")
                                        all_commands.append((int(event), int(volume)))
                                    except:
                                        continue
                            print(f"Loaded commands from {filename}")
                    except FileNotFoundError:
                        print(f"File {filename} not found - skipping")
                    except Exception as e:
                        print(f"Error reading {filename}: {e}")
                
                # Remove duplicates while preserving order
                seen = set()
                unique_commands = []
                for cmd in all_commands:
                    if cmd not in seen:
                        seen.add(cmd)
                        unique_commands.append(cmd)
                
                # Load and skip working commands
                skip_commands = load_working_commands()
                if skip_commands:
                    print(f"Loaded {len(skip_commands)} working commands to skip")
                    unique_commands = [cmd for cmd in unique_commands if cmd not in skip_commands]
                
                if not unique_commands:
                    print("\nNo commands found in any file (after skipping working commands)")
                    continue
                
                print(f"\nFound {len(unique_commands)} unique commands to test")
                print(f"Will test in batches of 10 commands")
                confirm = input("Proceed? (y/n): ")
                
                if confirm.lower() == 'y':
                    possibly_working = []
                    batch_size = 10
                    
                    # Process in batches
                    for batch_start in range(0, len(unique_commands), batch_size):
                        batch_end = min(batch_start + batch_size, len(unique_commands))
                        batch = unique_commands[batch_start:batch_end]
                        
                        print(f"\n{'='*50}")
                        print(f"Testing batch {batch_start//batch_size + 1} (commands {batch_start+1}-{batch_end} of {len(unique_commands)})")
                        print(f"{'='*50}\n")
                        
                        batch_commands = []
                        disconnected = False
                        
                        for i, (event, volume) in enumerate(batch):
                            if disconnected:
                                break
                                
                            print(f"[{batch_start+i+1}/{len(unique_commands)}] Testing Event={event}, Volume={volume}...")
                            
                            # Build and store the command
                            cmd_bytes = bytes([0x09, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, event, volume])
                            batch_commands.append({
                                'event': event,
                                'volume': volume,
                                'bytes': cmd_bytes.hex(),
                                'index': batch_start + i + 1
                            })
                            
                            try:
                                # Send command
                                await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                                
                                # Short delay between commands
                                await asyncio.sleep(0.5)
                                
                                # Check if still connected
                                if not controller.client.is_connected:
                                    print(">>> TRAIN DISCONNECTED!")
                                    print(f">>> Disconnection at Event={event}, Volume={volume}")
                                    possibly_working.append({
                                        'event': event,
                                        'volume': volume,
                                        'bytes': cmd_bytes.hex(),
                                        'effect': 'shutdown/disconnect'
                                    })
                                    disconnected = True
                                    break
                                    
                            except Exception as e:
                                print(f"Error: {e}")
                                possibly_working.append({
                                    'event': event,
                                    'volume': volume,
                                    'bytes': cmd_bytes.hex(),
                                    'effect': 'error/possible shutdown'
                                })
                                disconnected = True
                                break
                        
                        if disconnected:
                            print("\nSkipping to save results due to disconnection...")
                            break
                        
                        # After batch, ask if any command worked
                        print(f"\n{'='*30}")
                        print("BATCH COMPLETE")
                        print(f"{'='*30}")
                        print(f"Tested {len(batch_commands)} commands")
                        
                        print("\nDid ANY of these commands have an effect on the train?")
                        print("(sound/movement/light/any effect)")
                        response = input("\nAny effects? (y/n): ").strip().lower()
                        
                        if response == 'y':
                            effect_desc = input("Describe the effect(s) (optional): ").strip()
                            # Save all commands from this batch
                            for cmd in batch_commands:
                                possibly_working.append({
                                    'event': cmd['event'],
                                    'volume': cmd['volume'],
                                    'bytes': cmd['bytes'],
                                    'effect': f"Batch {batch_start//batch_size + 1} - {effect_desc if effect_desc else 'effect observed'}"
                                })
                            print(f"✓ Saved all {len(batch_commands)} commands from this batch")
                        
                        # Pause between batches
                        if batch_end < len(unique_commands):
                            print(f"\nCompleted batch {batch_start//batch_size + 1}")
                            input("Press Enter to continue to next batch...")
                    
                    # Save possibly working commands
                    if possibly_working:
                        try:
                            import datetime
                            with open("possibly_working_commands.txt", "a") as f:  # Append mode
                                f.write("\n# Possibly working commands - batch tested " + 
                                       f"on {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                                for cmd in possibly_working:
                                    f.write(f"\n# Event={cmd['event']}, Volume={cmd['volume']}")
                                    if cmd['effect']:
                                        f.write(f" - Effect: {cmd['effect']}")
                                    f.write(f"\n{cmd['bytes']}\n")
                                    # Also write the command construction for reference
                                    f.write(f"# bytes([0x09, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, ")
                                    f.write(f"0x{cmd['event']:02x}, 0x{cmd['volume']:02x}])\n")
                            
                            print(f"\n✅ Saved {len(possibly_working)} possibly working commands to possibly_working_commands.txt")
                            print("\nPossibly working commands summary:")
                            for cmd in possibly_working:
                                print(f"  Event={cmd['event']}, Volume={cmd['volume']} - {cmd['effect']}")
                        except Exception as e:
                            print(f"\nError saving possibly working commands: {e}")
                    else:
                        print("\nNo working commands identified")
                    
                    print(f"\nTesting complete. Tested {len(unique_commands)} unique commands.")
                        
            elif cmd == 'P':
                print("Play Working Commands")
                print("=" * 50)
                print("\nThis sends each confirmed working command one by one")
                print("Press Enter to send the next command\n")
                
                try:
                    # Read working commands
                    with open("working_commands.list", "r") as f:
                        lines = f.readlines()
                    
                    commands = []
                    for line in lines:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            # Check if it's hex format (18 chars) or old format (event,volume)
                            if len(line) == 18 and all(c in '0123456789abcdefABCDEF' for c in line):
                                commands.append(line)
                            elif ',' in line:
                                # Old format - convert to hex
                                try:
                                    event, volume = line.split(',')
                                    event = int(event)
                                    volume = int(volume)
                                    cmd_bytes = bytes([0x09, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, event, volume])
                                    commands.append(cmd_bytes.hex())
                                    print(f"Converted old format: {event},{volume} -> {cmd_bytes.hex()}")
                                except:
                                    continue
                    
                    if not commands:
                        print("No valid commands found in working_commands.list")
                        print("File might be empty or in wrong format")
                        print("\nExpected format: hex commands like '0900813411510103c8'")
                        print("Run option 'Y' to create properly formatted commands")
                        continue
                    
                    print(f"Found {len(commands)} working commands\n")
                    
                    for i, hex_cmd in enumerate(commands):
                        # Extract event and volume for display
                        try:
                            event = int(hex_cmd[14:16], 16)
                            volume = int(hex_cmd[16:18], 16)
                            print(f"\n[{i+1}/{len(commands)}] Command: Event={event}, Volume={volume}")
                            print(f"Hex: {hex_cmd}")
                        except:
                            print(f"\n[{i+1}/{len(commands)}] Command: {hex_cmd}")
                        
                        input("\nPress Enter to send this command...")
                        
                        # Send command
                        try:
                            cmd_bytes = bytes.fromhex(hex_cmd)
                            await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                            print("✓ Command sent")
                            
                            # Check if still connected
                            if not controller.client.is_connected:
                                print("\n>>> TRAIN DISCONNECTED!")
                                break
                        except Exception as e:
                            print(f"✗ Error sending command: {e}")
                    
                    print("\nFinished playing all working commands")
                    
                except FileNotFoundError:
                    print("Error: working_commands.list not found")
                    print("Run option 'Y' first to confirm working commands")
                except Exception as e:
                    print(f"Error: {e}")
                    
            elif cmd == 'Y':
                print("Confirm Working Commands")
                print("=" * 50)
                print("\nThis tests each command from possibly_working_commands.txt")
                print("and confirms which ones actually work")
                print("Confirmed commands are saved to working_commands.list\n")
                
                try:
                    # Read possibly working commands
                    with open("possibly_working_commands.txt", "r") as f:
                        lines = f.readlines()
                    
                    # Parse unique commands
                    seen = set()
                    test_commands = []
                    for line in lines:
                        line = line.strip()
                        # Look for hex command lines
                        if line and not line.startswith("#") and len(line) == 18:  # hex command length
                            try:
                                # Extract event and volume from the hex string
                                # Format: 0900813411510107c8 where 07 is event, c8 is volume
                                event = int(line[14:16], 16)
                                volume = int(line[16:18], 16)
                                if (event, volume) not in seen:
                                    seen.add((event, volume))
                                    test_commands.append({
                                        'event': event,
                                        'volume': volume,
                                        'hex': line
                                    })
                            except:
                                continue
                    
                    if not test_commands:
                        print("No valid commands found in possibly_working_commands.txt")
                        print("Run option 'W' first to identify possibly working commands")
                        continue
                    
                    print(f"Found {len(test_commands)} unique commands to verify")
                    confirm = input("Proceed? (y/n): ")
                    
                    if confirm.lower() == 'y':
                        confirmed_working = []
                        
                        for i, cmd in enumerate(test_commands):
                            print(f"\n[{i+1}/{len(test_commands)}] Testing Event={cmd['event']}, Volume={cmd['volume']}")
                            print(f"Command: {cmd['hex']}")
                            
                            # Send command
                            cmd_bytes = bytes([0x09, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, cmd['event'], cmd['volume']])
                            await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                            
                            # Wait for effect
                            await asyncio.sleep(1.5)
                            
                            # Check if still connected
                            if not controller.client.is_connected:
                                print(">>> TRAIN DISCONNECTED!")
                                response = input("Was this the intended effect? (y/n): ")
                                if response.lower() == 'y':
                                    confirmed_working.append((cmd['event'], cmd['volume']))
                                break
                            
                            # Ask for confirmation
                            print("\nDid this command have the expected effect?")
                            response = input("Confirm working? (y/n): ").strip().lower()
                            
                            if response == 'y':
                                confirmed_working.append((cmd['event'], cmd['volume']))
                                print("✓ Confirmed as working")
                            else:
                                print("✗ Not working - skipped")
                        
                        # Save confirmed working commands
                        if confirmed_working:
                            try:
                                import datetime
                                with open("working_commands.list", "w") as f:
                                    f.write("# Confirmed working commands for Duplo train\n")
                                    f.write(f"# Generated on {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                                    f.write("#\n")
                                    f.write("# Format: full hex command (18 characters)\n")
                                    f.write("# Command structure: 0900813411510[event][volume]\n")
                                    f.write("#   09 = message length\n")
                                    f.write("#   00 = hub ID\n") 
                                    f.write("#   81 = port output command\n")
                                    f.write("#   34 = sound port\n")
                                    f.write("#   11 = startup/completion info\n")
                                    f.write("#   51 = write direct mode data\n")
                                    f.write("#   01 = EVENTS mode\n")
                                    f.write("#   [event] = event byte (hex)\n")
                                    f.write("#   [volume] = volume byte (hex)\n")
                                    f.write("#\n\n")
                                    
                                    for event, volume in confirmed_working:
                                        # Build the full command
                                        cmd_bytes = bytes([0x09, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, event, volume])
                                        f.write(f"# Event={event} (0x{event:02x}), Volume={volume} (0x{volume:02x})\n")
                                        f.write(f"{cmd_bytes.hex()}\n\n")
                                
                                print(f"\n✅ Saved {len(confirmed_working)} confirmed working commands to working_commands.list")
                                print("\nConfirmed commands:")
                                for event, volume in confirmed_working:
                                    print(f"  Event={event}, Volume={volume}")
                                print("\nThese commands will be skipped in future D, R, and W tests")
                            except Exception as e:
                                print(f"\nError saving working commands: {e}")
                        else:
                            print("\nNo commands were confirmed as working")
                
                except FileNotFoundError:
                    print("Error: possibly_working_commands.txt not found")
                    print("Run option 'W' first to identify possibly working commands")
                except Exception as e:
                    print(f"Error: {e}")
                    
            elif cmd == 'N':
                print("Next Byte Testing - 3-byte format with known working commands")
                print("=" * 50)
                print("\nTesting 3-byte format using known working base commands:")
                print("(1,1), (2,1), (4,1), (6,1), (7,1)")
                print("\nAdding a third byte (0-255) to each")
                
                working_base = [(1,1), (2,1), (4,1), (6,1), (7,1)]
                total_tests = len(working_base) * 256
                
                print(f"\nTotal combinations: {total_tests} (5 commands × 256 values)")
                print(f"Estimated time: ~{total_tests * 0.1 / 60:.1f} minutes at 0.1s per test")
                
                print("\nTest options:")
                print("1. Quick test - Common values (0, 1, 10, 50, 100, 127, 200, 255)")
                print("2. Full test - All 256 values")
                print("3. Cancel")
                
                choice = input("\nSelect option (1-3): ")
                
                if choice == '1':
                    test_values = [0, 1, 5, 10, 25, 50, 75, 100, 127, 150, 200, 255]
                    print(f"\nTesting with {len(test_values)} common values")
                elif choice == '2':
                    test_values = list(range(256))
                    print("\nTesting all 256 values")
                else:
                    continue
                
                results = []
                
                for event, volume in working_base:
                    print(f"\n\nTesting base command ({event},{volume}) with third byte:")
                    print("-" * 50)
                    effects_found = []
                    
                    # First show what the 2-byte version does
                    print(f"Reminder - 2-byte ({event},{volume}) effect: ", end="")
                    cmd_bytes = bytes([0x09, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, event, volume])
                    await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                    await asyncio.sleep(1)
                    base_effect = input("Describe base effect: ")
                    
                    print(f"\nNow testing 3-byte format:")
                    tested_count = 0
                    
                    for param in test_values:
                        # 3-byte command
                        cmd_bytes = bytes([0x0A, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, event, volume, param])
                        try:
                            await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                            await asyncio.sleep(0.1)
                            tested_count += 1
                            
                            # Show progress
                            if param in [0, 50, 100, 150, 200, 255]:
                                print(f"  Progress: param={param}")
                                
                        except Exception as e:
                            print(f"\n  Error at param {param}: {e}")
                            break
                    
                    print(f"\nTested {tested_count} values for ({event},{volume})")
                    response = input("Did the 3rd byte change the effect? (y/n): ")
                    
                    if response.lower() == 'y':
                        describe = input("Describe the changes (e.g., 'speed control', 'volume control'): ")
                        which_values = input("Which values had different effects? (comma-separated or 'all'): ")
                        
                        if which_values.lower() == 'all':
                            effects_found = [(param, describe) for param in test_values]
                        else:
                            try:
                                values = [int(v.strip()) for v in which_values.split(',')]
                                effects_found = [(v, describe) for v in values if v in test_values]
                            except:
                                effects_found = [(0, describe)]
                        
                        results.append({
                            'base': (event, volume),
                            'base_effect': base_effect,
                            'param_effects': effects_found
                        })
                        print(f"✓ Third byte DOES affect command ({event},{volume})")
                    else:
                        print(f"✗ Third byte has no effect on ({event},{volume})")
                
                # Show summary
                if results:
                    print("\n\n" + "=" * 60)
                    print("SUMMARY - Commands that accept 3rd byte:")
                    print("=" * 60)
                    
                    for r in results:
                        event, volume = r['base']
                        print(f"\nCommand ({event},{volume}) - {r['base_effect']}:")
                        print("  3rd byte effects:")
                        for param, effect in r['param_effects'][:5]:  # Show first 5
                            print(f"    Param {param}: {effect}")
                        if len(r['param_effects']) > 5:
                            print(f"    ... and {len(r['param_effects'])-5} more")
                    
                    # Save results
                    save = input("\nSave results to 3byte_commands.txt? (y/n): ")
                    if save.lower() == 'y':
                        with open("3byte_commands.txt", "w") as f:
                            f.write("# 3-byte command discoveries\n")
                            f.write("# Format: base_event,base_volume,param\n\n")
                            for r in results:
                                event, volume = r['base']
                                f.write(f"# Base: ({event},{volume}) - {r['base_effect']}\n")
                                for param, effect in r['param_effects']:
                                    f.write(f"{event},{volume},{param} # {effect}\n")
                                f.write("\n")
                        print("Results saved to 3byte_commands.txt")
                else:
                    print("\n\nNo 3-byte effects found. The commands might be strictly 2-byte format.")
                    
            elif cmd == 'B':
                print("Byte Format Testing - Events 0-10")
                print("=" * 50)
                print("\nTesting events 0-10 with 2-byte and 3-byte formats")
                print("Excluding known working: (1,1), (2,1), (4,1), (6,1), (7,1)")
                
                # Load working commands to skip
                skip_commands = {(1,1), (2,1), (4,1), (6,1), (7,1)}
                
                # Calculate total combinations
                events = list(range(11))  # 0-10
                volumes_2byte = list(range(256))  # 0-255 for 2-byte format
                params_3byte = list(range(256))  # 0-255 for 3rd byte
                
                # Count combinations
                total_2byte = len(events) * len(volumes_2byte) - len(skip_commands)
                total_3byte = len(events) * len(volumes_2byte) * len(params_3byte)
                total_combinations = total_2byte + total_3byte
                
                print(f"\nTotal combinations to test:")
                print(f"- 2-byte format: {total_2byte} commands (11 events × 256 volumes - 5 known)")
                print(f"- 3-byte format: {total_3byte} commands (11 events × 256 × 256)")
                print(f"- Total: {total_combinations:,} combinations")
                print(f"\nAt 0.1s per test: ~{total_combinations * 0.1 / 3600:.1f} hours")
                
                print("\nTest options:")
                print("1. Quick test - Common values only (~5 minutes)")
                print("2. Full 2-byte test only (~5 minutes)")
                print("3. Selected 3-byte test (~30 minutes)")
                print("4. Cancel")
                
                choice = input("\nSelect option (1-4): ")
                
                if choice == '1':
                    # Quick test with common values
                    print("\nQuick test - common values only")
                    test_volumes = [0, 1, 2, 5, 10, 50, 100, 128, 200, 255]
                    test_params = [0, 1, 50, 100, 255]
                    
                    working_commands = []
                    
                    # 2-byte format
                    print("\nTesting 2-byte format...")
                    for event in range(11):
                        for volume in test_volumes:
                            if (event, volume) in skip_commands:
                                continue
                            
                            cmd_bytes = bytes([0x09, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, event, volume])
                            await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                            await asyncio.sleep(0.1)
                        
                        # Ask after each event
                        print(f"\nEvent {event} tested with volumes: {test_volumes}")
                        response = input("Any effects? (y/n): ")
                        if response.lower() == 'y':
                            which = input("Which volumes worked? (comma-separated or 'all'): ")
                            if which == 'all':
                                for v in test_volumes:
                                    if (event, v) not in skip_commands:
                                        working_commands.append((event, v, "2-byte"))
                            else:
                                try:
                                    for v in which.split(','):
                                        vol = int(v.strip())
                                        if vol in test_volumes:
                                            working_commands.append((event, vol, "2-byte"))
                                except:
                                    pass
                    
                    # 3-byte format
                    print("\n\nTesting 3-byte format...")
                    for event in range(11):
                        print(f"\nTesting Event {event} with 3-byte format:")
                        for volume in [1, 50, 100, 255]:  # Fewer volumes for 3-byte
                            for param in test_params:
                                cmd_bytes = bytes([0x0A, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, event, volume, param])
                                try:
                                    await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                                    await asyncio.sleep(0.1)
                                except Exception as e:
                                    print(f"Error with 3-byte format: {e}")
                                    break
                        
                        response = input("Any effects with 3-byte format? (y/n): ")
                        if response.lower() == 'y':
                            details = input("Describe what worked: ")
                            working_commands.append((event, -1, f"3-byte: {details}"))
                    
                    # Save results
                    if working_commands:
                        print(f"\n\nFound {len(working_commands)} new working commands:")
                        for event, volume, format_type in working_commands:
                            if volume >= 0:
                                print(f"  Event={event}, Volume={volume} ({format_type})")
                            else:
                                print(f"  Event={event} ({format_type})")
                        
                        save = input("\nSave to new_working_commands.txt? (y/n): ")
                        if save.lower() == 'y':
                            with open("new_working_commands.txt", "w") as f:
                                f.write("# New working commands found\n")
                                for event, volume, format_type in working_commands:
                                    if volume >= 0:
                                        f.write(f"{event},{volume} # {format_type}\n")
                                    else:
                                        f.write(f"{event} # {format_type}\n")
                            print("Saved to new_working_commands.txt")
                
                elif choice == '2':
                    print("\nFull 2-byte test for events 0-10, all volumes 0-255")
                    print("Skipping known working commands")
                    
                    # Test in batches by event
                    for event in range(11):
                        print(f"\nTesting Event {event} with all volumes 0-255...")
                        has_effect = False
                        
                        for volume in range(256):
                            if (event, volume) in skip_commands:
                                continue
                            
                            cmd_bytes = bytes([0x09, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, event, volume])
                            await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                            await asyncio.sleep(0.05)  # Faster for full test
                            
                            if volume % 50 == 0:
                                print(f"  Progress: volume {volume}/255")
                        
                        response = input(f"\nDid Event {event} have any effects? (y/n): ")
                        if response.lower() == 'y':
                            print("Run option 'D' with this specific event range to find exact volumes")
                
            elif cmd == 'A':
                print("Analyze Working Commands")
                print("=" * 50)
                print("\nBased on working commands, testing variations")
                print("Working: 1,1 2,1 4,1 6,1 7,1")
                print("\nTesting theory: Event=function, Volume=1 might be enable/mode")
                
                # Test variations
                test_commands = []
                
                # 1. Fill in gaps with volume=1
                print("\n1. Testing missing events with volume=1:")
                for event in [0, 3, 5, 8, 9, 10]:
                    test_commands.append((event, 1, f"Gap filler"))
                
                # 2. Test working events with different volumes
                print("\n2. Testing working events with different volumes:")
                for event in [1, 2, 4, 6, 7]:
                    for volume in [0, 2, 10, 50, 100, 255]:
                        test_commands.append((event, volume, f"Event {event} volume variation"))
                
                # 3. Test if these could be multi-byte commands
                print("\n3. Could these commands accept additional parameters?")
                print("   Testing with longer messages...")
                
                confirm = input("\nProceed with testing? (y/n): ")
                if confirm.lower() == 'y':
                    print("\nTesting standard format commands:")
                    for event, volume, desc in test_commands[:20]:  # First 20 only
                        print(f"\nTesting {desc}: Event={event}, Volume={volume}")
                        cmd_bytes = bytes([0x09, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, event, volume])
                        await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                        await asyncio.sleep(1)
                        effect = input("Effect? (none/sound/move/light/other): ")
                        if effect != "none":
                            print(f"✓ Found: Event={event}, Volume={volume} -> {effect}")
                    
                    # Test extended commands
                    print("\n\nTesting extended format (3-byte data):")
                    for event in [1, 2, 4, 6, 7]:
                        print(f"\nTesting Event={event} with extra parameter:")
                        # Try 3-byte format: event, param1, param2
                        for param in [0, 50, 100, 255]:
                            cmd_bytes = bytes([0x0A, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, event, 1, param])
                            print(f"  Event={event}, Mode=1, Param={param}")
                            try:
                                await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                                await asyncio.sleep(0.5)
                            except Exception as e:
                                print(f"  Error: {e}")
                        
                        effect = input("Any different effects with extra parameter? ")
                        if effect:
                            print(f"✓ Extended format works for Event={event}")
                
            elif cmd.startswith('F') and len(cmd) > 1 and cmd[1:].isdigit():
                # Forward with speed parameter (was E1)
                try:
                    speed = int(cmd[1:])
                    if 0 <= speed <= 255:
                        cmd_bytes = bytes([0x0A, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, 1, 1, speed])
                        await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                        print(f"Forward with speed {speed}")
                    else:
                        print("Speed must be 0-255")
                except ValueError:
                    print("Invalid speed value")
                    
            elif cmd.startswith('B') and len(cmd) > 1 and cmd[1:].isdigit():
                # Backward with speed parameter (was E2)
                try:
                    speed = int(cmd[1:])
                    if 0 <= speed <= 255:
                        cmd_bytes = bytes([0x0A, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, 2, 1, speed])
                        await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                        print(f"Backward with speed {speed}")
                    else:
                        print("Speed must be 0-255")
                except ValueError:
                    print("Invalid speed value")
                    
            elif cmd.startswith('C') and len(cmd) > 1 and cmd[1:].isdigit():
                # Color/light control (was E4)
                try:
                    color = int(cmd[1:])
                    if 0 <= color <= 24:
                        cmd_bytes = bytes([0x0A, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, 4, 1, color])
                        await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                        print(f"Set color to {color}")
                    else:
                        print("Color must be 0-24")
                except ValueError:
                    print("Invalid color value")
                    
            elif cmd.startswith('S') and len(cmd) > 1 and cmd[1:].isdigit():
                # Sound control (was E6)
                try:
                    sound = int(cmd[1:])
                    if 0 <= sound <= 255:
                        cmd_bytes = bytes([0x0A, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, 6, 1, sound])
                        await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                        print(f"Play sound {sound}")
                    else:
                        print("Sound must be 0-255")
                except ValueError:
                    print("Invalid sound value")
                    
            elif cmd.startswith('E') and len(cmd) > 1:
                # Handle E0, E1, E2... E10 commands
                try:
                    # Check if there's a space and parameter for E3 or E4
                    parts = cmd.split()
                    if len(parts) == 2 and parts[0] in ['E3', 'E4']:
                        event_num = int(parts[0][1])
                        try:
                            specific_param = int(parts[1])
                            if 0 <= specific_param <= 255:
                                # Test with specific parameter
                                print(f"Testing Event {event_num} with specific 3rd byte: {specific_param}")
                                print("=" * 50)
                                cmd_bytes = bytes([0x0A, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, event_num, 1, specific_param])
                                print(f"Sending: ({event_num},1,{specific_param})")
                                print(f"Hex: {cmd_bytes.hex()}")
                                
                                await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                                
                                effect = input("\nDescribe the effect (or Enter if none): ")
                                if effect:
                                    print(f"✓ Event {event_num} with param {specific_param}: {effect}")
                                    save = input(f"Save to event_{event_num}_effects.txt? (y/n): ")
                                    if save.lower() == 'y':
                                        with open(f"event_{event_num}_effects.txt", "a") as f:
                                            f.write(f"{event_num},1,{specific_param} # {effect}\n")
                                        print("Saved!")
                                continue
                            else:
                                print(f"Parameter must be 0-255, got {specific_param}")
                                continue
                        except ValueError:
                            print(f"Invalid parameter: {parts[1]}")
                            continue
                    
                    # Normal E command processing
                    event_num = int(cmd[1:])
                    if 0 <= event_num <= 10:
                        # Special handling for E7 - just send (7,1) without third byte
                        if event_num == 7:
                            print("Sending Event 7 (stop/brake)")
                            print("=" * 50)
                            cmd_bytes = bytes([0x09, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, 7, 1])
                            print(f"Sending: (7,1)")
                            print(f"Hex: {cmd_bytes.hex()}")
                            await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                            print("Command sent")
                            continue
                            
                        print(f"Testing Event {event_num} with 3-byte format")
                        print("=" * 50)
                        print(f"\nBase command: ({event_num},1,X) where X is the 3rd byte")
                        
                        # Special handling for E4 (lights) and E6 (sounds)
                        if event_num == 4:
                            test_values = list(range(21))  # 0-20 for light colors
                            print("Testing light colors: 0-20")
                        elif event_num == 6:
                            test_values = list(range(256))  # 0-255 for all sound variations
                            print("Testing all sound variations: 0-255")
                            print("This will take time - press 'q' to quit at any point")
                        else:
                            test_values = [0, 1, 5, 10, 25, 50, 75, 100, 127, 150, 200, 255]
                            print("Testing common values: 0, 1, 5, 10, 25, 50, 75, 100, 127, 150, 200, 255")
                        
                        print("Press any key to test next value, or 'q' to skip to next event\n")
                        found_effects = []
                        
                        for i, param in enumerate(test_values):
                            print(f"\n[{i+1}/{len(test_values)}] Testing ({event_num},1,{param})")
                            print(f"Hex: 0a00813411510{event_num:01x}01{param:02x}")
                            
                            # Send 3-byte command
                            cmd_bytes = bytes([0x0A, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, event_num, 1, param])
                            
                            try:
                                await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                                
                                # Wait for observation
                                print("\nObserve the effect...")
                                response = input("Press Enter for next, 'y' if interesting, 'q' to quit: ").strip().lower()
                                
                                if response == 'q':
                                    break
                                elif response == 'y':
                                    effect = input("Describe the effect: ")
                                    found_effects.append((param, effect))
                                    print(f"✓ Marked: param {param} -> {effect}")
                                    
                            except Exception as e:
                                print(f"Error: {e}")
                                if not controller.client.is_connected:
                                    print("Train disconnected!")
                                    break
                        
                        # Summary for this event
                        if found_effects:
                            print(f"\n\nSummary for Event {event_num}:")
                            print("-" * 40)
                            for param, effect in found_effects:
                                print(f"  ({event_num},1,{param}) -> {effect}")
                            
                            # Save to file
                            save = input("\nSave these findings? (y/n): ")
                            if save.lower() == 'y':
                                filename = f"event_{event_num}_effects.txt"
                                with open(filename, "w") as f:
                                    f.write(f"# Event {event_num} 3-byte effects\n")
                                    f.write(f"# Format: event,volume,param # effect\n\n")
                                    for param, effect in found_effects:
                                        f.write(f"{event_num},1,{param} # {effect}\n")
                                print(f"Saved to {filename}")
                        else:
                            print(f"\nNo effects found for Event {event_num}")
                    else:
                        print(f"Invalid event number: {event_num}. Use E0 through E10")
                except ValueError:
                    print(f"Invalid command format. Use E0, E1, E2... E10")
                    
            elif cmd == 'T':
                print("Testing shutdown commands from file")
                print("=" * 50)
                print("\nThis will test commands from shutdown_commands.txt")
                print("WARNING: These commands may cause the train to shut down!")
                
                try:
                    with open("shutdown_commands.txt", "r") as f:
                        lines = f.readlines()
                    
                    # Parse commands from file
                    commands = []
                    for line in lines:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            try:
                                event, volume = line.split(",")
                                commands.append((int(event), int(volume)))
                            except:
                                continue
                    
                    if not commands:
                        print("No valid commands found in shutdown_commands.txt")
                        continue
                    
                    print(f"\nFound {len(commands)} commands to test")
                    print("Each command will be tested with a 3-second delay")
                    confirm = input("\nProceed? (y/n): ")
                    
                    if confirm.lower() == 'y':
                        for i, (event, volume) in enumerate(commands):
                            print(f"\nTesting command {i+1}/{len(commands)}: Event={event}, Volume={volume}")
                            cmd_bytes = bytes([0x09, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, event, volume])
                            print(f"  Command bytes: {cmd_bytes.hex()}")
                            
                            try:
                                await controller.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
                                await asyncio.sleep(0.5)  # Brief wait for response
                                
                                # Log the response
                                if controller.last_notification:
                                    response = controller.last_notification.hex()
                                    print(f"  Response: {response}")
                                    result = controller.evaluate_response(f"Event={event}, Volume={volume}")
                                    print(f"  {result}")
                                else:
                                    print("  No response received")
                                
                                # Check if still connected
                                if not controller.client.is_connected:
                                    print("\n>>> TRAIN DISCONNECTED!")
                                    print(f">>> Shutdown command found: Event={event}, Volume={volume}")
                                    print(f">>> Command bytes: {cmd_bytes.hex()}")
                                    break
                                
                                print(f"  Waiting 3 seconds before next command...")
                                await asyncio.sleep(3)
                                
                            except Exception as e:
                                print(f"\n>>> Error or disconnection detected!")
                                print(f">>> Likely shutdown command: Event={event}, Volume={volume}")
                                print(f">>> Error: {e}")
                                break
                        
                        print("\nTesting complete.")
                        if controller.client.is_connected:
                            print("Train is still connected - no shutdown command found in the list.")
                        
                except FileNotFoundError:
                    print("Error: shutdown_commands.txt not found")
                    print("Run option 'D' first to generate the shutdown commands file")
                except Exception as e:
                    print(f"Error reading file: {e}")
            else:
                print("Unknown command")
                
        except KeyboardInterrupt:
            break

async def main():
    controller = DuploTrainController()
    
    trains = await controller.scan_for_trains()
    
    if not trains:
        logger.error("No LEGO trains found")
        return
    
    print("\nFound trains:")
    for i, train in enumerate(trains):
        print(f"{i + 1}. {train.name or 'Unknown'} - {train.address}")
    
    if len(trains) == 1:
        selected_train = trains[0]
    else:
        try:
            selection = int(input("\nSelect train number: ")) - 1
            selected_train = trains[selection]
        except (ValueError, IndexError):
            logger.error("Invalid selection")
            return
    
    connected = await controller.connect(selected_train)
    if not connected:
        return
    
    try:
        # Wait for initial device discovery
        await asyncio.sleep(2)
        
        logger.info("Ready for commands!")
        
        await interactive_control(controller)
        
    finally:
        # Only try to stop and disconnect if still connected
        if controller.client and controller.client.is_connected:
            try:
                await controller.stop()
            except Exception as e:
                logger.debug(f"Error stopping motor: {e}")
            
            try:
                await controller.disconnect()
            except Exception as e:
                logger.debug(f"Error disconnecting: {e}")
        else:
            logger.info("Train already disconnected")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Program interrupted")