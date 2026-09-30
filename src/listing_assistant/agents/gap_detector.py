"""Gap Detector: finds missing required fields and asks the seller (architecture doc §4).

Which fields are missing is decided by code from the category schema. The model only
words the Turkish questions, and it only ever sees field names from our own schema file,
never seller text. If its wording is unusable or the model cannot be called, a fixed
template question is used.
"""

from listing_assistant.agent_io import GapQuestions
from listing_assistant.field_schema import CategorySchema, FieldDefinition
from listing_assistant.llm import LLMError, LLMRequest, TextPart
from listing_assistant.models import Clarification, Fact
from listing_assistant.prompting import load_prompt
from listing_assistant.text_utils import tr_lower
from listing_assistant.tools import AgentRun
from listing_assistant.toolset import describe_field

GAP_PROMPT = "gap_questions_v1"


def find_gaps(
    schema: CategorySchema, approved_facts: list[Fact], settled_keys: set[str]
) -> list[FieldDefinition]:
    """Required fields with no approved value that the seller has not already declined."""
    known = {fact.field_key for fact in approved_facts}
    return [schema.get(key) for key in schema.required_keys if key not in known | settled_keys]


def template_question(definition: FieldDefinition) -> str:
    return f"Lütfen aracınızın {tr_lower(definition.label_tr)} bilgisini girer misiniz?"


def phrase_questions(run: AgentRun, gaps: list[FieldDefinition]) -> dict[str, str]:
    prompt = load_prompt(GAP_PROMPT)
    fields = "\n".join(f"{describe_field(d)} [label: {d.label_tr}]" for d in gaps)
    request = LLMRequest(
        system=prompt.text,
        parts=(TextPart(f"Missing fields:\n{fields}"),),
        output_model=GapQuestions,
        prompt_version=prompt.version,
    )
    wanted = {d.key for d in gaps}
    phrased: dict[str, str] = {}
    try:
        output: GapQuestions = run.generate(request)
        phrased = {q.field_key: q.question_tr for q in output.questions if q.field_key in wanted}
    except LLMError:
        # Wording is cosmetic, and gate 1 is already recorded: an unusable answer, an
        # unreachable API or a spent budget must not stop the flow. Templates are safe.
        pass
    return {d.key: phrased.get(d.key) or template_question(d) for d in gaps}


def run_gap_detector(
    run: AgentRun, schema: CategorySchema, settled_keys: set[str]
) -> list[Clarification]:
    approved: list[Fact] = run.call("get_listing_facts")
    gaps = find_gaps(schema, approved, settled_keys)
    if not gaps:
        return []
    return run.call("request_user_input", questions=phrase_questions(run, gaps))
