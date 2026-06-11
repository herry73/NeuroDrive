import time
from wifi_sender import send_command

def run_test_sequence():
    print("NeuroDrive UDP Test Sender: ")

    sequence = [
        ("FORWARD", 2),
        ("STOP",    1),
        ("LEFT",    1),
        ("STOP",    1),
        ("RIGHT",   1),
        ("STOP",    1),
    ]

    for command, delay in sequence:
        send_command(command)
        time.sleep(delay)

    print("\nTest sequence complete.")

if __name__ == "__main__":
    run_test_sequence()