# Standard library imports
from typing import AsyncIterator
import asyncio
from io import StringIO
import xml.sax.saxutils as saxutils
from xml.sax import make_parser
from xml.sax.handler import ContentHandler
import uuid
import shutil
import os

# Third-party imports
from dotenv import load_dotenv
from uuid import UUID
import boto3
from botocore.exceptions import ClientError
from core.app_state import fastapp
from infrastructure.generate_presigned_url import GeneratePresignedURL
from lxml import etree

# Agno framework imports
from agno.workflow import Workflow, RunResponse, RunEvent
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
from workflows.utils.slide_utils.slide_basics import PptxSlideUpdater
from workflows.utils.slide_utils.extract_ppt import PPTXExtractor
from workflows.deck_generation.prompts.previous_slide_selection import SLIDE_UPDATE_PROMPT
from workflows.deck_generation.models import TextMapping
# from testing_local.test import deck_message
from workflows.deck_generation.slide_mapping import SLIDE_MAPPING

load_dotenv()
logger = get_logger()


class DeckGeneration(Workflow):

    def __init__(self, provider: str = "azure", model_name: str = "o4-mini", **kwargs):
        super().__init__(**kwargs)
        # Add PowerPoint XML namespaces
        self.namespaces = {
            'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
            'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
            'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
        }
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
        self.embedder = AzureOpenAIEmbedder()
        self.memory_session = str(uuid.uuid4())
        self.memory_userid = str(uuid.uuid4())

        # Initialize PPTXExtractor and store slide text
        self.pptx_extractor = PPTXExtractor()
        self.input_pptx_path = "./Solution Deck 1.pptx"
        self.output_pptx_path = f"./aptronics_{random.randint(1000, 9999)}.pptx"
        # Read the input PPT and create a copy for output
        shutil.copy2(self.input_pptx_path, self.output_pptx_path)
        
        self.slide_text = self.pptx_extractor.extract_from_pptx(self.input_pptx_path)

        # Configure the Agent for slide-by-slide updates, keeping only user memories
        self.previous_slide_extraction: Agent = Agent(
            model=AzureOpenAI(id="gpt-4.1", api_version="2024-12-01-preview"),
            knowledge=self.knowledge_base,
            description=SLIDE_UPDATE_PROMPT,
            response_model=TextMapping,
            structured_outputs=True
        )

    def validate_xml(self, xml_string: str) -> bool:
        """Validate if the XML string is well-formed."""
        try:
            xml_file = StringIO(xml_string)
            parser = make_parser()
            parser.setContentHandler(ContentHandler())
            parser.parse(xml_file)
            return True
        except Exception as e:
            logger.error(f"XML validation error: {str(e)}")
            return False

    def update_slide_text(self, slide_xml: str, replacements: list) -> str:
        """Update text in slide XML while preserving structure and relationships."""
        try:
            # Parse XML with lxml, preserving all formatting and declarations
            parser = etree.XMLParser(remove_blank_text=False)
            root = etree.fromstring(slide_xml.encode('utf-8'), parser)
            
            # Register namespaces
            for prefix, uri in self.namespaces.items():
                etree.register_namespace(prefix, uri)
            
            # Find all text elements
            for elem in root.xpath('.//a:t', namespaces=self.namespaces):
                current_text = elem.text if elem.text else ''
                for replacement in replacements:
                    if replacement.previous_text in current_text:
                        # Update only the text content, preserving all attributes
                        elem.text = current_text.replace(
                            replacement.previous_text,
                            replacement.updated_text
                        )
            
            # Convert back to string with proper namespace handling and formatting
            # Note: We don't include XML declaration when converting to unicode
            return etree.tostring(
                root,
                encoding='unicode',
                method='xml',
                pretty_print=True,
                xml_declaration=False  # Changed to False for unicode output
            )
        except Exception as e:
            logger.error(f"Error updating slide text: {str(e)}")
            return slide_xml

    async def run_workflow(self, deck_message: ChatMessageDB) -> AsyncIterator[RunResponse]:
        try:
            tech_details = deck_message["technical_deck_details"]
        except:
            tech_details = deck_message.technical_deck_details

        slide_outline = tech_details["slide_content_summary"]
        transcript_summary = tech_details["complete_transcript_summary"]
        pptx_updater = PptxSlideUpdater(self.output_pptx_path)
        slide_count = pptx_updater.get_slide_count()
        previous_generated_slides = []

        for slide_number in range(1, slide_count + 1):
            mapped_slide = [i for i in SLIDE_MAPPING if i["slide_number"] == slide_number]
            try:
                logger.info(f"Processing slide {slide_number}")
                slide_xml = pptx_updater.read_slide_xml(slide_number)
                current_slide_texts = self.slide_text["slides"][slide_number - 1]

                # Get the response from the agent
                logger.info(f"Calling agent for slide {slide_number}")
                response = self.previous_slide_extraction.run(
                    messages=[
                        Message(role="user", content=f"#### Meeting Summary - {transcript_summary}"),
                        Message(role="user", content=f"#### Slide Outline - {slide_outline}"),
                        Message(role="user", content=f"#### Analysis of slide given to update - {mapped_slide}"),
                        Message(role="user", content=f"#### Slide Text/Images - {current_slide_texts}"),
                    ],
                    images=[Image(filepath=f'./output_images/slide_{slide_number}.png')],
                )
                logger.info(f"Received agent response for slide {slide_number}: {type(response)}")

                # Extract the TextMapping from the RunResponse
                if isinstance(response, RunResponse):
                    text_mapping = response.content
                    if not isinstance(text_mapping, TextMapping):
                        logger.error(f"Invalid response type for slide {slide_number}. Expected TextMapping, got {type(text_mapping)}")
                        continue
                else:
                    logger.error(f"Invalid response type for slide {slide_number}. Expected RunResponse, got {type(response)}")
                    continue

                logger.info(f"Slide required: {text_mapping.slide_required}")
                logger.info(f"New summary: {text_mapping.slide_summary}")
                logger.info(f"Reasoning: {text_mapping.reasoning}")
                if text_mapping.slide_required:
                    # Use the new update_slide_text method instead of direct replacement
                    updated_xml = self.update_slide_text(slide_xml, text_mapping.replacements)
                    
                    if not self.validate_xml(updated_xml):
                        logger.error(f"Invalid XML structure for slide {slide_number}")
                        continue

                    pptx_updater.replace_slide_xml(slide_number, updated_xml)
                    logger.info(f"Successfully updated slide {slide_number}")
                else:
                    pptx_updater.delete_slide(slide_number)

            except Exception as e:
                logger.error(f"Error processing slide {slide_number}: {str(e)}")
                continue

        # Upload the generated PPT and create a downloadable URL
        try:
            logger.info("Uploading generated PPT to cloud storage")
            file_name = os.path.basename(self.output_pptx_path)
            file_type = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            
            # Generate a unique path for the PPT
            file_path = f"presentations/{UUID(self.memory_session)}/{file_name}"
            logger.info(f"Uploading file to path: {file_path}")
            
            # Initialize S3 client and upload the file
            s3_client = boto3.client("s3")
            with open(self.output_pptx_path, 'rb') as file:
                s3_client.upload_fileobj(
                    file,
                    "test-deepak-deepak",
                    file_path,
                    ExtraArgs={
                        'ContentType': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
                        'CacheControl': 'max-age=31536000'
                    }
                )
            
            # Generate the direct URL
            public_url = f"https://test-deepak-deepak.s3.amazonaws.com/{file_path}"
            logger.info(f"Successfully uploaded PPT and generated URL")
            yield RunResponse(content=public_url, event=RunEvent.workflow_completed)
            
        except Exception as e:
            logger.error(f"Error uploading PPT: {str(e)}")
            yield RunResponse(content="", event=RunEvent.workflow_completed)
            return


# async def main():
#     chat = DeckGeneration()
#     output = []

#     logger.info("Workflow execution started.")

#     async for response in chat.run_workflow(deck_message):
#         logger.info(f"Received response: {response.content}")

#     logger.info(f"Workflow completed. Total responses: {len(output)}")
    
#     return output


# if __name__ == "__main__":
#     import asyncio
#     asyncio.run(main())