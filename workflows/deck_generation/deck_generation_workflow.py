# Standard library imports
from typing import AsyncIterator
import asyncio
import shutil
import os

# Third-party imports
from dotenv import load_dotenv
from uuid import UUID, uuid4
import boto3

# Agno framework imports
from agno.workflow import Workflow, RunResponse
from agno.agent import Agent
from agno.models.azure import AzureOpenAI
from agno.knowledge.pdf import PDFKnowledgeBase
from agno.vectordb.pgvector import PgVector
from agno.document.chunking.recursive import RecursiveChunking
from agno.embedder.azure_openai import AzureOpenAIEmbedder
from agno.media import Image
from agno.models.message import Message
import random

# Local application imports
from app_layer.utils.logger import get_logger
from core.database import get_master_session
from models.chat_db_models import ChatMessage as ChatMessageDB
from workflows.utils.get_master_db_url import get_master_db_url
from workflows.utils.slide_utils.slide_basics import PptxBasicSlideManager
from workflows.utils.slide_utils.extract_ppt import PPTXExtractor
from workflows.utils.slide_utils.pptx_text_replacer import PPTXTextReplacer
from workflows.deck_generation.prompts.previous_slide_selection import SLIDE_UPDATE_PROMPT
from workflows.deck_generation.models import TextMapping

# from testing_local.test import deck_message
from workflows.deck_generation.slide_mapping import SLIDE_MAPPING

load_dotenv()
logger = get_logger()


def upload_to_s3(file_path: str, bucket_name: str = "test-deepak-deepak") -> str:
    """
    Upload a file to S3 and return the public URL.

    Args:
        file_path (str): Local path to the file to upload
        bucket_name (str): Name of the S3 bucket

    Returns:
        str: Public URL of the uploaded file
    """
    try:
        file_name = os.path.basename(file_path)
        # Generate a unique path for the file
        # Using UUID to ensure uniqueness, but not using memory_session here
        unique_id = uuid4()
        s3_path = f"presentations/{unique_id}/{file_name}"

        # Initialize S3 client and upload the file
        s3_client = boto3.client("s3")
        with open(file_path, "rb") as file:
            s3_client.upload_fileobj(
                file,
                bucket_name,
                s3_path,
                ExtraArgs={
                    "ContentType": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    "CacheControl": "max-age=31536000",
                },
            )

        # Generate the direct URL
        public_url = f"https://{bucket_name}.s3.amazonaws.com/{s3_path}"

        # Clean up local file
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"Successfully deleted local file: {file_path}")

        return public_url
    except Exception as e:
        logger.error(f"Error uploading file to S3: {str(e)}")
        raise


class DeckGeneration(Workflow):

    def __init__(self, provider: str = "azure", model_name: str = "o4-mini", **kwargs):
        super().__init__(**kwargs)

        # Initialize list to store previous slide summaries
        self.previous_slides = []

        df_url = get_master_db_url()
        self.knowledge_base = PDFKnowledgeBase(
            path="",
            vector_db=PgVector(
                table_name="knowledge_base",
                db_url=df_url,
                embedder=AzureOpenAIEmbedder(),
                schema="public",
            ),
            chunking_strategy=RecursiveChunking(),
        )
        self.master_session = next(get_master_session())

        # Define base paths for input and output PPTX files
        self.input_pptx_path = "./Solution Deck 1.pptx"
        self.output_pptx_path = f"./aptronics_{random.randint(1000, 9999)}.pptx"
        shutil.copy2(self.input_pptx_path, self.output_pptx_path)

        # Initialize PPTX Classes and store slide text
        self.pptx_extractor = PPTXExtractor()
        self.pptx_basic_slide_manager = PptxBasicSlideManager("./Solution Deck 1.pptx")
        self.pptx_text_replacer = PPTXTextReplacer(self.output_pptx_path)
        self.delete_slide = []

        # Read the input PPT and create a copy for output
        self.slide_text = self.pptx_extractor.extract_from_pptx(self.input_pptx_path)

        # Configure the Agent for slide-by-slide updates, keeping only user memories
        self.previous_slide_extraction: Agent = Agent(
            model=AzureOpenAI(id="gpt-4.1", api_version="2024-12-01-preview"),
            tools=[self.get_previous_slides()],
            knowledge=self.knowledge_base,
            description=SLIDE_UPDATE_PROMPT,
            response_model=TextMapping,
            structured_outputs=True,
        )

    def get_previous_slides(self) -> str:
        """
        Returns a string containing all previous slide summaries.

        Returns:
            str: String containing summaries of all previously processed slides, separated by newlines
        """
        logger.info("Retrieving previous slide summaries.")
        return "\n".join(self.previous_slides) if self.previous_slides else ""

    async def run_workflow(self, deck_message: ChatMessageDB) -> AsyncIterator[RunResponse]:
        try:
            tech_details = deck_message["technical_deck_details"]
        except:
            tech_details = deck_message.technical_deck_details

        # Extract slide outline and transcript summary from the technical details
        slide_outline = tech_details["slide_content_summary"]
        transcript_summary = tech_details["complete_transcript_summary"]

        slide_count = self.pptx_basic_slide_manager.get_slide_count()

        for slide_number in range(1, slide_count + 1):
            mapped_slide = [i for i in SLIDE_MAPPING if i["slide_number"] == slide_number]

            # Get current slide content
            current_slide_texts = self.slide_text["slides"][slide_number - 1]

            # Process texts through the agent to get updated content
            messages = [
                Message(role="assistant", content=f"#### Meeting Summary - {transcript_summary}"),
                Message(role="assistant", content=f"#### Slide Outline - {slide_outline}"),
                Message(role="assistant", content=f"#### Analysis of slide given to update - {mapped_slide}"),
                Message(role="user", content=f"#### Slide Text/Images - {current_slide_texts}"),
            ]
            images_list = [Image(filepath=f"./output_images/slide_{slide_number}.png")]

            text_mapping_response = await self.previous_slide_extraction.arun(messages=messages, images=images_list)

            if isinstance(text_mapping_response.content, TextMapping):
                content = text_mapping_response.content.model_dump()
                if content["slide_required"]:
                    slide_replacement = {
                        "slide_number": slide_number,
                        "replacement_texts": content.get("replacements", []),
                    }

                    # Store the slide summary
                    self.previous_slides.append(content.get("slide_summary"))
                    logger.info(f"Slide summary - {content.get('slide_summary')}")

                    # Ensure the slide number is valid
                    if slide_replacement["replacement_texts"]:
                        self.pptx_text_replacer.run(slide_replacement)
                else:
                    self.delete_slide.append(slide_number)
            yield RunResponse(
                content={"completion_percentage": round((slide_number / slide_count) * 100, 2), "public_url": ""}
            )
        # Delete slides that are marked for deletion
        self.pptx_text_replacer.delete_slides(self.delete_slide)

        # Upload the generated PPT and create a downloadable URL
        public_url = upload_to_s3(self.output_pptx_path)
        yield RunResponse(content={"completion_percentage": 100, "public_url": public_url}, status="COMPLETED")


# async def main():
#     chat = DeckGeneration()
#     output = []

#     logger.info("Workflow execution started.")

#     try:
#         async for response in chat.run_workflow(deck_message):
#             logger.info(f"Received response: {response}")
#     except Exception as e:
#         logger.error(f"Error in workflow: {str(e)}")

#     logger.info(f"Workflow completed.")
#     return output

# if __name__ == "__main__":
#     asyncio.run(main())
