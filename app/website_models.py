"""
Pydantic models for website subagent responses and website registry.
"""
from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional, Type

from pydantic import BaseModel


class Todo(BaseModel):
    todo: str
    due: date
    email_url: str


class EmailResponse(BaseModel):
    success: bool
    todos: Optional[List[Todo]] = None
    deleted: int
    filed: int


class HostMessage(BaseModel):
    timestamp: datetime
    message_abstract: str


@dataclass
class WebsiteConfig:
    name: str
    url: str
    response_model: Type[BaseModel]
    instructions: str


WEBSITE_REGISTRY: dict[str, WebsiteConfig] = {
    "email": WebsiteConfig(
        name="email",
        url="https://mail.proton.me/u/4/inbox",
        response_model=EmailResponse,
        instructions=(
            "You are an email management assistant. Navigate to the inbox, "
            "review emails, identify action items as todos with due dates, "
            "and report what was deleted and filed."
        ),
    ),
    "airbnb": WebsiteConfig(
        name="airbnb",
        url="https://www.airbnb.com/",
        response_model=HostMessage,
        instructions=(
            "You are an Airbnb host assistant. Navigate to the hosting dashboard, "
            "review recent guest messages, and extract each message with its "
            "timestamp and a brief abstract."
        ),
    ),
}
