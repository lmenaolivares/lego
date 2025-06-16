# LEGO Duplo Train Controller

Control your LEGO Duplo train (model 10426) via Bluetooth using Python.

## Setup

1. Create and activate virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate  # On macOS/Linux
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

## Usage

1. Turn on your LEGO Duplo train
2. Run the controller:
```bash
python duplo_train_controller.py
```

3. The script will:
   - Scan for LEGO trains
   - Connect to your selected train
   - Play a sound to confirm connection
   - Start interactive control mode

## Controls

- `f` - Forward (slow)
- `F` - Forward (fast)
- `b` - Backward (slow)
- `B` - Backward (fast)
- `s` - Stop
- `1-5` - Play sounds
- `c` - Change light color
- `q` - Quit

## Requirements

- Python 3.8+
- macOS (for CoreBluetooth support) or Linux with BlueZ
- Bluetooth 4.0+ adapter