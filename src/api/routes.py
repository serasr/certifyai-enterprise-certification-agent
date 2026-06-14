# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE.md file in the project root for full license information.

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import AsyncGenerator, Mapping, Optional, Dict


import fastapi
from fastapi import Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse

import logging
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from azure.ai.projects.models import AgentVersionDetails
from openai.types.conversations.message import Message
from openai.types.responses import ResponseOutputMessage
from openai.types.conversations import Conversation

from azure.ai.projects.aio import AIProjectClient

from util import encode_project_resource_id

from urllib.parse import quote


from openai import AsyncOpenAI


from azure.identity.aio import AzureCliCredential as AsyncAzureCliCredential
import re


# Create a logger for this module
logger = logging.getLogger("azureaiapp")

# Set the log level for the azure HTTP logging policy to WARNING (or ERROR)
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)

from opentelemetry import trace

tracer = trace.get_tracer(__name__)

# Define the directory for your templates.
directory = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=directory)

# Create a new FastAPI router
router = fastapi.APIRouter()

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from typing import Optional
import secrets

security = HTTPBasic()

username = os.getenv("WEB_APP_USERNAME")
password = os.getenv("WEB_APP_PASSWORD")
basic_auth = username and password

def authenticate(credentials: Optional[HTTPBasicCredentials] = Depends(security)) -> None:

    if not basic_auth:
        logger.info("Skipping authentication: WEB_APP_USERNAME or WEB_APP_PASSWORD not set.")
        return
    
    correct_username = secrets.compare_digest(credentials.username, username)
    correct_password = secrets.compare_digest(credentials.password, password)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return

auth_dependency = Depends(authenticate) if basic_auth else None

def cleanup_created_at_metadata(metadata: Mapping[str, str]) -> None:
    """Remove oldest created_at timestamp entries to keep metadata under 16 items limit."""
    if not metadata:
        return

    # metadata go to be up to 16 items.  If there is more than that, remove the one ended with _created_at key with smallest value
    while len(metadata) > 16:
        created_at_keys = [k for k in metadata if k.endswith("_created_at")]
        if not created_at_keys:
            break  # No more _created_at keys to remove
        min_key = min(created_at_keys, key=metadata.get)
        del metadata[min_key]

def get_project_client(request: Request) -> AIProjectClient:
    return request.app.state.ai_project

def get_agent_version_details(request: Request) -> AgentVersionDetails:
    return request.app.state.agent_version_details

def get_openai_client(request: Request) -> AsyncOpenAI:
    return get_project_client(request).get_openai_client()

def get_created_at_label(message_id: str) -> str:
    return f"{message_id}_created_at"

def serialize_sse_event(data: Dict) -> str:
    return f"data: {json.dumps(data)}\n\n"

async def get_or_create_conversation(
    openai_client: AsyncOpenAI,
    conversation_id: Optional[str],
    agent_id: Optional[str],
    current_agent_id: str
) -> Conversation:
    """
    Get an existing conversation or create a new one.
    Returns the conversation_id.
    """
    conversation: Optional[Conversation] = None
    
    # Attempt to get an existing conversation if we have matching agent and conversation IDs
    if conversation_id and agent_id == current_agent_id:
        try:
            logger.info(f"Using existing conversation with ID {conversation_id}")
            conversation = await openai_client.conversations.retrieve(conversation_id=conversation_id)
            logger.info(f"Retrieved conversation: {conversation.id}")
        except Exception as e:
            logger.error(f"Error retrieving conversation: {e}")

    # Create a new conversation if we don't have one
    if not conversation:
        try:
            logger.info("Creating a new conversation")
            conversation = await openai_client.conversations.create()
            logger.info(f"Generated new conversation ID: {conversation.id}")
        except Exception as e:
            logger.error(f"Error creating conversation: {e}")
            raise HTTPException(status_code=400, detail=f"Error handling conversation: {e}")
    
    return conversation

async def get_message_and_annotations(event: Message | ResponseOutputMessage) -> Dict:
    annotations = []
    # Get file annotations for the file search.
    text = ""
    content = event.content[0]
    if content.type == "output_text" or content.type == "input_text":
        text = content.text
    if content.type == "output_text":
        for annotation in content.annotations:
            if annotation.type == "file_citation":
                ann = {
                    'label': annotation.filename,
                    "index": annotation.index
                }
                annotations.append(ann)
            elif annotation.type == "url_citation":
                ann = {
                    'label': annotation.title,
                    "index": annotation.start_index
                }
                annotations.append(ann)
            
    return {
        'content': text,
        'annotations': annotations
    }


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, _ = auth_dependency):
    return templates.TemplateResponse(
        "index.html", 
        {
            "request": request,
        }
    )

async def save_user_message_created_at(openai_client: AsyncOpenAI, conversation: Conversation,  input_created_at: float):
    conversation.metadata = conversation.metadata  or {}
    try:
        logger.info(f"Saving created_at.")
        messages = await openai_client.conversations.items.list(conversation_id=conversation.id, order="desc")
        last_input_message = None
        async for message in messages:
            if isinstance(message, Message) and message.role == "user":
                last_input_message = message
                break
        if last_input_message:
            conversation.metadata[get_created_at_label(last_input_message.id)] = str(input_created_at)
        cleanup_created_at_metadata(conversation.metadata)

        await openai_client.conversations.update(conversation.id, metadata=conversation.metadata)
        
        logger.info(f"Successfully saved created_at for user message")
        return  # Success, exit the retry loop

    except Exception as e:
        logger.error(f"Error updating message created_at.")
        

def extract_authorized_emp_id(conversation_history: str) -> str:
    """Extract the first employee ID mentioned by the user in the conversation."""
    matches = re.findall(r'(?:EMP|MGR)-\d+', conversation_history.upper())
    return matches[0] if matches else ""

def is_access_violation(user_message: str, authorized_id: str) -> bool:
    """Check if user is trying to access another employee's data."""
    if not authorized_id:
        return False
    requested_ids = re.findall(r'(?:EMP|MGR)-\d+', user_message.upper())
    if not requested_ids:
        return False  # No employee ID in message, not a violation
    for req_id in requested_ids:
        if req_id != authorized_id.upper():
            return True
    return False

def clean_citations(text: str) -> str:
    """Replace citation markers with readable knowledge base source names."""
    KB_SOURCES = {
        "0": "Workload Insights Report",
        "1": "Team Learning Report",
        "2": "Work Activity Signals",
        "3": "Engineering Certification Guide",
        "4": "Learner Performance Data",
        "5": "Employee Directory",
    }

    def replace_citation(match):
        doc_idx = match.group(1)
        source_name = KB_SOURCES.get(doc_idx, "Knowledge Base")
        return f"[{source_name}]"

    text = re.sub(r'【\d+:(\d+)†source】', replace_citation, text)
    text = re.sub(r'\[doc_\d+\]', '[Knowledge Base]', text)
    return text

async def get_result(
    agent: AgentVersionDetails,
    conversation: Conversation,
    user_message: str,
    project_client: AIProjectClient,
    carrier: Dict[str, str]
) -> AsyncGenerator[str, None]:
    ctx = TraceContextTextMapPropagator().extract(carrier=carrier)
    with tracer.start_as_current_span('get_result', context=ctx):
        async with project_client.get_openai_client() as openai_client:
            logger.info(f"get_result invoked for conversation={conversation.id}")
            input_created_at = datetime.now(timezone.utc).timestamp()
            try:
                # RBAC enforcement using conversation metadata
                try:
                    conv_metadata = conversation.metadata or {}
                    authorized_id = conv_metadata.get("authorized_emp_id", "")
                    
                    # Check if current message contains an employee ID
                    requested_ids = re.findall(r'(?:EMP|MGR)-\d+', user_message.upper())
                    
                    if requested_ids:
                        if not authorized_id:
                            # First time user provides their ID - store it
                            authorized_id = requested_ids[0]
                            conv_metadata["authorized_emp_id"] = authorized_id
                            await openai_client.conversations.update(
                                conversation.id, metadata=conv_metadata
                            )
                            logger.info(f"Authorized ID stored: {authorized_id}")
                        else:
                            # Check if they're trying to access another employee's data
                            for req_id in requested_ids:
                                if req_id != authorized_id:
                                    logger.info(f"Access violation: {authorized_id} tried to access {req_id}")
                                    violation_response = "I can only show you your own data. Team insights are available to managers only. Access logged for audit purposes."
                                    stream_data = {'content': violation_response, 'annotations': [], 'type': "completed_message"}
                                    yield serialize_sse_event(stream_data)
                                    return
                except Exception as rbac_err:
                    logger.warning(f"RBAC check error: {rbac_err}")
                # Step 1: Orchestrator handles RBAC, identity verification, and routing
                logger.info(f"Calling orchestrator agent: {agent.name}")
                orch_response = await openai_client.responses.create(
                    conversation=conversation.id,
                    input=user_message,
                    extra_body={"agent_reference": {"name": agent.name, "type": "agent_reference"}},
                )
                orch_output = orch_response.output_text
                logger.info(f"Orchestrator completed: {len(orch_output)} chars")

                user_message_stripped = user_message.strip().upper()
                is_just_emp_id = bool(re.match(r'^(EMP|MGR)-\d+$', user_message_stripped))

                if is_just_emp_id:
                    full_response = orch_output
                    logger.info("Identity verification only - no specialists needed")
                else:
                    # LLM-based routing - orchestrator decides which specialists are needed
                    routing_conv = await openai_client.conversations.create()
                    routing_response = await openai_client.responses.create(
                        conversation=routing_conv.id,
                        input=(
                            f"Based on this user request: '{user_message}', "
                            f"which specialist agents are needed to answer it fully? "
                            f"Reply with ONLY a comma-separated list from: "
                            f"learning-path-curator, study-plan-generator, assessment-agent, engagement-agent, none"
                        ),
                        extra_body={"agent_reference": {"name": agent.name, "type": "agent_reference"}},
                    )
                    routing_text = routing_response.output_text.lower()
                    valid_specialists = [
                        "learning-path-curator",
                        "study-plan-generator",
                        "assessment-agent",
                        "engagement-agent",
                    ]
                    required_specialists = [
                        s.strip() for s in routing_text.replace("\n", ",").split(",")
                        if s.strip() in valid_specialists
                    ]
                    logger.info(f"LLM routing decision: {required_specialists}")

                    if required_specialists:
                        accumulated_context = f"Original request: {user_message}\n\nOrchestrator analysis:\n{orch_output}\n\nPlease add your specialist perspective."
                        full_response = orch_output
                        final_annotations = []

                        for agent_name in required_specialists:
                            logger.info(f"Calling specialist agent: {agent_name}")
                            conv = await openai_client.conversations.create()
                            response = await openai_client.responses.create(
                                conversation=conv.id,
                                input=accumulated_context,
                                extra_body={"agent_reference": {"name": agent_name, "type": "agent_reference"}},
                            )
                            agent_output = response.output_text
                            logger.info(f"Agent {agent_name} completed: {len(agent_output)} chars")
                            accumulated_context = f"Original request: {user_message}\n\nPrevious findings:\n{agent_output}\n\nPlease build on the above and add your specialist perspective."
                            full_response = agent_output
                            if hasattr(response, 'output') and response.output:
                                for item in response.output:
                                    if hasattr(item, 'content') and item.content:
                                        for part in item.content:
                                            if hasattr(part, 'annotations') and part.annotations:
                                                final_annotations.extend(part.annotations)
                    else:
                        full_response = orch_output
                        logger.info("Orchestrator handled directly - no specialists needed")

                if full_response:
                    full_response = clean_citations(full_response)
                    stream_data = {'content': full_response, 'annotations': [], 'type': "completed_message"}
                    yield serialize_sse_event(stream_data)
            except Exception as e:
                logger.exception(f"Exception in get_result: {e}")
                error_data = {
                    'content': str(e),
                    'annotations': [],
                    'type': "completed_message"
                }
                yield serialize_sse_event(error_data)
            finally:
                yield serialize_sse_event({'type': "stream_end"})


@router.get("/chat/history")
async def history(
    request: Request,
    agent: AgentVersionDetails = Depends(get_agent_version_details),
    openai_client: AsyncOpenAI = Depends(get_openai_client),
    _ = auth_dependency
):
    with tracer.start_as_current_span("chat_history"):
        async with openai_client:
            conversation = await get_or_create_conversation(
                openai_client, None, None, agent.id
            )
            response = JSONResponse(content=[])
            response.set_cookie("conversation_id", conversation.id)
            response.set_cookie("agent_id", agent.id)
            return response

@router.get("/agent")
async def get_chat_agent(
    agent: AgentVersionDetails = Depends(get_agent_version_details),
):
    wsid = os.environ.get("AZURE_EXISTING_AIPROJECT_RESOURCE_ID")
    agent_id = os.environ.get("AZURE_EXISTING_AGENT_ID")
    agent_name = agent_id.split(":")[0]
    agent_version = agent_id.split(":")[1]
    agent_playground_url = f"https://ai.azure.com/nextgen/r/{encode_project_resource_id(wsid)}/build/agents/{quote(agent_name)}/build?version={agent_version}"
    return JSONResponse(content={"name": "CertifyAI - Enterprise Certification Assistant", "metadata": agent.metadata, "agentPlaygroundUrl": agent_playground_url})


@router.post("/chat")
async def chat(
    request: Request,
    project_client: AIProjectClient = Depends(get_project_client),
    agent: AgentVersionDetails = Depends(get_agent_version_details),
    
	_ = auth_dependency
):
    # Retrieve the conversation ID from the cookies (if available).
    conversation_id = request.cookies.get('conversation_id')
    agent_id = request.cookies.get('agent_id')    

    carrier = {}        
    TraceContextTextMapPropagator().inject(carrier)

    with tracer.start_as_current_span("chat_request"):
        async with project_client.get_openai_client() as openai_client:
            # if the connection no longer exist or agent is changed, create a new one
            conversation = await get_or_create_conversation(
                openai_client, conversation_id, agent_id, agent.id
            )
            conversation_id = conversation.id
            agent_id = agent.id
        
    # Parse the JSON from the request.
    try:
        user_message = await request.json()
    except Exception as e:
        logger.error(f"Invalid JSON in request: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid JSON in request: {e}")
    # Create a new message from the user's input.

    # Set the Server-Sent Events (SSE) response headers.
    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Content-Type": "text/event-stream"
    }
    logger.info(f"Starting streaming response for conversation ID {conversation_id}")

    # Create the streaming response using the generator.
    response = StreamingResponse(get_result(agent, conversation, user_message.get('message', ''), project_client, carrier), headers=headers)

    # Update cookies to persist the conversation and agent IDs.
    response.set_cookie("conversation_id", conversation_id)
    response.set_cookie("agent_id", agent_id)
    return response
