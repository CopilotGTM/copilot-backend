from typing import Dict, Optional
from app_layer.utils.logger import get_logger

logger = get_logger()


def create_row_object(
    sheet_name: str, row_data: Dict, row_index: int, question_column: Optional[str], answer_column: Optional[str]
) -> Dict:
    """
    Create a row object with sheet name, row number, and formatted input message.

    Args:
        sheet_name: Name of the sheet
        row_data: Dictionary containing row data
        row_index: Index of the row (starting from 2 as row 1 is header)
        question_column: Name of the question column (if any)
        answer_column: Name of the answer column

    Returns:
        Dict containing:
            - sheetname: Name of the sheet
            - row_number: Row number
            - input_message: Formatted message with query and additional details
    """
    # Skip if no answer column or answer is empty
    if not question_column or not row_data.get(question_column):
        return None

    # Get description value if question column is not specified
    description_value = row_data.get("Description", "") if not question_column else row_data.get(question_column, "")

    # Get values from other columns (excluding answer column)
    other_columns_data = {
        col: row_data.get(col, "") for col in row_data.keys() if col != answer_column and row_data.get(col)
    }

    # Create additional details string
    additional_details = "\n".join(f"\t{col}: {value}" for col, value in other_columns_data.items())

    # Create input message
    input_message = f"""
        user query: {description_value}
        =====================================

        Additional details : 
        {additional_details}
    """

    # Create the object
    row_object = {"sheetname": sheet_name, "row_number": row_index, "input_message": input_message}

    return row_object
