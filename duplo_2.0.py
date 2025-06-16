#!/usr/bin/env python3

import asyncio
import sys
import termios
import tty
import logging
import json
import os
from bleak import BleakScanner, BleakClient
from bleak.exc import BleakError

# Configure logging
logging.basicConfig(level=logging.INFO)
logging.getLogger("bleak").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# LEGO Duplo Train constants
LEGO_MANUFACTURER_DATA = 0x0397
SERVICE_UUID = "00001623-1212-efde-1623-785feabcd123"
CHARACTERISTIC_UUID = "00001624-1212-efde-1623-785feabcd123"

# Configuration file path
CONFIG_FILE = "duplo_config.json"

class DuploTrainToddlerController:
    def __init__(self):
        self.client = None
        self.device = None
        self.running = True
        self.config = self.load_config()
        self.current_color = 0
        self.current_sound = 0
        self.reconnecting = False
        
    def load_config(self):
        """Load configuration from file"""
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            print(f"Configuration file {CONFIG_FILE} not found")
            print("Using default configuration")
            return self.get_default_config()
        except json.JSONDecodeError:
            print(f"Error parsing {CONFIG_FILE}")
            print("Using default configuration")
            return self.get_default_config()
    
    def get_default_config(self):
        """Return default configuration"""
        return {
            "key_mappings": {
                "F": "FORWARD_64",
                "B": "BACKWARD_64",
                "S": "STOP",
                "Q": "QUIT"
            },
            "actions": {
                "FORWARD_64": {"event": 1, "speed": 64},
                "BACKWARD_64": {"event": 2, "speed": 64},
                "STOP": {"event": 7, "speed": 0}
            },
            "color_range": {"min": 0, "max": 23},
            "sound_range": {"min": 0, "max": 6},
            "reconnect_settings": {
                "max_attempts": 10,
                "retry_delay": 5
            }
        }
        
    async def scan_for_train(self):
        """Scan for LEGO Duplo train"""
        print("Looking for train...")
        devices = await BleakScanner.discover(timeout=10.0)
        
        for device in devices:
            if device.metadata.get('manufacturer_data'):
                for mfr_id in device.metadata['manufacturer_data']:
                    if mfr_id == LEGO_MANUFACTURER_DATA:
                        print(f"Found train: {device.name}")
                        return device
        
        return None
    
    async def connect(self, device):
        """Connect to the train"""
        self.device = device
        self.client = BleakClient(device.address)
        
        try:
            await self.client.connect()
            print("Connected to train")
            
            # Enable notifications (simplified version)
            async def notification_handler(sender, data):
                pass  # Silent handler for toddler version
            
            await self.client.start_notify(CHARACTERISTIC_UUID, notification_handler)
            
            # Activate hub
            command = bytearray([0x01, 0x02, 0x02])  # Enable button updates
            await self.send_command(command)
            await asyncio.sleep(0.5)
            
            return True
        except BleakError as e:
            print("Could not connect to train")
            return False
    
    async def send_command(self, command):
        """Send command to train with length prefix"""
        message = bytearray([len(command) + 2, 0x00]) + command
        await self.client.write_gatt_char(CHARACTERISTIC_UUID, message)
    
    async def execute_motor_action(self, action_name):
        """Execute a motor action from config"""
        if action_name not in self.config["actions"]:
            print(f"Unknown action: {action_name}")
            return
            
        action = self.config["actions"][action_name]
        if "event" in action and "speed" in action:
            if action_name == "STOP":
                # Stop uses direct motor control, not events
                cmd_bytes = bytes([0x08, 0x00, 0x81, 0x32, 0x01, 0x51, 0x00, 0x00])
            else:
                # Forward/Backward use event format with 3-byte payload: event, 1, speed
                cmd_bytes = bytes([0x0A, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, 
                                  action["event"], 1, action["speed"]])
            await self.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
            print(f"Executing: {action_name}")
    
    async def play_horn(self):
        """Play horn sound"""
        # Use event 7 for horn (E7)
        cmd_bytes = bytes([0x0A, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, 7, 1, 1])
        await self.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
        print("Horn!")
    
    async def set_color(self, color):
        """Set a specific color"""
        color_range = self.config.get("color_range", {"min": 0, "max": 23})
        if color < color_range["min"] or color > color_range["max"]:
            print(f"Color {color} out of range ({color_range['min']}-{color_range['max']})")
            return
        
        self.current_color = color
        # Use event-based command for color (event 4, 1, color)
        cmd_bytes = bytes([0x0A, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, 4, 1, self.current_color])
        await self.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
        print(f"Color: {self.current_color}")
    
    async def cycle_color(self):
        """Cycle through colors, skipping 0, 11, and 16"""
        color_range = self.config.get("color_range", {"min": 0, "max": 23})
        skip_colors = [0, 11, 16]
        
        # Find next color that's not in skip list
        next_color = self.current_color
        while True:
            next_color = (next_color + 1) % (color_range["max"] + 1)
            if next_color < color_range["min"]:
                next_color = color_range["min"]
            if next_color not in skip_colors:
                break
        
        self.current_color = next_color
        # Use event-based command for color (event 4, 1, color)
        cmd_bytes = bytes([0x0A, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, 4, 1, self.current_color])
        await self.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
        print(f"Color: {self.current_color}")
    
    async def cycle_sound(self):
        """Cycle through sounds"""
        sound_range = self.config.get("sound_range", {"min": 0, "max": 6})
        
        # Increment and wrap around correctly (0-6)
        self.current_sound = (self.current_sound + 1)
        if self.current_sound > sound_range["max"]:
            self.current_sound = sound_range["min"]
        
        # Use event-based command for sound (event 6, 1, sound_variation)
        cmd_bytes = bytes([0x0A, 0x00, 0x81, 0x34, 0x11, 0x51, 0x01, 6, 1, self.current_sound])
        await self.client.write_gatt_char(CHARACTERISTIC_UUID, cmd_bytes)
        print(f"Sound: {self.current_sound} (sending 6,1,{self.current_sound})")
    
    def getch(self):
        """Get single character from terminal"""
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(sys.stdin.fileno())
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        return ch
    
    async def run_interactive(self):
        """Run the interactive control loop"""
        print("\nTrain Control Ready")
        print("Configuration loaded from:", CONFIG_FILE)
        print("\nKey mappings:")
        for key, action in self.config["key_mappings"].items():
            print(f"  {key} = {action}")
        
        while self.running:
            try:
                # Check connection
                if not self.client or not self.client.is_connected:
                    if not self.reconnecting:
                        print("\nConnection lost")
                        await self.handle_reconnection()
                    continue
                
                # Check for keyboard input
                char = self.getch()
                
                # # Check for ESC key (ASCII 27)
                # if ord(char) == 27:
                #     print("\nQuitting")
                #     self.running = False
                #     break
                
                # Look up action in config
                action = self.config["key_mappings"].get(char)
                if not action:
                    action = self.config["key_mappings"].get(char.upper())
                
                if action == "QUIT":
                    print("\nQuitting")
                    self.running = False
                    break
                elif action == "HORN":
                    await self.play_horn()
                elif action == "COLOR":
                    await self.cycle_color()
                elif action == "SOUND":
                    await self.cycle_sound()
                elif action and action.startswith("COLOR_"):
                    # Handle direct color selection
                    try:
                        color_value = int(action.split("_")[1])
                        await self.set_color(color_value)
                    except (ValueError, IndexError):
                        print(f"Invalid color action: {action}")
                elif action:
                    await self.execute_motor_action(action)
                else:
                    # Print the character value for unassigned keys
                    print(f"Unassigned key: '{char}' (value: {ord(char)})")
                    
            except Exception as e:
                logger.error(f"Error in control loop: {e}")
                if not self.client or not self.client.is_connected:
                    await self.handle_reconnection()
    
    async def handle_reconnection(self):
        """Handle automatic reconnection"""
        self.reconnecting = True
        reconnect_config = self.config.get("reconnect_settings", {
            "max_attempts": 10,
            "retry_delay": 5
        })
        
        for attempt in range(reconnect_config["max_attempts"]):
            print(f"\nReconnection attempt {attempt + 1}/{reconnect_config['max_attempts']}")
            print("Trying to reconnect...")
            
            # Try to find the train again
            device = await self.scan_for_train()
            if device:
                if await self.connect(device):
                    print("Reconnected")
                    self.reconnecting = False
                    return
            
            if attempt < reconnect_config["max_attempts"] - 1:
                print(f"Waiting {reconnect_config['retry_delay']} seconds before next attempt")
                await asyncio.sleep(reconnect_config["retry_delay"])
        
        print("Failed to reconnect after all attempts")
        self.running = False
        self.reconnecting = False

async def main():
    """Main function"""
    controller = DuploTrainToddlerController()
    
    # Keep trying to find and connect to train
    reconnect_config = controller.config.get("reconnect_settings", {
        "max_attempts": 1000,
        "retry_delay": 2
    })
    
    connected = False
    for attempt in range(reconnect_config["max_attempts"]):
        print(f"\nConnection attempt {attempt + 1}/{reconnect_config['max_attempts']}")
        
        # Find train
        device = await controller.scan_for_train()
        if device:
            # Try to connect
            if await controller.connect(device):
                connected = True
                break
        else:
            print("No train found. Make sure it is turned on")
        
        if attempt < reconnect_config["max_attempts"] - 1:
            print(f"Waiting {reconnect_config['retry_delay']} seconds before next attempt")
            await asyncio.sleep(reconnect_config["retry_delay"])
    
    if not connected:
        print("Failed to connect after all attempts")
        return
    
    try:
        # Run interactive control
        await controller.run_interactive()
    finally:
        # Disconnect
        if controller.client and controller.client.is_connected:
            # Send stop command before disconnecting
            stop_action = controller.config["actions"].get("STOP")
            if stop_action:
                await controller.execute_motor_action("STOP")
            await controller.client.disconnect()
            print("Disconnected from train")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nGoodbye")