"""
NeuroDrive Python Bridge
"""

from eeg_reader import read_eeg_sample
from signal_processor import process_signal
from command_mapper import map_to_command
from wifi_sender import send_command

def main_loop():
    print("NeuroDrive Bridge")

    sample = read_eeg_sample()
    intent = process_signal(sample)
    command = map_to_command(intent)
    send_command(command)

if __name__ == "__main__":
    main_loop()