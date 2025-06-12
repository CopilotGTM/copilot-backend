from pydantic import BaseModel
import asyncio
from typing import AsyncIterator, List, Dict
from textwrap import dedent

from agno.workflow import Workflow, RunResponse, RunEvent
from agno.agent import Agent
from agno.models.azure import AzureOpenAI

from dotenv import load_dotenv
from app_layer.utils.logger import get_logger
from workflows.utils.get_master_db_url import get_master_db_url
from models.chat_socket_models import AttachmentModel

from infrastructure.read_docx import open_docx
from workflows.utils.cloud_uri import to_s3_uri

load_dotenv()
logger = get_logger()


class TranscriptSummaryAgent(Workflow):

    def __init__(self, provider: str = "azure", model_name: str = "o4-mini", **kwargs):
        super().__init__(**kwargs)

    summary_agent = Agent(
        model=AzureOpenAI(
            id="o4-mini",
            api_version="2024-12-01-preview",
            reasoning_effort="high"
        ),
        description=dedent(
            """
            You are an AI assistant tasked with analyzing a presales meeting transcript. Thoroughly analyze the meeting transcript. 
            You work for Xeno Organization and have to summarize the provided points from their perspective. The provided output summary will be used for deck creation.

            Follow these exact instructions and formatting:

            Following Sections are potential Output Sections
            # Overview
            - Provide a quick overview of the meeting. This overview will set a context in the reader's mindset.
            - Sharply analyze the crux of the meeting and then provide this summary in as minimum words.
            - Everything should be to the point. Reader should be able to understand and able to explain the agenda of this meeting.

            # Individuals
            - Create a table listing all individuals mentioned. Ensure that the names provided are actual individuals.
            - Analyze the speakers in depth and determine the list of individuals present in the meeting.
            - Avoid adding any details unrelated to the meeting participants.
            - Table columns:
                • Full Name  
                • Role/Title  
                • Company (if mentioned)
                - If any information is missing, leave the cell blank.

            # Meeting Discussion Organized into Logical Sections
              Analyze the meeting transcript and determine 2 to 4 logical sections based on the flow and themes of the conversation. These sections should clearly explain all the points discussed in the meeting.
              As these sections will determine the next steps for the users. Capture all the numbers clearly as these numbers will be useful for our sales pitch.
            - For each section:
            - Create a clear sub-heading: ## [Section Name] (for example: "Case Study Discussion" or "Technical Challenges and Solutions")
            - Summarize the points discussed under this section using bullet points. Try to capture all the points associated with the section.
            - Each bullet point should cover one idea clearly and concisely. Requirements should be clearly visible from the generated point.
            - Capture important aspects: pain points, proposed solutions, requirements, questions, objections, commitments, decisions, etc.

            # Definitions
              Every business has its own terminologies. And specific business rules that re defined to run business.
              Clearly mention all such and then definations. provide the points in order of decresing importance. You should clearly identify such business rules defined by business.
            - This should cover all the business terminologies which are discussed in the meeting. 
            - Also include the internal definitions and limits set by the business for different programs/events/tools etc
            - Terminology: Crisp Explanations
            - Only show at max top 8 points.

            # Requirements
              Once all the sections are clearly outlined. List out all the requirements of the client that Xeno needs to take care. These requirements should capture all the pain points that client has shared in the meeting.
              These requirements should be aligned with the final goal business goal of our client. Do a thorough research while you put these points as these will be used for the building blocks for sales pitch.
              - Provide these as bullet points. 
              - The problem and goal should be clearly visible to the reader.

            # Important Points
              This section will contain all the important points on which Xeno needs to focus to win this client. Notice in the transcript if any point is there which can make our sales or technical pitch a direct hit.
              This section should contain only 1-3 points. But these points need to be out of the box. 
              - Clearly determine these in form of bullet points 

            # Follow-ups / Action Items / Next Steps
            - Summarize all action items discussed during the meeting in bullet point format.
            - Group action items logically to avoid repetition (even if assigned to the same person).
            - For each action item, clearly specify:
            • What needs to be done  
            • Who is responsible  
            • Due date or timeline (if mentioned)
            - Ensure that each action item is unique and described only once.
            - If multiple tasks are assigned to the same person, list them as separate bullet points under their name or group together logically.
            - Keep language direct, concise, and actionable.

            Things to keep in mind while doing summary analysis
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
        """
        ),
        debug_mode=False,
        markdown=True,
    )

    async def run_workflow(self, attached_models: List[AttachmentModel]) -> AsyncIterator[RunResponse]:
        logger.info("Starting TranscriptSummaryAgent workflow")

        if not attached_models:
            raise ValueError("No attachments provided.")

        file_metadata = attached_models[0]
        transcript_file_path = to_s3_uri(file_metadata["url"])
        docs = open_docx(transcript_file_path)

        full_text = "\n".join([para.text for para in docs.paragraphs])

        # Properly await coroutine before using async for
        summary_response = await self.summary_agent.arun(full_text, stream=True)
        logger.info("Summary response initiated")

        async for chunk in summary_response:
            yield RunResponse(event=RunEvent.run_response, content=chunk.content)


# async def main():
#     attachments = [
#         {
#             "filename": "Harika Discussion.docx",
#             "url": "https://gtm-dev-application.s3.amazonaws.com/uploads/8c67b20f-9305-4f93-9e14-895f39b30b83/c9ac5227-c27d-4676-bf3f-42b712775e45/ffda9a64-071e-4fd8-9852-3924785bc817/Harika%20Discussion.docx",
#             "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
#             "size_bytes": 1926221,
#             "uploaded_at": "2025-05-07T10:12:12.910Z",
#             "metadata": {}
#         }
#     ]

#     transcript_summary_agent = TranscriptSummaryAgent()

#     logger.info("Workflow execution started.")
#     async for response in transcript_summary_agent.run_workflow(attachments):
#         logger.info(response.content)  # Prints each chunk directly


# if __name__ == "__main__":
#     asyncio.run(main())
