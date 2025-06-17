import re
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

# Constants for transcript parsing
HEADERS = [
    "Outcome", "Next Steps", "Pain Points", "Interests", "Key Takeaways", 
    "Transcript", "Discussion", "Meeting Notes", "Summary", "Action Items"
]
WANTED_SECTIONS = ["Outcome", "Next Steps", "Transcript", "Discussion"]

# Regex patterns for various transcript formats
PATTERNS = {
    # [10:15] John: Hello everyone
    "timestamp_bracket": re.compile(r'^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*([^:]+):\s*(.+)'),
    # 10:15 John: Hello everyone
    "timestamp_plain": re.compile(r'^(\d{1,2}:\d{2}(?::\d{2})?)\s+([^:]+):\s*(.+)'),
    # (10:15) John: Hello everyone
    "timestamp_paren": re.compile(r'^\((\d{1,2}:\d{2}(?::\d{2})?)\)\s*([^:]+):\s*(.+)'),
    # John: Hello everyone
    "no_timestamp": re.compile(r'^([^:]+):\s*(.+)'),
    # John Smith - Hello everyone
    "dash_format": re.compile(r'^([^-]+)\s*-\s*(.+)'),
}

# Header detection patterns
HEADER_MARKERS = [
    re.compile(r'^#+\s*(.+)$'),  # Markdown headers
    re.compile(rf"({'|'.join(re.escape(h) + r':?' for h in HEADERS)})", re.I),  # Standard headers
    re.compile(r'^[A-Z][A-Z\s]+:$'),  # ALL CAPS headers
    re.compile(r'^=+$'),  # Underline headers
    re.compile(r'^-+$')   # Underline headers
]

class Speaker:
    def __init__(self, name: str):
        self.original_name = name
        self.normalized_name = self._normalize_name(name)
        self.variations: List[str] = [self.normalized_name]
    
    @staticmethod
    def _normalize_name(name: str) -> str:
        """Normalize speaker names to handle variations."""
        name = name.strip().lower()
        # Remove common titles
        for title in ['mr.', 'mrs.', 'ms.', 'dr.', 'prof.']:
            name = name.replace(title, '')
        return name.strip()
    
    def matches(self, other_name: str) -> bool:
        """Check if another name matches this speaker."""
        normalized = self._normalize_name(other_name)
        if normalized in self.variations:
            return True
        # Check for partial matches (e.g., "John Smith" matches "John")
        for variation in self.variations:
            if variation in normalized or normalized in variation:
                self.variations.append(normalized)
                return True
        return False


class TranscriptParser:
    def __init__(self):
        self.speakers: List[Speaker] = []
        self.last_timestamp = ""
        self.current_section = "Transcript"
        self.sections: Dict[str, List[Dict[str, str]]] = {"Transcript": []}
    
    def _parse_timestamp(self, ts: str) -> str:
        """Normalize and validate timestamp format."""
        try:
            # Handle various time formats
            if ':' not in ts:
                return ""
            
            parts = ts.split(':')
            if len(parts) == 2:
                hour, minute = map(int, parts)
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    return f"{hour:02d}:{minute:02d}"
            elif len(parts) == 3:
                hour, minute, second = map(int, parts)
                if 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59:
                    return f"{hour:02d}:{minute:02d}:{second:02d}"
            return ""
        except ValueError:
            return ""

    def _get_or_create_speaker(self, name: str) -> Speaker:
        """Get existing speaker or create new one."""
        name = name.strip()
        for speaker in self.speakers:
            if speaker.matches(name):
                return speaker
        new_speaker = Speaker(name)
        self.speakers.append(new_speaker)
        return new_speaker

    def _parse_line(self, line: str) -> Optional[Dict[str, str]]:
        """Parse a single line of transcript."""
        line = line.strip()
        if not line:
            return None

        # Try all patterns
        for pattern_name, pattern in PATTERNS.items():
            match = pattern.match(line)
            if match:
                if pattern_name in ["timestamp_bracket", "timestamp_plain", "timestamp_paren"]:
                    timestamp, speaker_name, content = match.groups()
                    self.last_timestamp = self._parse_timestamp(timestamp) or self.last_timestamp
                elif pattern_name == "no_timestamp":
                    speaker_name, content = match.groups()
                else:  # dash_format
                    speaker_name, content = match.groups()

                speaker = self._get_or_create_speaker(speaker_name)
                return {
                    "speaker": speaker.original_name,
                    "content": content.strip(),
                    "timestamp": self.last_timestamp
                }

        # If no pattern matches but we're in a conversation
        if self.sections[self.current_section] and line:
            # Append to the last speaker's content
            last_dialog = self.sections[self.current_section][-1]
            last_dialog["content"] = f"{last_dialog['content']} {line}".strip()
            return None

        # If nothing matches and we're not in a conversation, try to detect a new section
        for header_pattern in HEADER_MARKERS:
            if header_pattern.match(line):
                section_name = header_pattern.match(line).group(1).strip(':').strip()
                if section_name not in self.sections:
                    self.sections[section_name] = []
                self.current_section = section_name
                return None

        # If still no match, add as system note
        return {
            "speaker": "System",
            "content": line,
            "timestamp": self.last_timestamp
        }

    def parse_transcript(self, text: str) -> Dict[str, Any]:
        """
        Parse transcript text into structured format.
        
        Args:
            text (str): Raw transcript text
            
        Returns:
            Dict[str, Any]: Structured transcript data
        """
        # Reset state
        self.speakers = []
        self.last_timestamp = ""
        self.sections = {"Transcript": []}
        self.current_section = "Transcript"

        # Extract title if present
        title_match = re.search(r'(?:Meeting Title|Title|Subject):\s*(.+)', text, re.I)
        title = title_match.group(1).strip() if title_match else ""

        # Process each line
        lines = text.split('\n')
        if not title and lines:
            title = lines[0][:60].strip()

        for line in lines:
            parsed = self._parse_line(line)
            if parsed:
                self.sections[self.current_section].append(parsed)

        # Build final result
        result = {
            "meeting_title": title,
            "sections": []
        }

        # Add transcript section first if it exists and has content
        if self.sections.get("Transcript"):
            result["sections"].append({
                "section_name": "Transcript",
                "dialogues": self.sections["Transcript"]
            })

        # Add other sections
        for section_name, dialogues in self.sections.items():
            if section_name != "Transcript" and dialogues:
                result["sections"].append({
                    "section_name": section_name,
                    "dialogues": dialogues
                })

        return result


# Create functions to maintain backward compatibility
def parse_transcript_dialog(block: str) -> List[Dict[str, str]]:
    """Backward compatibility function."""
    parser = TranscriptParser()
    result = parser.parse_transcript(block)
    return result["sections"][0]["dialogues"] if result["sections"] else []

def build_transcript_json(text: str) -> Dict[str, Any]:
    """Backward compatibility function."""
    parser = TranscriptParser()
    return parser.parse_transcript(text)
