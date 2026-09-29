"""Vision Analyst: proposes facts that are visible in the photos (architecture doc §8).

The model sees one photo at a time and may only return a `PhotoAnalysis`. Code then:
- attaches the evidence photo ID (the model never chooses it),
- drops observations for fields a photo cannot show (schema-level block),
- normalises values, discarding anything malformed.
The result is only a set of proposals; nothing becomes a fact without the seller.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from listing_assistant.agent_io import PhotoAnalysis, PhotoView
from listing_assistant.field_schema import CategorySchema, NotObservableError, UnknownFieldError
from listing_assistant.llm import LLMOutputError
from listing_assistant.models import PrivacyFlag, VisionFactProposal
from listing_assistant.tools import AgentRun
from listing_assistant.toolset import PhotoWithBytes

# Photos are independent, so they are analysed in parallel (architecture doc §4).
MAX_PARALLEL_CALLS = 4


@dataclass(frozen=True)
class RejectedObservation:
    field_key: str
    reason: str


@dataclass(frozen=True)
class PhotoVisionResult:
    photo_id: str
    analyzed: bool
    view: PhotoView = PhotoView.OTHER
    privacy_flags: tuple[PrivacyFlag, ...] = ()
    instruction_text_detected: bool = False
    proposals: tuple[VisionFactProposal, ...] = ()
    rejected: tuple[RejectedObservation, ...] = ()


@dataclass(frozen=True)
class VisionReport:
    photos: tuple[PhotoVisionResult, ...] = field(default_factory=tuple)

    @property
    def proposals(self) -> list[VisionFactProposal]:
        return [p for result in self.photos for p in result.proposals]

    @property
    def rejected_count(self) -> int:
        return sum(len(result.rejected) for result in self.photos)


def screen_analysis(
    photo_id: str, analysis: PhotoAnalysis, schema: CategorySchema
) -> PhotoVisionResult:
    """Turn raw model output into validated proposals; pure code, easy to test."""
    accepted: dict[str, VisionFactProposal] = {}
    rejected = []
    for observation in analysis.observations:
        try:
            proposal = schema.validate_vision_proposal(
                VisionFactProposal(
                    field_key=observation.field_key,
                    value=observation.value,
                    confidence=observation.confidence,
                    evidence_photo_id=photo_id,
                )
            )
        except UnknownFieldError:
            rejected.append(RejectedObservation(observation.field_key, "unknown field"))
            continue
        except NotObservableError:
            rejected.append(RejectedObservation(observation.field_key, "not observable"))
            continue
        except ValueError:
            rejected.append(RejectedObservation(observation.field_key, "invalid value"))
            continue
        # One value per field per photo: keep the most confident one.
        current = accepted.get(proposal.field_key)
        if current is None or proposal.confidence > current.confidence:
            accepted[proposal.field_key] = proposal
    return PhotoVisionResult(
        photo_id=photo_id,
        analyzed=True,
        view=analysis.view,
        privacy_flags=tuple(dict.fromkeys(analysis.privacy_flags)),
        instruction_text_detected=analysis.instruction_text_detected,
        proposals=tuple(accepted.values()),
        rejected=tuple(rejected),
    )


def run_vision_analyst(run: AgentRun, schema: CategorySchema) -> VisionReport:
    photos: list[PhotoWithBytes] = run.call("get_listing_photos")
    if not photos:
        raise ValueError("a listing needs at least one photo before analysis")

    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_CALLS, len(photos))) as pool:
        futures = [(p, pool.submit(run.call, "vision_describe", photo=p)) for p in photos]

    results = []
    for photo, future in futures:
        try:
            analysis = future.result()
        except LLMOutputError:
            # A refused or malformed answer costs us this photo's proposals, nothing more.
            results.append(PhotoVisionResult(photo_id=photo.photo.id, analyzed=False))
            continue
        results.append(screen_analysis(photo.photo.id, analysis, schema))

    if not any(result.analyzed for result in results):
        run.mark_failed("no photo could be analysed")
        raise LLMOutputError(
            "Görüntü modeli hiçbir fotoğraf için kullanılabilir bir analiz döndürmedi."
        )
    return VisionReport(photos=tuple(results))
