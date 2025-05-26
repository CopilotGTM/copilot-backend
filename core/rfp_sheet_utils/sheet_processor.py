import asyncio
from typing import Dict, List
from app_layer.utils.logger import get_logger
from workflows.rfp_sheet_mapping.sheet_mapping_workflow import SheetIdentificationWorkflow

logger = get_logger()

async def process_sheets_in_parallel(sheets_data: Dict[str, List], session_id: str, system_message_id: str) -> List[Dict]:
    """
    Process multiple sheets in parallel using the SheetIdentificationWorkflow.
    
    Args:
        sheets_data: Dictionary containing sheet data where key is sheet name and value is sheet rows
        session_id: The chat session ID
        system_message_id: The system message ID for tracking
        
    Returns:
        List of processed sheet details with the following structure:
        [
            {
                "sheet_name": str,
                "is_sheet_selected": bool,
                "columns": [
                    {
                        "name": str,
                        "is_question_column": bool,
                        "is_answer_column": bool
                    }
                ],
                "column_row_number": int
            }
        ]
    """
    try:
        # Initialize the workflow
        workflow = SheetIdentificationWorkflow(
            websocket_room=str(session_id),
            system_message_id=system_message_id
        )
        
        # Create tasks for each sheet
        tasks = []
        for temp in sheets_data:
            task = workflow.run_workflow({
                "sheet_name": temp["sheetname"],
                "data": temp["data"]
            })
            tasks.append(task)
        
        # Run all tasks concurrently
        sheets_details = await asyncio.gather(*tasks)
        
        # Format the response
        sheet_list_details = [
            {
                "sheet_name": sheet.sheet_name,
                "is_sheet_selected": False,
                "columns": [
                    {"name": column.name, "is_question_column": False, "is_answer_column": False}
                    for column in sheet.columns
                ],
                "column_row_number": 1
            }
            for sheet in sheets_details
        ]
        
        return sheet_list_details
        
    except Exception as e:
        logger.error(f"Error in parallel sheet processing: {str(e)}")
        raise 