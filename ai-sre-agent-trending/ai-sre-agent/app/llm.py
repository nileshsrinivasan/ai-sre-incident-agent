"""OpenAI wrapper. If there is no API key (or the call fails) we fall back to simple rules,
so the whole project still runs end-to-end for free."""
import json
import logging

from openai import AsyncOpenAI

from .config import settings

log = logging.getLogger("llm")
_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, base_url=settings.OPENAI_BASE_URL or None) if settings.OPENAI_API_KEY else None

async def ask_json(system: str, payload: dict, fallback):
    """Returns (result_dict, source) where source says who produced the answer."""
    if _client is None:
        return fallback(), "rules"
    try:
        r = await _client.chat.completions.create(
            model=settings.OPENAI_MODEL, temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system + " Respond with a single JSON object only."},
                {"role": "user", "content": json.dumps(payload, default=str)[:12000]},
            ])
        return json.loads(r.choices[0].message.content), "openai"
    except Exception as e:
        log.warning("OpenAI call failed, using rules: %s", e)
        return fallback(), "rules(fallback)"
