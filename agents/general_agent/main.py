# agents/general_agent/main.py
import os
from typing import Iterator

from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.models.azure import AzureOpenAI


class BasicChatAgent:
    def __init__(self, provider: str, model_name: str):
        self.provider = provider
        if self.provider == "openai":
            api_key = os.getenv("OPENAI_API_KEY")
            self.model = OpenAIChat(id=self.model_id, api_key=api_key)
        elif self.provider == "azure":
            self.model = AzureOpenAI(id="o3-mini", api_version="2024-12-01-preview")
        else:
            raise ValueError("Provider must be 'openai' or 'azure'")

    def agent_invoke(self, prompt: str, **agent_kwargs) -> Iterator[str]:
        agent = Agent(model=self.model)
        for run_response in agent.run(prompt, stream=True):
            yield run_response.content


# if __name__ == "__main__":
#     agent = BasicChatAgent(provider="azure", model_name="o3-mini")
#     for response in agent.agent_invoke("Hello How are you"):
#         print(response)
