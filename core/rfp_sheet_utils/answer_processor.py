import asyncio
from typing import Dict, List
from app_layer.utils.logger import get_logger
from workflows.rfp_qna.rfp_qna_workflow import RfpAgent
import time
from concurrent.futures import ThreadPoolExecutor
from app_layer.utils.socket_app import sio

logger = get_logger()

# Maximum number of concurrent processes
MAX_CONCURRENT_PROCESSES = 10
WEBSOCKET_NAMESPACE = "/websocket/v1"
RESPONSE_EVENT = "recieve_sheet_answers"

def process_single_answer_sync(workflow: RfpAgent, row_obj: Dict) -> Dict:
    """Process a single row object synchronously and return the answer."""
    start_time = time.time()
    logger.info(f"Starting processing for row {row_obj['row_number']} in sheet {row_obj['sheetname']}")
    
    try:
        answer = workflow.run(input_message=row_obj["input_message"])
        end_time = time.time()
        logger.info(f"Completed processing row {row_obj['row_number']} in {end_time - start_time:.2f} seconds")
        return {
            "sheetname": row_obj["sheetname"],
            "row_number": row_obj["row_number"],
            "answer": answer.content["answer"],
            "confidence_score": answer.content["confidence_score"],
            "confidence_level": answer.content["confidence_level"]
        }
    except Exception as e:
        end_time = time.time()
        logger.error(f"Error processing row {row_obj['row_number']} after {end_time - start_time:.2f} seconds: {str(e)}")
        return None

async def process_answers_in_parallel(row_objects: List[Dict], session_id: str, system_message_id: str) -> List[Dict]:
    """
    Process multiple row objects in parallel using the RfpAgent workflow.
    Limits concurrent processing to MAX_CONCURRENT_PROCESSES at a time.
    """
    try:
        total_start_time = time.time()
        logger.info(f"Starting parallel processing of {len(row_objects)} rows")
        
        # Initialize response data
        response_data = {
            "chat_id": session_id,
            "message_id": system_message_id,
            "isComplete": False,
            "isLineComplete": False,
            "isError": False,
            "response": "",
            "isFirstMessage": False,
        }
        full_response: List = []
        
        # Initialize the workflow
        workflow = RfpAgent(
            provider="azure", 
            model_name="o4-mini"
        )
        
        # Create a semaphore to limit concurrent processes
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_PROCESSES)
        logger.info(f"Created semaphore with limit of {MAX_CONCURRENT_PROCESSES} concurrent processes")
        
        # Create a thread pool for CPU-bound tasks
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_PROCESSES) as executor:
            # Create tasks for each row object
            loop = asyncio.get_event_loop()
            tasks = []
            
            for row_obj in row_objects:
                async def process_with_semaphore(row):
                    async with semaphore:
                        result = await loop.run_in_executor(
                            executor,
                            process_single_answer_sync,
                            workflow,
                            row
                        )
                        if result:
                            # Send each answer to websocket as it's processed
                            response_data.update({
                                "response": result,
                                "isLineComplete": True
                            })
                            await sio.emit(
                                RESPONSE_EVENT,
                                response_data,
                                namespace=WEBSOCKET_NAMESPACE
                            )
                            full_response.append(result)
                        return result
                tasks.append(process_with_semaphore(row_obj))
            
            # Run all tasks concurrently
            logger.info("Starting concurrent execution of all tasks")
            answers = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Filter out exceptions and None results
            processed_answers = []
            for i, answer in enumerate(answers):
                if isinstance(answer, Exception):
                    logger.error(f"Task {i} failed with error: {str(answer)}")
                elif answer is not None:
                    processed_answers.append(answer)
        
        total_end_time = time.time()
        logger.info(f"Completed processing {len(processed_answers)} rows in {total_end_time - total_start_time:.2f} seconds")
        logger.info(f"Successfully processed {len(processed_answers)} out of {len(row_objects)} rows")
        
        # Send final completion message
        response_data.update({
            "isComplete": True,
            "isLineComplete": True,
            "response": ""
        })
        await sio.emit(
            RESPONSE_EVENT,
            response_data,
            namespace=WEBSOCKET_NAMESPACE
        )
        
        return processed_answers
        
    except Exception as e:
        logger.error(f"Error in parallel answer processing: {str(e)}")
        # Send error message to websocket
        response_data.update({
            "isError": True,
            "isComplete": True,
            "isLineComplete": True,
            "response": str(e)
        })
        await sio.emit(
            RESPONSE_EVENT,
            response_data,
            namespace=WEBSOCKET_NAMESPACE
        )
        raise 