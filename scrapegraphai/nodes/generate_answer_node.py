"""
GenerateAnswerNode Module
"""
from typing import List, Optional
from langchain_core.messages import SystemMessage
from langchain.prompts import PromptTemplate, ChatPromptTemplate, HumanMessagePromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.runnables import RunnableParallel
from langchain_openai import ChatOpenAI, AzureChatOpenAI
from langchain_aws import ChatBedrock
from langchain_mistralai import ChatMistralAI
from langchain_community.chat_models import ChatOllama
from tqdm import tqdm
from .base_node import BaseNode
from ..utils.output_parser import get_structured_output_parser, get_pydantic_output_parser
from ..prompts import (
    TEMPLATE_CHUNKS, TEMPLATE_NO_CHUNKS, TEMPLATE_MERGE,
    TEMPLATE_CHUNKS_MD, TEMPLATE_NO_CHUNKS_MD, TEMPLATE_MERGE_MD
)

class GenerateAnswerNode(BaseNode):
    """
    Initializes the GenerateAnswerNode class.

    Args:
        input (str): The input data type for the node.
        output (List[str]): The output data type(s) for the node.
        node_config (Optional[dict]): Configuration dictionary for the node,
        which includes the LLM model, verbosity, schema, and other settings.
        Defaults to None.
        node_name (str): The name of the node. Defaults to "GenerateAnswer".

    Attributes:
        llm_model: The language model specified in the node configuration.
        verbose (bool): Whether verbose mode is enabled.
        force (bool): Whether to force certain behaviors, overriding defaults.
        script_creator (bool): Whether the node is in script creation mode.
        is_md_scraper (bool): Whether the node is scraping markdown data.
        additional_info (Optional[str]): Any additional information to be
        included in the prompt templates.
    """
    def __init__(
        self,
        input: str,
        output: List[str],
        node_config: Optional[dict] = None,
        node_name: str = "GenerateAnswer",
    ):
        super().__init__(node_name, "node", input, output, 2, node_config)
        self.llm_model = node_config["llm_model"]

        if isinstance(node_config["llm_model"], ChatOllama):
            self.llm_model.format = "json"

        self.verbose = node_config.get("verbose", False)
        self.force = node_config.get("force", False)
        self.script_creator = node_config.get("script_creator", False)
        self.is_md_scraper = node_config.get("is_md_scraper", False)
        self.additional_info = node_config.get("additional_info")

    def execute(self, state: dict) -> dict:
        """
        Executes the GenerateAnswerNode.

        Args:
            state (dict): The current state of the graph. The input keys will be used
                          to fetch the correct data from the state.

        Returns:
            dict: The updated state with the output key containing the generated answer.
        """
        self.logger.info(f"--- Executing {self.node_name} Node ---")

        input_keys = self.get_input_keys(state)
        input_data = [state[key] for key in input_keys]
        user_prompt = input_data[0]
        doc = input_data[1]

        if self.node_config.get("schema", None) is not None:
            if isinstance(self.llm_model, (ChatOpenAI, ChatMistralAI)):
                self.llm_model = self.llm_model.with_structured_output(
                    schema=self.node_config["schema"]
                )
                output_parser = get_structured_output_parser(self.node_config["schema"])
                format_instructions = "NA"
            else:
                if not isinstance(self.llm_model, ChatBedrock):
                    output_parser = get_pydantic_output_parser(self.node_config["schema"])
                    format_instructions = output_parser.get_format_instructions()
                else:
                    output_parser = None
                    format_instructions = ""
        else:
            if not isinstance(self.llm_model, ChatBedrock):
                output_parser = JsonOutputParser()
                format_instructions = output_parser.get_format_instructions()
            else:
                output_parser = None
                format_instructions = ""

        if isinstance(self.llm_model, (ChatOpenAI, AzureChatOpenAI)) \
            and not self.script_creator \
            or self.force \
            and not self.script_creator or self.is_md_scraper:
            template_no_chunks_prompt = TEMPLATE_NO_CHUNKS_MD
            template_chunks_prompt = TEMPLATE_CHUNKS_MD
            template_merge_prompt = TEMPLATE_MERGE_MD
        else:
            template_no_chunks_prompt = TEMPLATE_NO_CHUNKS
            template_chunks_prompt = TEMPLATE_CHUNKS
            template_merge_prompt = TEMPLATE_MERGE

        if self.additional_info is not None:
            template_no_chunks_prompt = self.additional_info + template_no_chunks_prompt
            template_chunks_prompt = self.additional_info + template_chunks_prompt
            template_merge_prompt = self.additional_info + template_merge_prompt

        if len(doc) == 1:
            system_msg = SystemMessage(content=template_no_chunks_prompt.format(format_instructions=format_instructions, question=user_prompt))
            chat_template = ChatPromptTemplate.from_messages(
                [
                    system_msg,
                    HumanMessagePromptTemplate.from_template("The following is the website content:\n{web_content}"),
                ]
            )

            chain = chat_template | self.llm_model
            if output_parser:
                chain = chain | output_parser
            answer =  chain.invoke({"web_content" : doc[0]})

            state.update({self.output[0]: answer})
            return state

        chains_dict = {}
        system_msg = SystemMessage(content=template_chunks_prompt.format(format_instructions=format_instructions, question=user_prompt))
        batch_input = []
        for i, chunk in enumerate(tqdm(doc, desc="Processing chunks", disable=not self.verbose)):
            chat_template = ChatPromptTemplate.from_messages(
                [
                    system_msg,
                    HumanMessagePromptTemplate.from_template("Content of {chunk_id}:\n{context}"),
                ]
            )
            batch_input.append({"chunk_id":i + 1, "context":chunk})
            chain_name = f"chunk{i+1}"
            chains_dict[chain_name] = chat_template | self.llm_model
            if output_parser:
                chains_dict[chain_name] = chains_dict[chain_name] | output_parser
        
        async_runner = RunnableParallel(**chains_dict)
        batch_results =  async_runner.batch(inputs=batch_input)

        system_msg = SystemMessage(content=template_merge_prompt.format(format_instructions=format_instructions, question=user_prompt))
        merge_prompt = ChatPromptTemplate.from_messages(
                [
                    system_msg,
                    HumanMessagePromptTemplate.from_template("Here are all the chunks:\n{context}"),
                ]
            )

        merge_chain = merge_prompt | self.llm_model
        if output_parser:
            merge_chain = merge_chain | output_parser
        answer =  merge_chain.invoke({"context": batch_results})

        state.update({self.output[0]: answer})
        return state
