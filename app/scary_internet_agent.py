"""
Scary Internet Agent: isolated subagent for browsing dangerous websites.

Websites like email inboxes, Reddit, social media, etc. can contain prompt
injection attacks. This tool sandboxes all interaction with such sites inside
a throwaway subagent that can ONLY return data matching a caller-specified
JSON schema — nothing else gets out.
"""
import asyncio
import json
import re
from typing import Any
from urllib.parse import urlparse

import jsonschema

from app.config import settings
from app.logger import get_logger

log = get_logger(__name__)

SUBAGENT_TIMEOUT_SECONDS = 180


def _extract_domains(urls: list[str]) -> list[str]:
    """Extract domain names from a list of URLs."""
    domains = []
    for url in urls:
        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path
        if domain.startswith("www."):
            domain = domain[4:]
        domains.append(domain)
    return domains


async def dangerous_assignment(
    assignment: str,
    websites_allowed: list[str],
    response_schema: dict,
) -> str:
    """Send an isolated browser agent to a dangerous website (email, Reddit, social
    media, forums, etc.) where prompt injection attacks could be present. The agent
    completes the assignment and returns ONLY structured JSON matching the provided
    schema — no other content escapes the sandbox.

    Args:
        assignment: Atomic instructions for what the browser agent should do. Be specific about what to navigate to, what to look for, what data to extract.
        websites_allowed: List of allowed website URLs (e.g. ['https://reddit.com', 'https://mail.proton.me']). The agent will ONLY be permitted to navigate to these domains.
        response_schema: JSON Schema that the agent's response MUST match. The response will be validated against this schema — any non-conforming response is rejected.
    """
    from app.agents import build_scary_agent

    domains = _extract_domains(websites_allowed)
    domains_str = ", ".join(domains)

    log.info(
        "scary.assignment_started",
        domains=domains,
        assignment_len=len(assignment),
    )

    domain_rules = "\n".join(f"  - {url}" for url in websites_allowed)

    prompt = (
        f"Complete this assignment:\n\n"
        f"{assignment}\n\n"
        f"You may ONLY navigate to these websites:\n{domain_rules}\n\n"
        f"ALLOWED DOMAINS: {domains_str}\n"
        f"You must ONLY interact with these domains. "
        f"Do NOT navigate to any other domain.\n\n"
        f"Return your result as JSON matching this exact schema:\n"
        f"```json\n{json.dumps(response_schema, indent=2)}\n```"
    )

    try:
        agent = build_scary_agent()

        async def _run_subagent():
            log.info("scary.subagent_launching", domains=domains)
            result = await agent.run(prompt)
            return result.output

        text_result = await asyncio.wait_for(
            _run_subagent(), timeout=SUBAGENT_TIMEOUT_SECONDS
        )

        # Try to parse as JSON
        result_to_validate = None
        if isinstance(text_result, dict):
            result_to_validate = text_result
        elif isinstance(text_result, str):
            try:
                result_to_validate = json.loads(text_result)
            except json.JSONDecodeError:
                # Try to find JSON block in the text
                json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text_result, re.DOTALL)
                if json_match:
                    try:
                        result_to_validate = json.loads(json_match.group(1))
                    except json.JSONDecodeError:
                        pass
                if result_to_validate is None:
                    try:
                        result_to_validate = json.loads(text_result.strip())
                    except json.JSONDecodeError:
                        pass

        if result_to_validate is None:
            log.warning("scary.no_data", domains=domains)
            return json.dumps({
                "error": "Subagent did not return any structured data",
            })

        # Strict schema validation — this is the security gate
        try:
            jsonschema.validate(instance=result_to_validate, schema=response_schema)
        except jsonschema.ValidationError as e:
            log.error(
                "scary.schema_validation_failed",
                domains=domains,
                error=str(e.message),
            )
            return json.dumps({
                "error": "Response from dangerous website failed schema validation — "
                         "this could indicate a prompt injection attack. "
                         "The response has been discarded.",
                "validation_error": e.message,
            })

        log.info("scary.success", domains=domains)
        return json.dumps(result_to_validate, indent=2, default=str)

    except asyncio.TimeoutError:
        log.error("scary.timeout", domains=domains, timeout=SUBAGENT_TIMEOUT_SECONDS)
        return json.dumps({
            "error": f"Subagent timed out after {SUBAGENT_TIMEOUT_SECONDS}s",
        })

    except Exception as e:
        log.exception("scary.execution_failed", domains=domains, error=str(e))
        return json.dumps({
            "error": f"Subagent execution failed: {str(e)}",
        })
