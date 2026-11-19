"""Turning a detected event into a readable research report.

Two analyzers, same interface:

  `TemplateAnalyzer` - FREE. Composes the report from the structured data the
                       detectors already produced. Deterministic, offline,
                       and incapable of inventing a fact. This is the default.

  `ClaudeAnalyzer`   - OPTIONAL. Adds natural-language explanation, costs
                       money per call, and is only used if an API key is set.

The template is not a degraded fallback. The detectors already record before
and after numbers, the risk flags carry their evidence, and the observation
knows which fields the provider failed to supply. A template can state all of
that accurately; what it cannot do is explain it in prose.
"""

from mihq.analysis.base import Analyzer, ResearchReport
from mihq.analysis.template import TemplateAnalyzer

__all__ = ["Analyzer", "ResearchReport", "TemplateAnalyzer", "build_analyzer"]


def build_analyzer(settings, *, repository=None):
    """Pick an analyzer based on configuration.

    Falls back to the free template analyzer whenever Claude is unavailable
    or switched off, so the pipeline always has something that works.
    """
    from mihq.logging_setup import get_logger

    log = get_logger(__name__)

    if settings.analysis_mode == "template":
        return TemplateAnalyzer(settings)

    if not settings.anthropic_api_key:
        log.info("ANALYSIS_MODE is claude but no API key is set; using the "
                 "free template analyzer instead")
        return TemplateAnalyzer(settings)

    try:
        from mihq.analysis.claude import ClaudeAnalyzer
        return ClaudeAnalyzer(settings, repository=repository)
    except ImportError as exc:
        log.warning("the anthropic package is not installed; using the free "
                    "template analyzer", extra={"error": str(exc)})
        return TemplateAnalyzer(settings)
