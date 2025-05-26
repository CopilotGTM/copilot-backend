import asyncio
from typing import List, Dict, AsyncIterator, Optional
from uuid import UUID
from dotenv import load_dotenv

from agno.workflow import Workflow, RunResponse, RunEvent
from agno.agent import Agent
from agno.models.azure import AzureOpenAI
from app_layer.utils.logger import get_logger
from workflows.utils.send_status import SendStatus
import json

from workflows.rfp_sheet_mapping.models import ColumnInfo, SheetInfo
from workflows.rfp_sheet_mapping.prompts.sheet_identification_prompt import COLUMN_IDENTIFICATION_PROMPT

load_dotenv()
logger = get_logger()


class SheetIdentificationWorkflow(Workflow):
    def __init__(
        self,
        provider: str = "azure",
        model_name: str = "gpt-4o-mini",
        sio=None,
        websocket_room=None,
        system_message_id: UUID = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.provider = provider
        self.model_name = model_name
        self.sio = sio
        self.room = websocket_room
        self.system_message_id = system_message_id
        logger.info(f"Initializing SheetIdentificationWorkflow with model: {model_name}")

        # Initialize the column identification agent
        self.column_identification_agent = Agent(
            model=AzureOpenAI(id="gpt-4o-mini", api_version="2024-12-01-preview"),
            description=COLUMN_IDENTIFICATION_PROMPT,
            response_model=SheetInfo,
            structured_outputs=True,
            markdown=True,
        )
        logger.info("Column identification agent initialized successfully")

    async def run_workflow(self, sheet_data: Dict) -> Dict:
        """
        Run the sheet identification workflow

        Args:
            sheet_data: Dictionary containing sheet information
                {
                    "sheet_name": str,
                    "rows": List[List[Any]]
                }
        """
        try:
            logger.info(f"Starting column identification workflow for sheet: {sheet_data.get('sheet_name')}")
            await SendStatus.send_chat_status(self, "started", "Column identification started.")

            # Prepare the input for the agent
            analysis_input = {"sheetName": sheet_data["sheet_name"], "rows": sheet_data["data"]}
            logger.info(f"Prepared analysis input for sheet: {sheet_data['sheet_name']}")

            # Run the analysis
            logger.info("Sending request to column identification agent")
            analysis_response = await self.column_identification_agent.arun(
                messages=[{"role": "user", "content": json.dumps(analysis_input)}], stream=True
            )
            logger.info(f"Received response from column identification agent- {analysis_response.content}")

            # Send completion status
            logger.info("Column identification completed successfully")
            await SendStatus.send_chat_status(self, "completed", "Column identification completed.")

            return analysis_response.content

        except Exception as e:
            error_msg = f"Error in column identification workflow: {str(e)}"
            logger.error(error_msg)
            logger.exception("Detailed error traceback:")
            await SendStatus.send_chat_status(self, "error", error_msg)
            raise


# # Example usage
# async def main():
#     # Example sheet data
#     sample_sheet = {
#         "sheet_name": "Sheet1",
#         "headers": ["Sheet Title", "Question", "Answer", "Category"],
#         "sample_values": {
#             "Sheet Title": ["Survey Responses", "What is your name?", "How old are you?"],
#             "Question": ["", "What is your name?", "How old are you?"],
#             "Answer": ["", "John Doe", "25"],
#             "Category": ["", "Personal", "Personal"]
#         },
#         "row_count": 100
#     }

#     # Create and run the workflow
#     workflow = SheetIdentificationWorkflow()
#     async for response in workflow.run_workflow(sample_sheet):
#         logger.info(response.content)

# if __name__ == "__main__":
#     asyncio.run(main())
