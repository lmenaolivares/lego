.PHONY: run install setup clean run-node setup-node

# Run the train controller (Python)
run:
	@source venv/bin/activate && python duplo_train_controller.py

# Run the simple train controller (motor only)
simple:
	@source venv/bin/activate && python duplo_train_simple.py

# Run the toddler-friendly controller
toddler:
	@source venv/bin/activate && python ../duplo_2.0.py

# Run Bluetooth sniffer to capture app commands
sniff:
	@source venv/bin/activate && python bluetooth_sniffer.py

# Monitor Bluetooth commands (no external tools needed)
monitor:
	@source venv/bin/activate && python bluetooth_monitor.py

# Capture commands via proxy mode
capture:
	@source venv/bin/activate && python bluetooth_capture_proxy.py

# Install Python dependencies
install:
	@source venv/bin/activate && pip install -r requirements.txt

# Initial setup for Python (create venv and install deps)
setup:
	@python3 -m venv venv
	@source venv/bin/activate && pip install -r requirements.txt
	@echo "Setup complete! Run 'make run' to start the controller"

# Setup Node.js version
setup-node:
	@npm install
	@echo "Node.js setup complete! Run 'make run-node' to start"

# Run Node.js version
run-node:
	@sudo node duplo_train_node.js

# Clean up virtual environment and node modules
clean:
	@rm -rf venv __pycache__ node_modules
	@echo "Cleaned up virtual environment and node modules"