from pydantic import BaseModel, Field
from typing import List, Dict, AsyncIterator, Iterator
from textwrap import dedent

from agno.workflow import Workflow, RunResponse, RunEvent
from agno.agent import Agent
from agno.models.azure import AzureOpenAI

from dotenv import load_dotenv
from app_layer.utils.logger import get_logger
from workflows.utils.get_master_db_url import get_master_db_url
from models.chat_socket_models import AttachmentModel

from infrastructure.read_docx import open_docx

load_dotenv()
logger = get_logger()

class TranscriptSummaryAgent(Workflow):

    def __init__(self, provider: str = "azure", model_name: str = "o4-mini", **kwargs):
        super().__init__(**kwargs)

    summary_agent = Agent(
        model=AzureOpenAI(
            id="o4-mini",
            api_version="2024-12-01-preview",
        ),
        description=dedent("""
            You are an AI assistant tasked with analyzing a presales meeting transcript. Follow these exact instructions and formatting:

            # 1. Individuals
            - Create a table listing all individuals mentioned.
            - Table columns:
            • Full Name  
            • Role/Title  
            • Company (if mentioned)
            - If any information is missing, leave the cell blank.

            # 2. Meeting Discussion Organized into Logical Sections
            - Determine 2 to 4 logical sections based on the flow and themes of the conversation.
            - For each section:
            - Create a clear sub-heading: ## [Section Name] (for example: "Case Study Discussion" or "Technical Challenges and Solutions")
            - Summarize the key points discussed under this section using bullet points.
            - Each bullet point should cover only one idea clearly and concisely.
            - Capture important aspects: pain points, proposed solutions, requirements, questions, objections, commitments, decisions, etc.

            # 3. Follow-ups / Action Items / Next Steps
            - Summarize all action items discussed during the meeting in bullet point format.
            - Group action items logically to avoid repetition (even if assigned to the same person).
            - For each action item, clearly specify:
            • What needs to be done  
            • Who is responsible  
            • Due date or timeline (if mentioned)
            - Ensure that each action item is unique and described only once.
            - If multiple tasks are assigned to the same person, list them as separate bullet points under their name or group together logically.
            - Keep language direct, concise, and actionable.

            # Tone & Style
            - Use simple, professional, and direct language.
            - No jargon, filler, or unnecessary commentary.
            - Keep the output clean with clear separation between sections.
            - Always use bullet points, never paragraphs, to describe discussion points.

            # Formatting Rules
            - Use "# [Section Name]" for main sections.
            - Use "## [Subsection Name]" for logical meeting sections.
            - Use bullet points for all lists and points.
            - Individuals must be presented in a proper table format at the start.

            Always follow
        """),
        debug_mode=False,
        markdown=True
    )

    def run(self, attached_models: AttachmentModel = []) -> Iterator[RunResponse]:

        file_metadata = attached_models[0]
        transcript_file_path =  file_metadata["url"]
        transcript_file_path = "x-local-test\Harika Discussion.docx"
        docs = open_docx(transcript_file_path)

        # Get complete text from the document as str variable
        full_text = ""
        for para in docs.paragraphs:
            full_text += para.text + "\n"

        summary_response = self.summary_agent.run(
            full_text
        )

        yield RunResponse(
            event=RunEvent.run_response,
            content=summary_response.content
        )

# if __name__ == "__main__":
#     workflow = TranscriptSummaryAgent()
#     for response in workflow.run():
#         print(response.content)
