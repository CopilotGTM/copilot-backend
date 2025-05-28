from typing import List, Dict

def format_user_messages(message_list: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Format a list of messages into a standardized format.
    
    Args:
        message_list (List[Dict[str, str]]): List of messages with role and content
        
    Returns:
        List[Dict[str, str]]: Formatted list of messages
    """
    formatted_messages = []
    for message in message_list:
        formatted_message = {
            "role": message.role,
            "content": message.content
        }
        formatted_messages.append(formatted_message)
    
    return formatted_messages 