# agents/general_agent/main.py
import os
from typing import Iterator

from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.models.azure import AzureOpenAI
from app_layer.utils.logger import get_logger

logger = get_logger()


class BasicChatAgent:
    def __init__(self, provider: str, model_name: str):
        self.provider = provider
        logger.info(f"Initializing BasicChatAgent with provider={provider}, model_name={model_name}")
        if self.provider == "openai":
            api_key = os.getenv("OPENAI_API_KEY")
            self.model = OpenAIChat(id=self.model_id, api_key=api_key)
            logger.info("OpenAI model initialized")
        elif self.provider == "azure":
            self.model = AzureOpenAI(id="o3-mini", api_version="2024-12-01-preview")
            logger.info("Azure OpenAI model initialized")
        else:
            logger.error(f"Invalid provider: {provider}")
            raise ValueError("Provider must be 'openai' or 'azure'")

    def agent_invoke(self, prompt: str, **agent_kwargs) -> Iterator[str]:
        logger.info(f"Invoking agent with prompt: {prompt}")
        agent = Agent(model=self.model)
        for run_response in agent.run(prompt, stream=True):
            logger.info(f"Generated response chunk: {run_response.content}")
            yield run_response.content


# if __name__ == "__main__":
#     agent = BasicChatAgent(provider="azure", model_name="o3-mini")
#     for response in agent.agent_invoke("Hello How are you"):
#         print(response)
