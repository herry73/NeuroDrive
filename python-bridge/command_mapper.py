"""
Command Mapper.
Takes a processed signal/intent and maps it to a wifi_sender command.
"""

VALID_COMMANDS = ["FORWARD", "LEFT", "RIGHT", "STOP"]

def map_to_command(intent):
    """
    Maps a processed intent string to a valid command.
    """
    if intent in VALID_COMMANDS:
        return intent
    return "STOP"  # fail-safe default